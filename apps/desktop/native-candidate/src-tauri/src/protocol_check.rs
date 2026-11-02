mod lifecycle;
fn main() {
    let result = if std::env::args().nth(1).as_deref() == Some("--f8-synthetic-sidecar") {
        lifecycle::sidecar()
    } else {
        lifecycle::self_test()
    };
    if let Err(error) = result { eprintln!("{error}"); std::process::exit(1); }
}
