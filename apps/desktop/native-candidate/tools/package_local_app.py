"""Package the verified binary as a local ad-hoc signed macOS app, no installer."""

import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

base = Path(__file__).resolve().parents[1]
(base / "g2-b-preflight").mkdir(exist_ok=True)
artifacts = base / "artifacts"
binary = artifacts / "ai-platform-desktop-candidate"
build = json.loads((base / "g2-a-evidence/g2b-build-result.json").read_text())
assert build["status"] == "BUILD_COMPLETE"
for relative, expected in build["first_party_snapshot_sha256"].items():
    source = base / relative.replace("project/", "src-tauri/", 1)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, f"Source changed since build: {relative}"
local = build["local_dev_inputs"]
assert hashlib.sha256(Path(local["entry"]).read_bytes()).hexdigest() == local["entry_sha256"]
assert hashlib.sha256(Path(local["python_resolved"]).read_bytes()).hexdigest() == local["python_sha256"]
for relative, expected in local["runtime_sources"].items():
    assert hashlib.sha256((Path(local["repo"]) / relative).read_bytes()).hexdigest() == expected, f"Runtime source changed: {relative}"
assert hashlib.sha256(binary.read_bytes()).hexdigest() == build["artifact"]["sha256"]
app = artifacts / "AI Platform F8.app"
assert not app.exists(), "Refuse to overwrite an existing app; preserve evidence first"
contents = app / "Contents"
(contents / "MacOS").mkdir(parents=True)
shutil.copy2(binary, contents / "MacOS/ai-platform-desktop-candidate")
(contents / "Helpers").mkdir()
helper = artifacts / "f8-synthetic-sidecar"
assert hashlib.sha256(helper.read_bytes()).hexdigest() == build["helper_artifact"]["sha256"]
shutil.copy2(helper, contents / "Helpers/f8-synthetic-sidecar")
(contents / "Info.plist").write_bytes(
    plistlib.dumps(
        {
            "CFBundleExecutable": "ai-platform-desktop-candidate",
            "CFBundleIdentifier": "local.ai-platform.isolated-candidate",
            "CFBundleName": "AI Platform F8",
            "CFBundleDisplayName": "AI Platform F8",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
            "CFBundleShortVersionString": "0.0.0",
            "LSMinimumSystemVersion": "10.15",
            "NSHighResolutionCapable": True,
        }
    )
)
subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
report = {
    "app": str(app),
    "signature": "ad-hoc-local-only",
    "notarized": False,
    "distribution_ready": False,
    "source_binary_sha256": build["artifact"]["sha256"],
    "packaged_binary_sha256": hashlib.sha256((contents / "MacOS/ai-platform-desktop-candidate").read_bytes()).hexdigest(),
}
(base / "g2-b-preflight/local-package-result.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
