fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["host_command"])
        )
    ).expect("candidate capability build failed");
}
