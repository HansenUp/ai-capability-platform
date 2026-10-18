//! Cedar 授权判定 sidecar（S8.3 / D126）。
//!
//! - `POST /v1/authorize_batch`：批量判定，每条返回 `{decision, policy_ids, errors}` 与当前 `policy_version`；
//! - `POST /v1/reload`：立即从磁盘重新加载策略；后台每隔若干秒检查策略 / Schema 文件 mtime 自动热加载；
//! - `GET /healthz`：健康检查，含 `policy_version`。
//!
//! 失败关闭：请求非法、实体 / context 不符合 Schema、策略求值出错、判定超时，一律 deny。
//! 新策略必须解析成功、每条带唯一 `@id`、并通过 Schema 严格校验，才原子替换（ArcSwap）；否则保留旧策略。

use std::collections::HashSet;
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use arc_swap::ArcSwap;
use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use cedar_policy::{
    Authorizer, Context, Decision, Entities, EntityUid, PolicyId, PolicySet, Request, Schema,
    ValidationMode, Validator,
};
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// 引擎配置（由 main 从环境变量读取，测试中直接构造）。
#[derive(Debug, Clone)]
pub struct Config {
    /// 策略文件（policies.cedar）
    pub policy_file: PathBuf,
    /// Schema 文件（schema.cedarschema）
    pub schema_file: PathBuf,
    /// 单次批量判定的最大条数，超过则整批拒绝（413）
    pub max_batch: usize,
    /// 请求体字节上限，超过则 413
    pub max_body_bytes: usize,
    /// 单批判定的超时，超时整批 deny
    pub eval_timeout: Duration,
}

/// 一份已加载并通过校验的策略快照。
pub struct Loaded {
    pub policies: PolicySet,
    pub schema: Schema,
    /// 策略与 Schema 内容的 sha256 前 16 位
    pub version: String,
    pub policy_count: usize,
    pub loaded_at: u64,
}

/// 单条判定结果（Cedar 原生只有 allow / deny；require_approval 由策略服务合成）。
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ItemResult {
    pub decision: &'static str,
    pub policy_ids: Vec<String>,
    pub errors: Vec<String>,
}

impl ItemResult {
    fn deny(err: impl Into<String>) -> Self {
        Self {
            decision: "deny",
            policy_ids: Vec::new(),
            errors: vec![err.into()],
        }
    }
}

