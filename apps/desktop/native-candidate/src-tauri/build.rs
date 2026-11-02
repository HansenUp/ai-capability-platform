fn main() {
    println!("cargo:rustc-check-cfg=cfg(f8_sidecar)");
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["host_command", "desktop_command", "verification_report"])
        )
    ).expect("candidate capability build failed");
}
