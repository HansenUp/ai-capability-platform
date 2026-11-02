mod lifecycle;
mod local_runtime;
use std::sync::{Arc, Mutex};
use tauri::Manager;

type SharedHost = Arc<Mutex<Host>>;
enum Host {
    Synthetic(lifecycle::Host),
    Local(local_runtime::Host),
}
impl Host {
    fn start(&mut self) -> Result<&'static str, String> {
        match self {
            Self::Synthetic(h) => h.start(),
            Self::Local(h) => h.start(),
        }
    }
    fn stop(&mut self) -> Result<&'static str, String> {
        match self {
            Self::Synthetic(h) => h.stop(),
            Self::Local(h) => h.stop(),
        }
    }
    fn status(&mut self) -> Result<&'static str, String> {
        match self {
            Self::Synthetic(h) => h.status(),
            Self::Local(h) => h.status(),
        }
    }
    fn close(&mut self) -> Result<&'static str, String> {
        match self {
            Self::Synthetic(h) => h.close(),
            Self::Local(h) => h.close(),
        }
    }
    fn clean_exit(&self) -> bool {
        match self {
            Self::Synthetic(_) => true,
            Self::Local(h) => h.clean_exit(),
        }
    }
    fn desktop(&mut self, message: serde_json::Value) -> Result<serde_json::Value, String> {
        match self {
            Self::Local(h) => h.request(message),
            _ => Err("LOCAL_RUNTIME_DISABLED".into()),
        }
    }
}
struct LoginSequence(std::sync::atomic::AtomicUsize);
struct LoginWindows(Mutex<Option<String>>);
fn close_login(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app.state::<LoginWindows>();
    let label = { state.0.lock().map_err(|_| "LOGIN_WINDOW_FAILED")?.take() };
    if let Some(label) = label {
        if let Some(login) = app.get_webview_window(&label) {
            login.close().map_err(|_| "LOGIN_WINDOW_FAILED")?;
        }
    }
    Ok(())
}
#[derive(Clone, Copy, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum DesktopAction {
    Login,
    AuthStatus,
    Logout,
    Read,
    SetOnline,
    Diagnostics,
}
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct DesktopMessage {
    action: DesktopAction,
    #[serde(skip_serializing_if = "Option::is_none")]
    online: Option<bool>,
}
#[tauri::command]
async fn desktop_command(
    window: tauri::WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, SharedHost>,
    mode: tauri::State<'_, VerificationMode>,
    sequence: tauri::State<'_, LoginSequence>,
    windows: tauri::State<'_, LoginWindows>,
    message: DesktopMessage,
) -> Result<serde_json::Value, String> {
    if window.label() != "main" || !window.url().map(|u| local(&u)).unwrap_or(false) {
        return Err("CALLER_REJECTED".into());
    }
    if matches!(message.action, DesktopAction::SetOnline) != message.online.is_some() {
        return Err("INVALID_MESSAGE".into());
    }
    let action = message.action;
    let going_offline = matches!(action, DesktopAction::SetOnline) && message.online == Some(false);
    let value = serde_json::to_value(message).map_err(|_| "INVALID_MESSAGE")?;
    let host = Arc::clone(state.inner());
    let result = tauri::async_runtime::spawn_blocking(move || {
        host.lock()
            .map_err(|_| "HOST_UNAVAILABLE".to_string())?
            .desktop(value)
    })
    .await
    .map_err(|_| "HOST_TASK_FAILED")??;
    if matches!(action, DesktopAction::Login) {
        close_login(&app)?;
        let url = tauri::Url::parse(
            result
                .get("url")
                .and_then(|v| v.as_str())
                .ok_or("LOGIN_URL_UNAVAILABLE")?,
        )
        .map_err(|_| "LOGIN_URL_REJECTED")?;
        if url.scheme() != "http"
            || url.host_str() != Some("127.0.0.1")
            || url.port() != Some(8180)
            || !url.username().is_empty()
            || url.password().is_some()
            || url.path() != "/realms/ai-platform/protocol/openid-connect/auth"
        {
            return Err("LOGIN_URL_REJECTED".into());
        }
        let test = mode.0.load(std::sync::atomic::Ordering::SeqCst);
        let generation = sequence.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let label = format!("f8-login-{generation}");
        *windows.0.lock().map_err(|_| "LOGIN_WINDOW_FAILED")? = Some(label.clone());
        let username = if generation % 2 == 0 {
            "f8-alice"
        } else {
            "f8-bob"
        };
        let script = if test {
            format!("window.addEventListener('DOMContentLoaded',()=>{{const u=document.querySelector('input[name=username]'),p=document.querySelector('input[name=password]');if(u&&p){{u.value='{username}';p.value='f8-local-development-only';p.form.requestSubmit();}}}});")
        } else {
            String::new()
        };
        tauri::WebviewWindowBuilder::new(&app, label, tauri::WebviewUrl::External(url))
            .title("本机 Keycloak 登录")
            .incognito(true)
            .inner_size(620.0, 720.0)
            .initialization_script(script)
            .on_navigation(|u| {
                u.scheme() == "http"
                    && u.host_str() == Some("127.0.0.1")
                    && u.username().is_empty()
                    && u.password().is_none()
                    && ((u.port() == Some(8180) && u.path().starts_with("/realms/ai-platform/"))
                        || (u.port() == Some(18765) && u.path() == "/callback"))
            })
            .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
            .build()
            .map_err(|_| "LOGIN_WINDOW_FAILED")?;
        return Ok(serde_json::json!({"pending":true}));
    }
    if matches!(action, DesktopAction::Logout)
        || going_offline
        || (matches!(action, DesktopAction::AuthStatus) && !result.is_null())
    {
        close_login(&app)?;
    }
    Ok(result)
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum Action {
    Start,
    Stop,
    Status,
    Close,
    Reconcile,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    action: Action,
}

fn local(url: &tauri::Url) -> bool {
    url.scheme() == "tauri"
        && url.host_str() == Some("localhost")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && matches!(url.path(), "" | "/" | "/index.html")
        && url.query().is_none()
}

#[tauri::command]
async fn host_command(
    window: tauri::WebviewWindow,
    message: Message,
    state: tauri::State<'_, SharedHost>,
) -> Result<String, String> {
    if window.label() != "main" || !window.url().map(|u| local(&u)).unwrap_or(false) {
        return Err("CALLER_REJECTED".into());
    }
    let state = Arc::clone(state.inner());
    let result = tauri::async_runtime::spawn_blocking(move || {
        let mut host = state.lock().map_err(|_| "HOST_UNAVAILABLE".to_string())?;
        match message.action {
            Action::Start => host.start(),
            Action::Stop => host.stop(),
            Action::Status | Action::Reconcile => host.status(),
            Action::Close => host.close(),
        }
        .map(str::to_owned)
    })
    .await
    .map_err(|_| "HOST_TASK_FAILED")??;
    if result == "CLOSED" {
        window.close().map_err(|_| "WINDOW_CLOSE_FAILED")?;
    }
    Ok(result)
}

fn contract_self_test() {
    for good in [
        "tauri://localhost",
        "tauri://localhost/",
        "tauri://localhost/index.html",
    ] {
        assert!(local(&tauri::Url::parse(good).unwrap()));
    }
    for bad in [
        "https://localhost/",
        "tauri://other/",
        "tauri://localhost:99/",
        "tauri://alice@localhost/",
        "tauri://localhost/other.html",
        "tauri://localhost/?token=x",
    ] {
        assert!(
            !local(&tauri::Url::parse(bad).unwrap()),
            "accepted forbidden origin"
        );
    }
    use serde::de::value::{Error, MapDeserializer};
    let valid = MapDeserializer::<_, Error>::new(vec![("action", "start")].into_iter());
    assert!(<Message as serde::Deserialize>::deserialize(valid).is_ok());
    for fields in [
        vec![("action", "exec")],
        vec![("action", "start"), ("path", "/bin/sh")],
        vec![("action", "stop"), ("pid", "1")],
    ] {
        let value = MapDeserializer::<_, Error>::new(fields.into_iter());
        assert!(<Message as serde::Deserialize>::deserialize(value).is_err());
    }
    println!("PASS exact native origins and actual serde message schema");
}

struct VerificationMode(std::sync::atomic::AtomicBool);
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct VerificationReport {
    passed: bool,
    cases: Vec<String>,
}
#[tauri::command]
fn verification_report(
    window: tauri::WebviewWindow,
    app: tauri::AppHandle,
    mode: tauri::State<'_, VerificationMode>,
    state: tauri::State<'_, SharedHost>,
    report: VerificationReport,
) -> Result<(), String> {
    if window.label() != "main"
        || !window.url().map(|u| local(&u)).unwrap_or(false)
        || !mode.0.swap(false, std::sync::atomic::Ordering::SeqCst)
        || report.cases.len() > 32
        || report.cases.iter().any(|s| s.len() > 500)
    {
        return Err("VERIFICATION_DISABLED".into());
    }
    let (closed, clean) = {
        let mut host = state.lock().map_err(|_| "HOST_UNAVAILABLE")?;
        (host.close()? == "CLOSED", host.clean_exit())
    };
    println!(
        "F8_WEBVIEW_RESULT {{\"passed\":{},\"closed\":{},\"runtime_clean\":{},\"cases\":{:?}}}",
        report.passed && clean,
        closed,
        clean,
        report.cases
    );
    app.exit(if report.passed && closed && clean {
        0
    } else {
        1
    });
    Ok(())
}

fn main() {
    let argument = std::env::args().nth(1);
    let local_dev = matches!(
        argument.as_deref(),
        Some("--f8-local-dev" | "--f8-local-webview-test")
    );
    let local_verify = argument.as_deref() == Some("--f8-local-webview-test");
    let verify = local_verify || argument.as_deref() == Some("--f8-webview-test");
    match std::env::args().nth(1).as_deref() {
        Some("--f8-local-process-test") => {
            let mut runtime = local_runtime::Host::default();
            let result = (|| -> Result<(), String> {
                if runtime.start()? != "READY" {
                    return Err("NOT_READY".into());
                }
                println!(
                    "F8_LOCAL_DIAGNOSTICS {}",
                    runtime.request(serde_json::json!({"action":"diagnostics"}))?
                );
                runtime.close()?;
                if !runtime.clean_exit() {
                    return Err("UNCLEAN_RUNTIME_EXIT".into());
                }
                Ok(())
            })();
            if let Err(error) = result {
                let _ = runtime.close();
                eprintln!("F8_LOCAL_PROCESS_FAILED:{error}");
                std::process::exit(1);
            }
            return;
        }
        Some("--f8-self-test") => {
            contract_self_test();
            if let Err(error) = lifecycle::self_test() {
                eprintln!("{error}");
                std::process::exit(1);
            }
            return;
        }
        Some("--f8-webview-test" | "--f8-local-dev" | "--f8-local-webview-test") => {}
        Some(_) => {
            eprintln!("unsupported argument");
            std::process::exit(2);
        }
        None => {}
    }
    tauri::Builder::default()
        .manage(Arc::new(Mutex::new(if local_dev {Host::Local(local_runtime::Host::default())}else{Host::Synthetic(lifecycle::Host::default())})))
        .manage(LoginSequence(std::sync::atomic::AtomicUsize::new(0)))
        .manage(LoginWindows(Mutex::new(None)))
        .manage(VerificationMode(std::sync::atomic::AtomicBool::new(verify)))
        .setup(move |app| {
            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                .title(if local_dev {"F8 本机 Keycloak + 隔离 Runtime"} else {"F8 本机原生验证 · 合成模式"}).incognito(true).inner_size(900.0, 700.0)
                .initialization_script(format!("window.__F8_VERIFY__={verify};window.__F8_LOCAL_DEV__={local_dev};window.__F8_LOCAL_VERIFY__={local_verify};"))
                .on_navigation(|url| {
                    eprintln!("F8 navigation: scheme={} host={:?} path={:?}", url.scheme(), url.host_str(), url.path());
                    local(url)
                })
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label().starts_with("f8-login-") {
                if matches!(event,tauri::WindowEvent::CloseRequested{..}) {
                    let windows=window.state::<LoginWindows>();
                    let cancel=windows.0.lock().map(|mut current| {
                        if current.as_deref()==Some(window.label()) {*current=None;true}else{false}
                    }).unwrap_or(false);
                    if cancel {
                        let app=window.app_handle().clone();
                        if let Some(main)=app.get_webview_window("main") {let _=main.eval("window.dispatchEvent(new Event('f8-login-cancelled'))");}
                        let host=Arc::clone(window.state::<SharedHost>().inner());
                        tauri::async_runtime::spawn_blocking(move || {
                            if let Ok(mut host)=host.lock() {let _=host.desktop(serde_json::json!({"action":"logout"}));}
                            if let Some(main)=app.get_webview_window("main") {let _=main.eval("window.dispatchEvent(new Event('f8-login-cancel-complete'))");}
                        });
                    }
                }
                return;
            }
            if window.label()!="main" {return;}
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if let Some(webview) = window.app_handle().get_webview_window(window.label()) {
                    let _ = webview.eval("window.dispatchEvent(new Event('f8-close'))");
                }
                let login_closed=close_login(window.app_handle()).is_ok();
                let state = window.state::<SharedHost>();
                let closed = state.lock().map(|mut host| host.close().is_ok()).unwrap_or(false);
                if !closed || !login_closed { api.prevent_close(); }
            }
        })
        .invoke_handler(tauri::generate_handler![host_command, desktop_command, verification_report])
        .build(tauri::generate_context!())
        .expect("native host failed")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                if let Some(main)=app.get_webview_window("main") {let _=main.eval("window.dispatchEvent(new Event('f8-close'))");}
                let login_closed=close_login(app).is_ok();
                let state = app.state::<SharedHost>();
                if !state.lock().map(|mut host| host.close().is_ok()).unwrap_or(false) || !login_closed {
                    api.prevent_exit();
                }
            }
        });
}