/// 简单日志：写 stderr，带 Unix 时间戳。
pub fn log(level: &str, msg: &str) {
    eprintln!("{} cedar-engine {level}: {msg}", now_secs());
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn mtime(path: &PathBuf) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// 从磁盘读取并校验策略与 Schema；任何一步失败都返回错误（调用方据此保留旧策略）。
pub fn load(policy_file: &PathBuf, schema_file: &PathBuf) -> Result<Loaded, String> {
    let schema_src = std::fs::read_to_string(schema_file)
        .map_err(|e| format!("read schema {}: {e}", schema_file.display()))?;
    let policy_src = std::fs::read_to_string(policy_file)
        .map_err(|e| format!("read policies {}: {e}", policy_file.display()))?;
    let (schema, _warnings) = Schema::from_cedarschema_str(&schema_src)
        .map_err(|e| format!("schema parse error: {e}"))?;
    let parsed =
        PolicySet::from_str(&policy_src).map_err(|e| format!("policy parse error: {e}"))?;
    if parsed.templates().next().is_some() {
        return Err("policy templates are not supported".into());
    }
    // 以 @id 作为策略 id：判定结果里的 policy_ids 直接对应基线规则名，便于审计追溯
    let mut policies = PolicySet::new();
    let mut seen = HashSet::new();
    for p in parsed.policies() {
        let id = p
            .annotation("id")
            .ok_or_else(|| format!("policy {} is missing @id(...)", p.id()))?;
        if id.is_empty() || !seen.insert(id.to_string()) {
            return Err(format!("duplicate or empty @id: {id:?}"));
        }
        policies
            .add(p.new_id(PolicyId::new(id)))
            .map_err(|e| format!("policy set error: {e}"))?;
    }
    if seen.is_empty() {
        return Err("policy file contains no policies".into());
    }
    let result = Validator::new(schema.clone()).validate(&policies, ValidationMode::Strict);
    if !result.validation_passed() {
        let errs: Vec<String> = result.validation_errors().map(|e| e.to_string()).collect();
        return Err(format!("schema validation failed: {}", errs.join("; ")));
    }
    let mut h = Sha256::new();
    h.update(policy_src.as_bytes());
    h.update([0u8]);
    h.update(schema_src.as_bytes());
    let digest = h.finalize();
    let version: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
    Ok(Loaded {
        policies,
        schema,
        version,
        policy_count: seen.len(),
        loaded_at: now_secs(),
    })
}

/// 判定引擎：持有当前策略快照（ArcSwap 原子替换）与上次观察到的文件 mtime。
pub struct Engine {
    pub cfg: Config,
    current: ArcSwap<Loaded>,
    seen_mtimes: Mutex<(Option<SystemTime>, Option<SystemTime>)>,
}

impl Engine {
    /// 启动时加载；失败直接返回错误（进程应退出，不以空策略运行）。
    pub fn new(cfg: Config) -> Result<Self, String> {
        let mtimes = (mtime(&cfg.policy_file), mtime(&cfg.schema_file));
        let loaded = load(&cfg.policy_file, &cfg.schema_file)?;
        Ok(Self {
            cfg,
            current: ArcSwap::from_pointee(loaded),
            seen_mtimes: Mutex::new(mtimes),
        })
    }

    pub fn current(&self) -> Arc<Loaded> {
        self.current.load_full()
    }

    /// 强制重新加载：成功则原子替换并返回新版本；失败保留旧策略并返回错误。
    pub fn reload(&self) -> Result<Arc<Loaded>, String> {
        let mtimes = (mtime(&self.cfg.policy_file), mtime(&self.cfg.schema_file));
        // 无论成败都记录本次看到的 mtime：坏策略不会每个周期重复报错，文件再次变化时才重试
        if let Ok(mut m) = self.seen_mtimes.lock() {
            *m = mtimes;
        }
        match load(&self.cfg.policy_file, &self.cfg.schema_file) {
            Ok(loaded) => {
                let old = self.current().version.clone();
                self.current.store(Arc::new(loaded));
                let cur = self.current();
                log(
                    "info",
                    &format!(
                        "policies reloaded: {old} -> {} ({} policies)",
                        cur.version, cur.policy_count
                    ),
                );
                Ok(cur)
            }
            Err(e) => {
                log(
                    "error",
                    &format!(
                        "policy reload rejected, keeping {}: {e}",
                        self.current().version
                    ),
                );
                Err(e)
            }
        }
    }

    /// 文件 mtime 变化时才重新加载；返回 None 表示未变化。
    pub fn reload_if_changed(&self) -> Option<Result<Arc<Loaded>, String>> {
        let mtimes = (mtime(&self.cfg.policy_file), mtime(&self.cfg.schema_file));
        let changed = self
            .seen_mtimes
            .lock()
            .map(|m| *m != mtimes)
            .unwrap_or(true);
        changed.then(|| self.reload())
    }

    /// 对一批请求逐条判定（同步、CPU 密集，调用方放到阻塞线程池）。
    pub fn authorize_batch(&self, requests: &[Value]) -> (Vec<ItemResult>, String) {
        let loaded = self.current();
        let authorizer = Authorizer::new();
        let results = requests
            .iter()
            .map(|r| authorize_one(&authorizer, &loaded, r))
            .collect();
        (results, loaded.version.clone())
    }
}

fn parse_uid(v: Option<&Value>, field: &str) -> Result<EntityUid, String> {
    match v {
        Some(Value::String(s)) => {
            EntityUid::from_str(s).map_err(|e| format!("invalid {field}: {e}"))
        }
        Some(obj @ Value::Object(_)) => {
            EntityUid::from_json(obj.clone()).map_err(|e| format!("invalid {field}: {e}"))
        }
        _ => Err(format!("missing or invalid {field}")),
    }
}

/// 单条判定：任何解析 / 校验 / 求值错误都返回 deny（失败关闭）。
pub fn authorize_one(authorizer: &Authorizer, loaded: &Loaded, req: &Value) -> ItemResult {
    let Some(obj) = req.as_object() else {
        return ItemResult::deny("request must be an object");
    };
    let parsed = (|| -> Result<Request, String> {
        let principal = parse_uid(obj.get("principal"), "principal")?;
        let action = parse_uid(obj.get("action"), "action")?;
        let resource = parse_uid(obj.get("resource"), "resource")?;
        let ctx_json = obj.get("context").cloned().unwrap_or_else(|| json!({}));
        let context = Context::from_json_value(ctx_json, Some((&loaded.schema, &action)))
            .map_err(|e| format!("invalid context: {e}"))?;
        Request::new(principal, action, resource, context, Some(&loaded.schema))
            .map_err(|e| format!("invalid request: {e}"))
    })();
    let request = match parsed {
        Ok(r) => r,
        Err(e) => return ItemResult::deny(e),
    };
    let entities_json = obj.get("entities").cloned().unwrap_or_else(|| json!([]));
    let entities = match Entities::from_json_value(entities_json, Some(&loaded.schema)) {
        Ok(e) => e,
        Err(e) => return ItemResult::deny(format!("invalid entities: {e}")),
    };
    let resp = authorizer.is_authorized(&request, &loaded.policies, &entities);
    let mut policy_ids: Vec<String> = resp
        .diagnostics()
        .reason()
        .map(|id| id.to_string())
        .collect();
    policy_ids.sort();
    let errors: Vec<String> = resp.diagnostics().errors().map(|e| e.to_string()).collect();
    // 求值出现错误时即使结果为 Allow 也按 deny 处理
    let decision = if resp.decision() == Decision::Allow && errors.is_empty() {
        "allow"
    } else {
        "deny"
    };
    ItemResult {
        decision,
        policy_ids,
        errors,
    }
}

fn error_response(status: StatusCode, msg: &str) -> Response {
    (status, Json(json!({ "error": msg, "results": [] }))).into_response()
}

async fn authorize_batch(State(engine): State<Arc<Engine>>, body: Bytes) -> Response {
    let parsed: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => return error_response(StatusCode::BAD_REQUEST, &format!("invalid JSON: {e}")),
    };
    let Some(requests) = parsed.get("requests").and_then(Value::as_array).cloned() else {
        return error_response(
            StatusCode::BAD_REQUEST,
            "body must be {\"requests\": [...]}",
        );
    };
    if requests.len() > engine.cfg.max_batch {
        return error_response(
            StatusCode::PAYLOAD_TOO_LARGE,
            &format!(
                "batch too large: {} > {}",
                requests.len(),
                engine.cfg.max_batch
            ),
        );
    }
    let n = requests.len();
    let timeout = engine.cfg.eval_timeout;
    let eng = engine.clone();
    let job = tokio::task::spawn_blocking(move || eng.authorize_batch(&requests));
    let (results, version) = match tokio::time::timeout(timeout, job).await {
        Ok(Ok(out)) => out,
        Ok(Err(e)) => {
            log("error", &format!("authorize task failed: {e}"));
            (
                vec![ItemResult::deny("internal error"); n],
                engine.current().version.clone(),
            )
        }
        Err(_) => {
            log(
                "warn",
                &format!("authorize batch of {n} timed out after {timeout:?}"),
            );
            (
                vec![ItemResult::deny("evaluation timeout"); n],
                engine.current().version.clone(),
            )
        }
    };
    Json(json!({ "results": results, "policy_version": version })).into_response()
}

