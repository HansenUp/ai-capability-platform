"""Compile first-party std-only pipe supervisor; exercise real owned processes."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

base = Path(__file__).resolve().parents[1]
(base / "g2-b-preflight").mkdir(exist_ok=True)
root = Path(tempfile.mkdtemp(prefix="aip-f8-protocol-", dir="/private/tmp"))
report = {"checked_utc": datetime.now(UTC).isoformat(), "cases": []}
try:
    exe = root / "protocol-check"
    rustc = Path.home() / ".rustup/toolchains/stable-aarch64-apple-darwin/bin/rustc"
    sources = [base / "src-tauri/src" / name for name in ("lifecycle.rs", "protocol_check.rs", "sidecar.rs")]
    report["source_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    helper = root / "f8-synthetic-sidecar"
    subprocess.run([str(rustc), "--edition=2021", "--cfg", "f8_sidecar", str(sources[2]), "-o", str(helper)], check=True, timeout=60)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    subprocess.run(  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
        [str(rustc), "--edition=2021", str(sources[1]), "-o", str(exe)],
        env={**os.environ, "F8_SIDECAR_BINARY": str(helper)},
        check=True,
        timeout=60,
    )
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    report["supervisor_result"] = result.stdout.strip()
    for name, data, code in [
        ("missing_handshake", b"", 1),
        ("wrong_protocol", b"F9/1 " + b"a" * 64 + b"\n", 1),
        ("wrong_challenge_type", b"F8/1 " + b"z" * 64 + b"\n", 1),
        ("extra_fields", b"F8/1 " + b"a" * 64 + b" extra\n", 1),
        ("oversized", b"x" * 256, 1),
        ("parent_pipe_eof", b"F8/1 " + b"a" * 64 + b"\n", 0),
    ]:
        result = subprocess.run([str(exe), "--f8-synthetic-sidecar"], input=data, capture_output=True, timeout=5)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
        assert result.returncode == code, (name, result.returncode)
        assert result.stdout == (data if code == 0 else b""), name
        report["cases"].append({"name": name, "exit": result.returncode, "pass": True})
    helper.write_bytes(helper.read_bytes() + b"tamper")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=5)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    assert result.returncode == 1 and "SIDECAR_INTEGRITY_FAILED" in result.stderr
    report["cases"].append({"name": "tampered_helper_rejected_before_spawn", "exit": result.returncode, "pass": True})
    report["status"] = "PASS"
finally:
    shutil.rmtree(root)
    report["cleanup_verified"] = not root.exists()
    (base / "g2-b-preflight/native-protocol-result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
