"""Real native local-development chain; Keycloak must be the owned fixture instance."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "apps/desktop/native-candidate/g2-b-preflight"
package = json.loads((EVIDENCE / "local-package-result.json").read_text())
binary = Path(package["app"]) / "Contents/MacOS/ai-platform-desktop-candidate"
assert hashlib.sha256(binary.read_bytes()).hexdigest() == package["packaged_binary_sha256"]
process_test = subprocess.run([str(binary), "--f8-local-process-test"], capture_output=True, text=True, timeout=30)  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
assert process_test.returncode == 0, process_test.stderr
proc = subprocess.Popen(  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
    [str(binary), "--f8-local-webview-test"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
)
report = {
    "checked_utc": datetime.now(UTC).isoformat(),
    "binary_sha256": package["packaged_binary_sha256"],
    "scope": "real-native-keycloak-pkce-runtime-jwt-private-sqlite",
    "process_test": {"exit": process_test.returncode, "stdout": process_test.stdout, "stderr": process_test.stderr},
}
try:
    stdout, stderr = proc.communicate(timeout=80)
except subprocess.TimeoutExpired:
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=10)
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
(EVIDENCE / "local-native-webview-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "PASS" else 1)
