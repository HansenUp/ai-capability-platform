//! Single, synthetic, pipe-only child. Never adopts a PID or invokes a shell.
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::time::{Duration, Instant};

#[derive(Default)]
#[cfg(not(f8_sidecar))]
pub struct Host {
    child: Option<Child>,
    input: Option<ChildStdin>,
    closing: bool,
    unknown: bool,
}

#[cfg(not(f8_sidecar))]
impl Host {
    pub fn status(&mut self) -> Result<&'static str, String> {
        if let Some(child) = self.child.as_mut() {
            match child.try_wait() {
                Ok(Some(_)) => { self.child = None; self.input = None; self.unknown = false; }
                Err(_) => { self.unknown = true; }
                _ => {}
            }
        }
        Ok(if self.unknown { "CLEANUP_UNKNOWN" } else if self.child.is_some() { "READY" }
           else if self.closing { "CLOSED" } else { "STOPPED" })
    }

    pub fn start(&mut self) -> Result<&'static str, String> {
        self.status()?;
        if self.closing || self.unknown || self.child.is_some() { return Err("START_BLOCKED".into()); }
        let mut random = [0u8; 32];
        std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut random))
            .map_err(|_| "RANDOM_UNAVAILABLE")?;
        let challenge: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let expected = format!("F8/1 {challenge}\n");
        let exe = std::env::current_exe().map_err(|_| "EXECUTABLE_UNAVAILABLE")?;
        let folder = exe.parent().ok_or("EXECUTABLE_UNAVAILABLE")?;
        let helper = if folder.file_name().and_then(|p| p.to_str()) == Some("MacOS") {
            folder.parent().ok_or("EXECUTABLE_UNAVAILABLE")?.join("Helpers/f8-synthetic-sidecar")
        } else { folder.join("f8-synthetic-sidecar") };
        if helper.is_symlink() || std::fs::read(&helper).map_err(|_| "SIDECAR_UNAVAILABLE")?.as_slice()
            != include_bytes!(env!("F8_SIDECAR_BINARY")) { return Err("SIDECAR_INTEGRITY_FAILED".into()); }
        let mut child = Command::new(helper).arg("--f8-synthetic-sidecar")
            .env_clear().current_dir("/").stdin(Stdio::piped()).stdout(Stdio::piped())
            .stderr(Stdio::null()).spawn().map_err(|_| "SPAWN_FAILED")?;
        self.input = child.stdin.take();
        let output = child.stdout.take().ok_or("PIPE_UNAVAILABLE")?;
        self.child = Some(child);
        let sent = self.input.as_mut().ok_or("PIPE_UNAVAILABLE")?.write_all(expected.as_bytes());
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(output).take(128).read_line(&mut line);
            let _ = tx.send(result.map(|_| line));
        });
        if sent.is_err() || !matches!(rx.recv_timeout(Duration::from_secs(3)), Ok(Ok(ref line)) if line == &expected) {
            // Retain the OS object until exact exit is observed; never retry spawn.
            self.unknown = true;
            self.input = None;
            if let Some(c) = self.child.as_mut() { let _ = c.kill(); }
            let _ = self.await_exit();
            return Err("HANDSHAKE_FAILED".into());
        }
        self.status()
    }

    fn await_exit(&mut self) -> Result<&'static str, String> {
        let until = Instant::now() + Duration::from_secs(3);
        while Instant::now() < until {
            self.status()?;
            if self.child.is_none() { return self.status(); }
            std::thread::sleep(Duration::from_millis(10));
        }
        self.unknown = true;
        Err("CLEANUP_UNKNOWN".into())
    }

    pub fn stop(&mut self) -> Result<&'static str, String> {
        if self.unknown { return Err("RECONCILE_REQUIRED".into()); }
        // Closing the owned pipe is the single stop request. EOF also handles parent exit.
        self.input = None;
        self.await_exit()
    }

    pub fn close(&mut self) -> Result<&'static str, String> {
        self.closing = true;
        self.stop()
    }
}

pub fn sidecar() -> Result<(), String> {
    let mut reader = BufReader::new(std::io::stdin()).take(128);
    let mut line = String::new();
    reader.read_line(&mut line).map_err(|_| "PIPE_READ")?;
    if line.len() != 70 || !line.starts_with("F8/1 ") || !line.ends_with('\n')
        || !line[5..69].bytes().all(|b| b.is_ascii_hexdigit()) { return Err("BAD_HANDSHAKE".into()); }
    let mut out = std::io::stdout();
    out.write_all(line.as_bytes()).and_then(|_| out.flush()).map_err(|_| "PIPE_WRITE")?;
    // No network, filesystem operations, business data or descendants.
    let mut byte = [0u8; 1];
    while reader.read(&mut byte).map_err(|_| "PIPE_READ")? != 0 {}
    Ok(())
}

#[cfg(not(f8_sidecar))]
pub fn self_test() -> Result<(), String> {
    let mut host = Host::default();
    assert_eq!(host.status()?, "STOPPED");
    for _ in 0..3 {
        assert_eq!(host.start()?, "READY");
        assert!(host.start().is_err());
        assert_eq!(host.stop()?, "STOPPED");
        assert_eq!(host.stop()?, "STOPPED");
    }
    host.start()?;
    assert_eq!(host.close()?, "CLOSED");
    assert!(host.start().is_err());
    assert_eq!(host.status()?, "CLOSED");
    println!("PASS native pipe handshake, duplicate-start rejection, 3 restart cycles, exact child wait, idempotent stop, irreversible close");
    Ok(())
}
