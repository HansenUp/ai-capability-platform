//! cedar-engine 测试：共享用例（cedar/tests.json）、热加载、坏策略保留旧版、失败关闭与 HTTP 契约。

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use cedar_engine::{router, watch, Config, Engine, ItemResult};
use serde_json::{json, Value};
use tower::ServiceExt;

fn cedar_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../cedar")
}

fn config(dir: &Path) -> Config {
    Config {
        policy_file: dir.join("policies.cedar"),
        schema_file: dir.join("schema.cedarschema"),
        max_batch: 16,
        max_body_bytes: 64 * 1024,
        eval_timeout: Duration::from_secs(5),
    }
}

/// 把仓库策略复制到临时目录，供修改类测试使用。
fn temp_copy() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    for f in ["policies.cedar", "schema.cedarschema"] {
        fs::copy(cedar_dir().join(f), tmp.path().join(f)).unwrap();
    }
    tmp
}

/// 写文件并把 mtime 推到未来，避免文件系统时间精度导致"看起来没变"。
fn write_bumped(path: &Path, content: &str, secs_ahead: u64) {
    fs::write(path, content).unwrap();
    let f = fs::OpenOptions::new().write(true).open(path).unwrap();
    f.set_modified(SystemTime::now() + Duration::from_secs(secs_ahead))
        .unwrap();
}

/// 与 policy_service 的实体映射一致：principal=User、resource=Capability、context={approved,taint,agent?}。
fn cedar_request(case: &Value, approved: bool) -> Value {
    let p = &case["principal"];
    let roles = match (&p["roles"], &p["role"]) {
        (Value::Array(r), _) => Value::Array(r.clone()),
        (_, Value::String(r)) => json!([r]),
        _ => json!([]),
    };
    let r = &case["resource"];
    let c = &case["context"];
    let mut context = json!({ "approved": approved, "taint": c["taint"].clone() });
    if let Some(agent) = c.get("agent") {
        context["agent"] = agent.clone();
    }
    json!({
        "principal": {"type": "User", "id": "u-1"},
        "action": {"type": "Action", "id": case.get("action").and_then(Value::as_str).unwrap_or("invoke")},
        "resource": {"type": "Capability", "id": "cap.test"},
        "context": context,
        "entities": [
            {"uid": {"type": "User", "id": "u-1"}, "attrs": {"tenant": p["tenant"], "roles": roles}, "parents": []},
            {"uid": {"type": "Capability", "id": "cap.test"}, "attrs": {
                "system": r["system"], "risk_level": r["risk_level"], "data_class": r["data_class"], "tenant": r["tenant"]
            }, "parents": []}
        ]
    })
}

const FORBID_PRIORITY: [&str; 3] = [
    "forbid:cross_tenant",
    "forbid:agent_cross_tenant",
    "forbid:taint_outbound",
];

fn first_by_priority(ids: &[String]) -> Option<String> {
    FORBID_PRIORITY
        .iter()
        .find(|f| ids.iter().any(|i| i == *f))
        .map(|s| s.to_string())
        .or_else(|| ids.first().cloned())
}

/// 与 policy_service 相同的 require_approval 合成规则（两次查询）。
fn synthesize(q1: &ItemResult, q2: Option<&ItemResult>) -> (String, String) {
    if q1.decision == "allow" {
        return ("allow".into(), q1.policy_ids[0].clone());
    }
    if let Some(q2) = q2 {
        if q2.decision == "allow" {
            let reason =
                first_by_priority(&q1.policy_ids).unwrap_or_else(|| q2.policy_ids[0].clone());
            return ("require_approval".into(), reason);
        }
    }
    let reason = first_by_priority(&q1.policy_ids).unwrap_or_else(|| {
        if q1.errors.is_empty() {
            "default_deny".into()
        } else {
            "policy_error".into()
        }
    });
    ("deny".into(), reason)
}

#[test]
fn shared_cases_match_expectations() {
    let engine =
        Engine::new(config(&cedar_dir())).expect("repository policies must load and validate");
    let cases: Vec<Value> =
        serde_json::from_str(&fs::read_to_string(cedar_dir().join("tests.json")).unwrap()).unwrap();
    assert!(cases.len() >= 20);
    for case in &cases {
        let approved = case["context"]["approved"].as_bool().unwrap();
        let mut reqs = vec![cedar_request(case, approved)];
        if !approved {
            reqs.push(cedar_request(case, true));
        }
        let (res, _) = engine.authorize_batch(&reqs);
        for r in &res {
            assert!(
                r.errors.is_empty(),
                "{}: unexpected errors {:?}",
                case["name"],
                r.errors
            );
        }
        let (decision, reason) = synthesize(&res[0], res.get(1));
        assert_eq!(
            decision,
            case["expect"].as_str().unwrap(),
            "case {}",
            case["name"]
        );
        if let Some(want) = case.get("expect_reason").and_then(Value::as_str) {
            assert_eq!(reason, want, "case {}", case["name"]);
        }
    }
}

