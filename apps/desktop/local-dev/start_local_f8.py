"""One command, owned Keycloak + native local-development app, deterministic cleanup."""

import argparse
import hashlib
import json
import signal
import subprocess
from pathlib import Path

from identity import ROOT, LocalIdentity
from verify_backend import verify


def main():
    parser = argparse.ArgumentParser(description="F8 本机 Keycloak + 隔离 Runtime；不接入生产或真实模型。")
    parser.add_argument("--verify", action="store_true", help="执行真实后端及原生WebView验收后退出")
    args = parser.parse_args()
    package = json.loads((ROOT / "apps/desktop/native-candidate/g2-b-preflight/local-package-result.json").read_text())
    binary = Path(package["app"]) / "Contents/MacOS/ai-platform-desktop-candidate"
    if not binary.is_file():
        raise RuntimeError("先构建并本地打包F8开发产物")
    expected = ROOT / "apps/desktop/native-candidate/artifacts/AI Platform F8.app"
    if Path(package["app"]) != expected or hashlib.sha256(binary.read_bytes()).hexdigest() != package["packaged_binary_sha256"]:
        raise RuntimeError("本机应用路径或摘要与已验收包不一致，拒绝启动")
    process = None

    def stop(*_):
        if process and process.poll() is None:
            process.terminate()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with LocalIdentity(verify=args.verify):
        print("F8_LOCAL_READY: 本机Keycloak已就绪；仅临时合成账号/数据", flush=True)
        try:
            if args.verify:
                verify()
                result = subprocess.run([str(ROOT / ".venv/bin/python"), str(Path(__file__).parent / "verify_native_local.py")], check=True)  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
                return result.returncode
            process = subprocess.Popen([str(binary), "--f8-local-dev"])  # noqa: S603 -- fixed reviewed tool/app paths and argument arrays, no shell or UI-supplied executable
            return process.wait()
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