async fn reload(State(engine): State<Arc<Engine>>) -> Response {
    let eng = engine.clone();
    let before = engine.current().version.clone();
    match tokio::task::spawn_blocking(move || eng.reload()).await {
        Ok(Ok(cur)) => Json(json!({
            "status": "ok",
            "policy_version": cur.version,
            "changed": cur.version != before,
        }))
        .into_response(),
        Ok(Err(e)) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({ "status": "rejected", "error": e, "policy_version": engine.current().version })),
        )
            .into_response(),
        Err(e) => error_response(StatusCode::INTERNAL_SERVER_ERROR, &format!("reload task failed: {e}")),
    }
}

async fn healthz(State(engine): State<Arc<Engine>>) -> Json<Value> {
    let cur = engine.current();
    Json(json!({
        "status": "ok",
        "policy_version": cur.version,
        "policies": cur.policy_count,
        "loaded_at": cur.loaded_at,
    }))
}

/// HTTP 路由；请求体上限由 `max_body_bytes` 控制（超过返回 413）。
pub fn router(engine: Arc<Engine>) -> Router {
    let limit = engine.cfg.max_body_bytes;
    Router::new()
        .route("/v1/authorize_batch", post(authorize_batch))
        .route("/v1/reload", post(reload))
        .route("/healthz", get(healthz))
        .layer(DefaultBodyLimit::max(limit))
        .with_state(engine)
}

/// 后台热加载循环：每 `interval` 检查一次文件 mtime。
pub async fn watch(engine: Arc<Engine>, interval: Duration) {
    let mut tick = tokio::time::interval(interval);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    tick.tick().await;
    loop {
        tick.tick().await;
        let eng = engine.clone();
        let _ = tokio::task::spawn_blocking(move || eng.reload_if_changed()).await;
    }
}