#[test]
fn policy_ids_are_annotation_ids() {
    let engine = Engine::new(config(&cedar_dir())).unwrap();
    let ids: Vec<String> = engine
        .current()
        .policies
        .policies()
        .map(|p| p.id().to_string())
        .collect();
    for want in [
        "permit:read_in_tenant",
        "permit:write_low_in_tenant",
        "permit:write_high_approved",
        "forbid:cross_tenant",
        "forbid:agent_cross_tenant",
        "forbid:taint_outbound",
    ] {
        assert!(ids.iter().any(|i| i == want), "missing {want}");
    }
    assert_eq!(engine.current().version.len(), 16);
}

fn sample_case() -> Value {
    json!({
        "principal": {"roles": ["sales"], "tenant": "t1"},
        "resource": {"system": "CRM", "risk_level": "read", "data_class": "internal", "tenant": "t1"},
        "context": {"approved": false, "taint": []}
    })
}

#[test]
fn hot_reload_swaps_valid_policy() {
    let tmp = temp_copy();
    let engine = Engine::new(config(tmp.path())).unwrap();
    let v1 = engine.current().version.clone();
    assert!(
        engine.reload_if_changed().is_none(),
        "unchanged files must not reload"
    );
    // 去掉只读放行规则 → 同租户只读变为 deny
    let src = fs::read_to_string(tmp.path().join("policies.cedar")).unwrap();
    let without_read = src.replace(
        "@id(\"permit:read_in_tenant\")\npermit(principal, action == Action::\"invoke\", resource)\nwhen { resource.risk_level == \"read\" && principal.tenant == resource.tenant };",
        "",
    );
    assert_ne!(src, without_read);
    write_bumped(&tmp.path().join("policies.cedar"), &without_read, 10);
    let cur = engine
        .reload_if_changed()
        .expect("mtime changed")
        .expect("valid policy");
    assert_ne!(cur.version, v1);
    let (res, version) = engine.authorize_batch(&[cedar_request(&sample_case(), false)]);
    assert_eq!(version, cur.version);
    assert_eq!(res[0].decision, "deny");
}

#[test]
fn bad_policies_keep_previous_version() {
    let tmp = temp_copy();
    let engine = Engine::new(config(tmp.path())).unwrap();
    let v1 = engine.current().version.clone();
    let good = fs::read_to_string(tmp.path().join("policies.cedar")).unwrap();
    let bad_variants = [
        // 语法错误
        format!("{good}\npermit(principal, action, resource) when {{ ;"),
        // 不符合 Schema（未声明的属性）
        format!("{good}\n@id(\"permit:bogus\")\npermit(principal, action == Action::\"invoke\", resource) when {{ resource.owner == \"x\" }};"),
        // 缺少 @id
        format!("{good}\npermit(principal, action == Action::\"invoke\", resource) when {{ resource.risk_level == \"x\" }};"),
        // 重复 @id
        format!("{good}\n@id(\"forbid:cross_tenant\")\nforbid(principal, action, resource) when {{ resource.system == \"x\" }};"),
        // 空文件
        String::new(),
    ];
    for (i, bad) in bad_variants.iter().enumerate() {
        write_bumped(&tmp.path().join("policies.cedar"), bad, 10 + i as u64);
        let r = engine.reload_if_changed().expect("mtime changed");
        assert!(r.is_err(), "variant {i} should be rejected");
        assert_eq!(
            engine.current().version,
            v1,
            "variant {i} must keep old policies"
        );
        // 坏策略的 mtime 已记录，下个周期不会重复加载
        assert!(engine.reload_if_changed().is_none());
        let (res, _) = engine.authorize_batch(&[cedar_request(&sample_case(), false)]);
        assert_eq!(res[0].decision, "allow", "old policies still serve");
    }
    // 坏 Schema 同样被拒绝
    write_bumped(&tmp.path().join("policies.cedar"), &good, 30);
    write_bumped(
        &tmp.path().join("schema.cedarschema"),
        "entity User = { tenant: String ",
        30,
    );
    assert!(engine.reload().is_err());
    assert_eq!(engine.current().version, v1);
    assert!(
        Engine::new(config(tmp.path())).is_err(),
        "startup with invalid schema must fail"
    );
}

