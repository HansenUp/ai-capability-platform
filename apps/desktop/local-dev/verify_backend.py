"""Actual Keycloak + Runtime integration, invoked only against our owned local IdP."""

import hashlib
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "apps/desktop/native-candidate/g2-b-preflight"


def source_manifest():
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in [ROOT / "services/runtime/src", ROOT / "packages/contract/src"]
        for p in folder.rglob("*.py")
    }


def verify():
    root = Path(tempfile.mkdtemp(prefix="aip-f8-runtime-", dir="/private/tmp"))
    root.chmod(0o700)
    (root / "sources.json").write_text(json.dumps(source_manifest()))
    errors = EVIDENCE / "local-runtime-stderr.log"
    outcomes = []
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root)}
    with errors.open("w") as log:
        proc = subprocess.Popen(  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
            [str(ROOT / ".venv/bin/python"), str(Path(__file__).parent / "sidecar.py"), str(root)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
        )
    replies = queue.Queue()

    def read():
        for line in proc.stdout:
            replies.put(line)
        replies.put(None)

    threading.Thread(target=read, daemon=True).start()

    def send(value):
        proc.stdin.write(json.dumps(value) + "\n")
        proc.stdin.flush()
        line = replies.get(timeout=15)
        if not line:
            raise RuntimeError("Sidecar stopped: " + (errors.read_text() if errors.exists() else ""))
        return json.loads(line)

    def command(action, **data):
        result = send({"action": action, **data})
        assert result["ok"], result.get("error")
        return result.get("value")

    def login(username, *, reject=False, cancel=False, nonce=False):
        url = command("login")["url"]
        with httpx.Client(trust_env=False) as client:
            assert client.get("http://127.0.0.1:18765/callback?state=wrong&code=x").status_code == 400
        if reject or nonce:
            parts = urlsplit(url)
            q = parse_qs(parts.query)
            q["nonce" if nonce else "code_challenge"] = ["a" * 43]
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({k: v[0] for k, v in q.items()}), ""))
        configuration = root / "browser.json"
        configuration.write_text(json.dumps({"url": url, "username": username, "reject": reject or cancel or nonce}))
        configuration.chmod(0o600)
        if cancel:
            command("logout")
        result = subprocess.run(  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
            [shutil.which("node"), str(Path(__file__).parent / "oidc-login.mjs"), str(configuration)],
            env={**env, "PLAYWRIGHT_MODULE": "/Users/mahansen/.npm/_npx/a80a913f4f8f2557/node_modules/playwright/index.mjs"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr[-1500:]
        return command("auth_status")

    report = {"scope": "real-keycloak-runtime-jwt-private-sqlite", "cases": outcomes}
    try:
        challenge = "F8/1 " + "a" * 64 + "\n"
        proc.stdin.write(challenge)
        proc.stdin.flush()
        handshake = replies.get(timeout=20)
        assert handshake == challenge, "Startup failed: " + (errors.read_text() if errors.exists() else "")
        diagnostics = command("diagnostics")
        assert diagnostics["unauthenticated_status"] == diagnostics["forged_headers_status"] == 401
        assert diagnostics["worker_started"] is False
        outcomes.append("real Runtime rejects anonymous and forged development identity headers; no worker")
        assert not send({"action": "read", "token": "forged"})["ok"]
        assert not send({"action": "exec"})["ok"]
        outcomes.append("pipe rejects credential/path/command injection")
        assert login("f8-alice", reject=True) is None
        outcomes.append("real Keycloak wrong PKCE and callback state rejected")
        assert login("f8-alice", nonce=True) is None
        outcomes.append("valid access token with mismatched ID-token nonce is rejected")
        assert login("f8-alice", cancel=True) is None
        outcomes.append("logout cancels pending OAuth and late callback cannot restore identity")
        alice = login("f8-alice")
        assert alice["tenant"] == "default"
        assert "1 条" in command("read")["text"]
        outcomes.append("real Keycloak access/id tokens verified by actual Runtime; Alice owns one row")
        command("set_online", online=False)
        assert not send({"action": "read"})["ok"]
        assert command("auth_status")["subject"] == alice["subject"]
        command("set_online", online=True)
        outcomes.append("offline blocks fresh Runtime reads")
        bob = login("f8-bob")
        assert bob["tenant"] == "t2" and bob["subject"] != alice["subject"]
        assert "2 条" in command("read")["text"]
        outcomes.append("Bob different tenant/subject reads only two owned rows")
        deadline = time.monotonic() + 20
        while command("auth_status") is not None:
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert not send({"action": "read"})["ok"]
        outcomes.append("actual Keycloak token expiry clears authorization and rejects read")
        command("logout")
        assert command("auth_status") is None
        proc.stdin.close()
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not root.exists()
        outcomes.append("parent pipe EOF stops callback/Runtime, disposes SQLite and removes owned directory")
        report["status"] = "PASS"
    finally:
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=5)
        report["exit"] = proc.returncode
        report["owned_directory_removed"] = not root.exists()
        (EVIDENCE / "local-backend-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    verify()
