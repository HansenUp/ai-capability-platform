"""Exercise the exact native bootstrap's fail-before-exec contract with public fixtures."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
source = (ROOT / "apps/desktop/native-candidate/src-tauri/src/local_runtime.rs").read_text()
bootstrap = source.split("const BOOTSTRAP:", 1)[1].split('r#"', 1)[1].split('"#;', 1)[0]
directory = Path(tempfile.mkdtemp(prefix="aip-f8-integrity-", dir="/private/tmp"))
cases = []
try:
    script = directory / "fixture.py"
    script.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1], 'executed').write_text('public fixture')\n")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()

    def run(expected):
        return subprocess.run(  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
            [str(ROOT / ".venv/bin/python"), "-I", "-c", bootstrap, str(script), expected, str(directory)],
            env={"PATH": "/usr/bin:/bin", "HOME": str(directory)},
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert run(digest).returncode == 0 and (directory / "executed").exists()
    cases.append("reviewed entry executes in verified interpreter process")
    (directory / "executed").unlink()
    script.write_text(script.read_text() + "\n# changed after review\n")
    rejected = run(digest)
    assert rejected.returncode != 0 and "RUNTIME_ENTRY_CHANGED" in rejected.stderr and not (directory / "executed").exists()
    cases.append("changed entry rejected before any entry code executes")
    owned = Path(tempfile.mkdtemp(prefix="aip-f8-runtime-", dir="/private/tmp"))
    owned.chmod(0o700)
    relative = "services/runtime/src/runtime/main.py"
    (owned / "sources.json").write_text(json.dumps({relative: "0" * 64}))
    rejected = subprocess.run(  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
        [str(ROOT / ".venv/bin/python"), str(Path(__file__).parent / "sidecar.py"), str(owned)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(owned)},
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert rejected.returncode == 1 and not rejected.stdout and not owned.exists()
    cases.append("changed Runtime dependency manifest fails before startup and cleans owned directory")
finally:
    shutil.rmtree(directory)
report = {
    "status": "PASS",
    "bootstrap_sha256": hashlib.sha256(bootstrap.encode()).hexdigest(),
    "cases": cases,
    "cleanup_verified": not directory.exists(),
}
(ROOT / "apps/desktop/native-candidate/g2-b-preflight/local-integrity-result.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