#[test]
fn invalid_requests_fail_closed() {
    let engine = Engine::new(config(&cedar_dir())).unwrap();
    let good = cedar_request(&sample_case(), false);
    let mut unknown_type = good.clone();
    unknown_type["principal"] = json!({"type": "Ghost", "id": "x"});
    let mut unknown_action = good.clone();
    unknown_action["action"] = json!({"type": "Action", "id": "delete_everything"});
    let mut bad_context = good.clone();
    bad_context["context"]["approved"] = json!("yes");
    let mut missing_context_field = good.clone();
    missing_context_field["context"] = json!({"approved": false});
    let mut no_entities = good.clone();
    no_entities["entities"] = json!([]);
    let mut bad_entity_attr = good.clone();
    bad_entity_attr["entities"][1]["attrs"]["tenant"] = json!(1);
    let mut missing_attr = good.clone();
    missing_attr["entities"][1]["attrs"]
        .as_object_mut()
        .unwrap()
        .remove("tenant");
    let mut bad_uid_string = good.clone();
    bad_uid_string["principal"] = json!("not a uid");
    let reqs = vec![
        unknown_type,
        unknown_action,
        bad_context,
        missing_context_field,
        no_entities,
        bad_entity_attr,
        missing_attr,
        bad_uid_string,
        json!("not an object"),
        json!({}),
    ];
    let (res, _) = engine.authorize_batch(&reqs);
    assert_eq!(res.len(), reqs.len());
    for (i, r) in res.iter().enumerate() {
        assert_eq!(r.decision, "deny", "request {i} must be denied: {r:?}");
        assert!(!r.errors.is_empty(), "request {i} must report an error");
    }
    let (ok, _) = engine.authorize_batch(&[good]);
    assert_eq!(ok[0].decision, "allow");
}

async fn call(app: axum::Router, method: &str, uri: &str, body: Body) -> (StatusCode, Value) {
    let resp = app
        .oneshot(
            Request::builder()
                .method(method)
                .uri(uri)
                .header("content-type", "application/json")
                .body(body)
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn http_contract() {
    let engine = Arc::new(Engine::new(config(&cedar_dir())).unwrap());
    let version = engine.current().version.clone();
    let app = router(engine.clone());

    let (s, v) = call(app.clone(), "GET", "/healthz", Body::empty()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["policy_version"], version.as_str());

    let body =
        json!({"requests": [cedar_request(&sample_case(), false), json!({"principal": "bad"})]});
    let (s, v) = call(
        app.clone(),
        "POST",
        "/v1/authorize_batch",
        Body::from(body.to_string()),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["policy_version"], version.as_str());
    assert_eq!(v["results"][0]["decision"], "allow");
    assert_eq!(
        v["results"][0]["policy_ids"],
        json!(["permit:read_in_tenant"])
    );
    assert_eq!(v["results"][1]["decision"], "deny");

    let (s, v) = call(
        app.clone(),
        "POST",
        "/v1/authorize_batch",
        Body::from("{not json"),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["results"], json!([]));
    let (s, _) = call(
        app.clone(),
        "POST",
        "/v1/authorize_batch",
        Body::from("{\"requests\": 1}"),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    let too_many = json!({"requests": vec![json!({}); 17]});
    let (s, _) = call(
        app.clone(),
        "POST",
        "/v1/authorize_batch",
        Body::from(too_many.to_string()),
    )
    .await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);
    let huge = format!(
        "{{\"requests\": [], \"pad\": \"{}\"}}",
        "x".repeat(70 * 1024)
    );
    let (s, _) = call(app.clone(), "POST", "/v1/authorize_batch", Body::from(huge)).await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);

    let (s, v) = call(app.clone(), "POST", "/v1/reload", Body::empty()).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["changed"], false);
}

#[tokio::test]
async fn http_reload_rejects_bad_policy() {
    let tmp = temp_copy();
    let engine = Arc::new(Engine::new(config(tmp.path())).unwrap());
    let v1 = engine.current().version.clone();
    write_bumped(&tmp.path().join("policies.cedar"), "permit(", 5);
    let (s, v) = call(router(engine.clone()), "POST", "/v1/reload", Body::empty()).await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["policy_version"], v1.as_str());
    assert_eq!(engine.current().version, v1);
}

#[tokio::test]
async fn timeout_denies_whole_batch() {
    let mut cfg = config(&cedar_dir());
    cfg.max_batch = 4096;
    cfg.max_body_bytes = 64 * 1024 * 1024;
    cfg.eval_timeout = Duration::from_nanos(1);
    let engine = Arc::new(Engine::new(cfg).unwrap());
    let body = json!({"requests": vec![cedar_request(&sample_case(), false); 4000]});
    let (s, v) = call(
        router(engine),
        "POST",
        "/v1/authorize_batch",
        Body::from(body.to_string()),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let results = v["results"].as_array().unwrap();
    assert_eq!(results.len(), 4000);
    assert!(results
        .iter()
        .all(|r| r["decision"] == "deny" && r["errors"][0] == "evaluation timeout"));
}

#[tokio::test]
async fn watcher_picks_up_changes() {
    let tmp = temp_copy();
    let engine = Arc::new(Engine::new(config(tmp.path())).unwrap());
    let v1 = engine.current().version.clone();
    let handle = tokio::spawn(watch(engine.clone(), Duration::from_millis(50)));
    let src = fs::read_to_string(tmp.path().join("policies.cedar")).unwrap();
    write_bumped(
        &tmp.path().join("policies.cedar"),
        &format!("{src}\n// comment-only change\n"),
        10,
    );
    let mut changed = false;
    for _ in 0..100 {
        tokio::time::sleep(Duration::from_millis(20)).await;
        if engine.current().version != v1 {
            changed = true;
            break;
        }
    }
    handle.abort();
    assert!(changed, "watcher should reload modified policies");
}
