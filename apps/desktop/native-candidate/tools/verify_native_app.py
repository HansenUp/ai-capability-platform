"""Run actual packaged Tauri/WebView assertions and retain exact binary identity."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

base = Path(__file__).resolve().parents[1]
(base / "g2-b-preflight").mkdir(exist_ok=True)
package = json.loads((base / "g2-b-preflight/local-package-result.json").read_text())
app = Path(package["app"])
binary = app / "Contents/MacOS/ai-platform-desktop-candidate"
assert hashlib.sha256(binary.read_bytes()).hexdigest() == package["packaged_binary_sha256"]
report = {
    "checked_utc": datetime.now(UTC).isoformat(),
    "binary_sha256": package["packaged_binary_sha256"],
    "scope": "actual-tauri-webview-and-native-ipc",
}
result = subprocess.run([str(binary), "--f8-self-test"], capture_output=True, text=True, timeout=30)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
report["process_self_test"] = {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
assert result.returncode == 0
proc = subprocess.Popen(  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    [str(binary), "--f8-webview-test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
)
try:
    stdout, stderr = proc.communicate(timeout=45)
except subprocess.TimeoutExpired:
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
    report["timeout"] = True
report.update(exit=proc.returncode, stdout=stdout, stderr=stderr)
lines = [line.removeprefix("F8_WEBVIEW_RESULT ") for line in stdout.splitlines() if line.startswith("F8_WEBVIEW_RESULT ")]
if lines:
    report["webview"] = json.loads(lines[-1])
report["status"] = (
    "PASS" if proc.returncode == 0 and report.get("webview", {}).get("passed") and report["webview"].get("closed") else "FAIL"
)
(base / "g2-b-preflight/native-webview-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "PASS" else 1)
