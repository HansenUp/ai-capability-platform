//! Fixed repository/venv development adapter; never accepts executable paths or credentials.
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::time::{Duration, Instant};

type Output = BufReader<ChildStdout>;
#[derive(Default)]
pub struct Host {
    child: Option<Child>,
    input: Option<ChildStdin>,
    output: Option<Output>,
    root: Option<PathBuf>,
    closing: bool,
    unknown: bool,
    last_exit: Option<i32>,
}
impl Host {
    pub fn status(&mut self) -> Result<&'static str, String> {
        if let Some(child) = self.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    self.last_exit = status.code();
                    self.child = None;
                    self.input = None;
                    self.output = None;
                    self.unknown = false;
                    if let Some(root) = self.root.take() {
                        if root.exists() {
                            std::fs::remove_dir_all(root)
                                .map_err(|_| "DIRECTORY_CLEANUP_FAILED")?;
                        }
                    }
                }
                Err(_) => self.unknown = true,
                _ => {}
            }
        }
        Ok(if self.unknown {
            "CLEANUP_UNKNOWN"
        } else if self.child.is_some() {
            "READY"
        } else if self.closing {
            "CLOSED"
        } else {
            "STOPPED"
        })
    }
    fn line(&mut self) -> Result<String, String> {
        let mut output = self.output.take().ok_or("PIPE_UNAVAILABLE")?;
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let read = output.by_ref().take(32769).read_line(&mut line);
            let _ = tx.send((output, read, line));
        });
        match rx.recv_timeout(Duration::from_secs(15)) {
            Ok((output, Ok(_), line)) if line.len() <= 32768 && line.ends_with('\n') => {
                self.output = Some(output);
                Ok(line)
            }
            _ => {
                self.unknown = true;
                self.input = None;
                Err("RUNTIME_PIPE_UNKNOWN".into())
            }
        }
    }
    pub fn start(&mut self) -> Result<&'static str, String> {
        self.status()?;
        if self.closing || self.unknown || self.child.is_some() {
            return Err("START_BLOCKED".into());
        }
        let repo = PathBuf::from(env!("F8_DEV_REPO"));
        let python = repo.join(".venv/bin/python");
        if python.canonicalize().map_err(|_| "PYTHON_UNAVAILABLE")?
            != PathBuf::from(env!("F8_DEV_PYTHON_RESOLVED"))
        {
            return Err("PYTHON_PATH_CHANGED".into());
        }
        let python_bytes = std::fs::read(&python).map_err(|_| "PYTHON_UNAVAILABLE")?;
        if format!("{:x}", Sha256::digest(python_bytes)) != env!("F8_DEV_PYTHON_SHA256") {
            return Err("PYTHON_CHANGED".into());
        }
        let script = repo.join("apps/desktop/local-dev/sidecar.py");
        if script.is_symlink() {
            return Err("RUNTIME_ENTRY_CHANGED".into());
        }
        let mut random = [0u8; 32];
        std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut random))
            .map_err(|_| "RANDOM_UNAVAILABLE")?;
        let nonce: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let mut directory_random = [0u8; 16];
        std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut directory_random))
            .map_err(|_| "RANDOM_UNAVAILABLE")?;
        let directory_id: String = directory_random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        let root = PathBuf::from("/private/tmp").join(format!("aip-f8-runtime-{directory_id}"));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&root)
            .map_err(|_| "OWNED_DIRECTORY_FAILED")?;
        if std::fs::write(root.join("sources.json"), env!("F8_DEV_SOURCES_JSON")).is_err() {
            let _ = std::fs::remove_dir_all(root);
            return Err("SOURCE_MANIFEST_FAILED".into());
        }
        // The authorized interpreter verifies the reviewed entry before execution
        // in that same process. No plaintext program is returned or copied out.
        const BOOTSTRAP: &str = r#"import hashlib,pathlib,sys
p=pathlib.Path(sys.argv[1])
source=p.read_bytes()
expected=sys.argv[2]
root=sys.argv[3]
if p.is_symlink() or hashlib.sha256(source).hexdigest()!=expected:
    raise SystemExit('RUNTIME_ENTRY_CHANGED')
sys.argv=[str(p),root]
exec(compile(source,str(p),'exec'),{'__name__':'__main__','__file__':str(p),'__builtins__':__builtins__})
"#;
        let spawned = Command::new(python)
            .arg("-I")
            .arg("-c")
            .arg(BOOTSTRAP)
            .arg(script)
            .arg(env!("F8_DEV_ENTRY_SHA256"))
            .arg(&root)
            .env_clear()
            .env("HOME", &root)
            .env("TMPDIR", &root)
            .env("PATH", "/usr/bin:/bin")
            .current_dir("/")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(_) => {
                let _ = std::fs::remove_dir_all(root);
                return Err("SPAWN_FAILED".into());
            }
        };
        self.input = child.stdin.take();
        self.output = child.stdout.take().map(BufReader::new);
        self.child = Some(child);
        self.root = Some(root);
        let expected = format!("F8/1 {nonce}\n");
        if self
            .input
            .as_mut()
            .ok_or("PIPE_UNAVAILABLE")?
            .write_all(expected.as_bytes())
            .is_err()
            || self.line().as_deref() != Ok(expected.as_str())
        {
            self.input = None;
            let _ = self.await_exit();
            return Err("RUNTIME_START_FAILED".into());
        }
        self.status()
    }
    pub fn request(&mut self, message: serde_json::Value) -> Result<serde_json::Value, String> {
        if self.status()? != "READY" {
            return Err("RUNTIME_NOT_READY".into());
        }
        let mut body = serde_json::to_vec(&message).map_err(|_| "INVALID_MESSAGE")?;
        body.push(b'\n');
        self.input
            .as_mut()
            .ok_or("PIPE_UNAVAILABLE")?
            .write_all(&body)
            .map_err(|_| "PIPE_WRITE_FAILED")?;
        let response: serde_json::Value =
            serde_json::from_str(&self.line()?).map_err(|_| "INVALID_RUNTIME_REPLY")?;
        if response.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            return Err(response
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("RUNTIME_REJECTED")
                .into());
        }
        Ok(response
            .get("value")
            .cloned()
            .unwrap_or(serde_json::Value::Null))
    }
    fn await_exit(&mut self) -> Result<&'static str, String> {
        let until = Instant::now() + Duration::from_secs(5);
        while Instant::now() < until {
            self.status()?;
            if self.child.is_none() {
                return self.status();
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        self.unknown = true;
        Err("CLEANUP_UNKNOWN".into())
    }
    pub fn stop(&mut self) -> Result<&'static str, String> {
        if self.unknown {
            return Err("RECONCILE_REQUIRED".into());
        }
        self.input = None;
        self.await_exit()
    }
    pub fn clean_exit(&self) -> bool {
        self.child.is_none() && self.root.is_none() && self.last_exit == Some(0)
    }
    pub fn close(&mut self) -> Result<&'static str, String> {
        self.closing = true;
        self.stop()
    }
}
