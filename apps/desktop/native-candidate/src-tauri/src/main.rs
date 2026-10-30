// Uncompiled candidate. No process/network/filesystem bridge is registered.
#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum Action { Start, Stop, Status, Close, Reconcile }

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Message { action: Action }

#[tauri::command]
fn host_command(window: tauri::WebviewWindow, message: Message) -> Result<String, String> {
    if window.label() != "main" { return Err("CALLER_REJECTED".into()); }
    let _ = message.action;
    // Native origin/session proof and owned-process adapter are intentionally absent.
    // Even a locally allowed caller cannot exercise lifecycle commands in this scaffold.
    Err("NATIVE_ADAPTER_NOT_ENABLED".into())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![host_command])
        .run(tauri::generate_context!())
        .expect("candidate host failed");
}
