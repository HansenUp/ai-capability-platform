#![allow(dead_code, unused_imports)]
mod lifecycle;
fn main() {
    if std::env::args().collect::<Vec<_>>().get(1..)
        != Some(&["--f8-synthetic-sidecar".to_string()][..])
        || lifecycle::sidecar().is_err() { std::process::exit(1); }
}
