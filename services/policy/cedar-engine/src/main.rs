//! cedar-engine 进程入口：读取环境变量配置、加载策略、启动 HTTP 服务与热加载循环。
//!
//! 环境变量（均可选）：
//! - `CEDAR_LISTEN`：监听地址，默认 `127.0.0.1:8183`
//! - `CEDAR_POLICY_FILE` / `CEDAR_SCHEMA_FILE`：策略与 Schema 路径，默认本 crate 旁的 `../cedar/`
//! - `CEDAR_RELOAD_INTERVAL_S`：mtime 检查周期（秒），默认 5
//! - `CEDAR_MAX_BATCH`：单批最大条数，默认 256
//! - `CEDAR_MAX_BODY_BYTES`：请求体上限，默认 1048576
//! - `CEDAR_EVAL_TIMEOUT_MS`：单批判定超时，默认 1000

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use cedar_engine::{log, router, watch, Config, Engine};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_num(key: &str, default: u64) -> Result<u64, String> {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => {
            let n: u64 = v
                .trim()
                .parse()
                .map_err(|_| format!("{key} must be a positive integer, got {v:?}"))?;
            if n == 0 {
                return Err(format!("{key} must be > 0"));
            }
            Ok(n)
        }
        _ => Ok(default),
    }
}

fn config() -> Result<(String, Config, Duration), String> {
    let default_dir = concat!(env!("CARGO_MANIFEST_DIR"), "/../cedar");
    let cfg = Config {
        policy_file: PathBuf::from(env_or(
            "CEDAR_POLICY_FILE",
            &format!("{default_dir}/policies.cedar"),
        )),
        schema_file: PathBuf::from(env_or(
            "CEDAR_SCHEMA_FILE",
            &format!("{default_dir}/schema.cedarschema"),
        )),
        max_batch: env_num("CEDAR_MAX_BATCH", 256)? as usize,
        max_body_bytes: env_num("CEDAR_MAX_BODY_BYTES", 1024 * 1024)? as usize,
        eval_timeout: Duration::from_millis(env_num("CEDAR_EVAL_TIMEOUT_MS", 1000)?),
    };
    let interval = Duration::from_secs(env_num("CEDAR_RELOAD_INTERVAL_S", 5)?);
    Ok((env_or("CEDAR_LISTEN", "127.0.0.1:8183"), cfg, interval))
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! { _ = ctrl_c => {}, _ = term => {} }
}

#[tokio::main]
async fn main() {
    let (listen, cfg, interval) = match config() {
        Ok(c) => c,
        Err(e) => {
            log("error", &format!("invalid configuration: {e}"));
            std::process::exit(2);
        }
    };
    let engine = match Engine::new(cfg) {
        Ok(e) => Arc::new(e),
        Err(e) => {
            // 失败关闭：初始策略无效时不启动（调用方连不上即按 deny 处理）
            log("error", &format!("initial policy load failed: {e}"));
            std::process::exit(1);
        }
    };
    let listener = match tokio::net::TcpListener::bind(&listen).await {
        Ok(l) => l,
        Err(e) => {
            log("error", &format!("bind {listen}: {e}"));
            std::process::exit(1);
        }
    };
    let cur = engine.current();
    log(
        "info",
        &format!(
            "listening on {listen}; policies {} ({} rules, version {}); reload every {interval:?}",
            engine.cfg.policy_file.display(),
            cur.policy_count,
            cur.version
        ),
    );
    tokio::spawn(watch(engine.clone(), interval));
    if let Err(e) = axum::serve(listener, router(engine))
        .with_graceful_shutdown(shutdown_signal())
        .await
    {
        log("error", &format!("server error: {e}"));
        std::process::exit(1);
    }
}
