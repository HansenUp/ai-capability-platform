"""Offline bounded extraction and static inventory; never executes package code."""

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

base = Path(__file__).resolve().parents[1]
for output_name in ["g2-a-evidence", "g2-b-preflight"]:
    (base / output_name).mkdir(exist_ok=True)
cache = Path.home() / "Library/Caches/AIPlatform/f8-native-g1/000ff71a45fe"
start = time.monotonic()
owner = Path(tempfile.mkdtemp(prefix="aip-f8-g2-apfs-", dir="/private/tmp"))
os.chmod(owner, 0o700)
root = owner / "mount"
root.mkdir()
image = owner / "audit.dmg"
mounted = False
attached_device = None
report = {
    "started_utc": datetime.now(UTC).isoformat(),
    "temporary_root": str(root),
    "packages": [],
    "status": "RUNNING",
    "bytes": 0,
    "members": 0,
}


# Reserve a full filesystem allocation block per member plus metadata overhead.
def guard():
    if image.exists() and image.stat().st_blocks * 512 + 16 * 1024**2 > 8 * 1024**3:
        raise ValueError("8 GiB allocated image limit")
    if time.monotonic() - start > 1800:
        raise ValueError("30 minute limit")
    if report["bytes"] > 1024**3:
        raise ValueError("1 GiB extraction limit")
    if report["bytes"] + report["members"] * 32768 + 16 * 1024**2 > 8 * 1024**3:
        raise ValueError("8 GiB conservative workspace allocation limit")


def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            guard()
            h.update(b)
    return h.hexdigest()


try:
    subprocess.run(  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
        ["/usr/bin/hdiutil", "create", "-size", "7g", "-type", "UDIF", "-fs", "Case-sensitive APFS", "-volname", "F8G2Audit", str(image)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    mounted = True  # Treat attach failure/timeout as uncertain until detach is confirmed.
    a = subprocess.run(  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
        ["/usr/bin/hdiutil", "attach", "-plist", "-nobrowse", "-mountpoint", str(root), str(image)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    mounted = True
    entities = plistlib.loads(a.stdout)["system-entities"]
    attached_device = next(x["dev-entry"] for x in entities if x.get("mount-point") == str(root))
    report["attached_device"] = attached_device
    report["owner_directory"] = str(owner)
    (root / "CaseProbe").write_text("A")
    (root / "caseprobe").write_text("B")
    assert (root / "CaseProbe").read_text() == "A" and (root / "caseprobe").read_text() == "B"
    assert (root / "CaseProbe").stat().st_ino != (root / "caseprobe").stat().st_ino
    (root / "CaseProbe").unlink()
    (root / "caseprobe").unlink()
    report["case_sensitive_probe"] = "PASS"
    inv = base / "build-inputs/packages.json"
    lock = base / "src-tauri/Cargo.lock"
    assert digest(inv) == "2387bbad398564ed9877c74942bdf9bd3ae3974ff43c3ba94289197d9fa3e29d"
    assert digest(lock) == "5dd2203b3c9d5f40c85c28c5d473312737083559eb56b3612cdfea66288586ca"
    items = json.loads(inv.read_text())
    sizes = {(x["name"], x["version"]): x for x in items}
    for item in items:
        guard()
        name = item["name"]
        version = item["version"]
        top = f"{name}-{version}"
        archive = cache / (top + ".crate")
        assert not archive.is_symlink() and archive.stat().st_size == sizes[name, version]["bytes"]
        assert digest(archive) == item["sha256"]
        seen = {}
        exact = set()
        count = 0
        amount = 0
        hashes = {}
        with tarfile.open(archive, "r|gz") as tf:
            for m in tf:
                guard()
                q = PurePosixPath(m.name)
                parts = q.parts
                if q.is_absolute() or ".." in parts or not parts or parts[0] != top or "\\" in m.name:
                    raise ValueError(f"unsafe path {top}")
                key = str(q).casefold()
                if str(q) in exact:
                    raise ValueError("exact duplicate member")
                if key in seen:
                    allowed = {top + "/README.md", top + "/Readme.md"}
                    if (
                        top != "static_vcruntime-3.0.0"
                        or {seen[key], str(q)} != allowed
                        or item["sha256"] != "ff7589a1859b09232522bb9edf076ca7fcf14ded96685d3fa629db73023ffbb7"
                    ):
                        raise ValueError(f"duplicate normalized member {top}: {m.name}")
                seen[key] = str(q)
                exact.add(str(q))
                count += 1
                report["members"] += 1
                if count > 20000 or report["members"] > 100000:
                    raise ValueError("member limit")
                if not (m.isdir() or m.isfile()):
                    raise ValueError(f"unsupported member type {top}: {m.name}")
                if m.size < 0 or m.size > 64 * 1024**2:
                    raise ValueError("single member limit")
                amount += m.size
                if amount > 128 * 1024**2:
                    raise ValueError(f"128 MiB package limit: {top}")
                report["bytes"] += m.size
                guard()
                dest = root.joinpath(*parts)
                if m.isdir():
                    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                h = hashlib.sha256()
                copied = 0
                with tf.extractfile(m) as src, dest.open("xb") as out:
                    os.chmod(dest, 0o600)
                    while b := src.read(65536):
                        guard()
                        copied += len(b)
                        if copied > m.size:
                            raise ValueError("member length overflow")
                        out.write(b)
                        h.update(b)
                if copied != m.size:
                    raise ValueError("member truncated")
                hashes[str(q.relative_to(top))] = h.hexdigest()
                if top == "static_vcruntime-3.0.0" and q.name in ("README.md", "Readme.md"):
                    assert h.hexdigest() == "45b9cf6dfd289cf7da6aa64fdc7a152c52cd779af7fa9fb0a1a5d5469bbaa9c2" and copied == 2498
        manifest = tomllib.loads((root / top / "Cargo.toml").read_text())
        pkg = manifest["package"]
        lib = manifest.get("lib", {})
        build = pkg.get("build", "build.rs" if (root / top / "build.rs").exists() else False)
        entries = []
        # Static indicators only; intentionally not a safety verdict.
        for f in (root / top).rglob("*"):
            if not f.is_file() or f.suffix not in (".rs", ".sh", ".py", ".toml") or f.stat().st_size > 4 * 1024**2:
                continue
            text = f.read_text(errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"Command::new|std::process|TcpStream|https?://|OUT_DIR|env::var|cargo:rustc-link|include!|proc_macro", line):
                    if len(entries) < 120:
                        entries.append({"file": str(f.relative_to(root / top)), "line": number, "text": line.strip()[:240]})
        report["packages"].append(
            {
                "name": name,
                "version": version,
                "bytes": amount,
                "members": count,
                "build": build,
                "proc_macro": lib.get("proc-macro", False),
                "build_dependencies": manifest.get("build-dependencies", {}),
                "target_build_dependencies": {
                    k: v.get("build-dependencies", {}) for k, v in manifest.get("target", {}).items() if v.get("build-dependencies")
                },
                "compile_source": {
                    str(f.relative_to(root / top)): f.read_text(errors="replace")
                    for f in (root / top).rglob("*.rs")
                    if f.stat().st_size < 1024 * 1024 and (f.name == "build.rs" or (lib.get("proc-macro", False) and f.name == "lib.rs"))
                },
                "file_sha256": hashes,
                "static_indicators_first_120": entries,
            }
        )
        if len(report["packages"]) % 50 == 0:
            print("audited", len(report["packages"]), flush=True)

    vendor = root / "vendor"
    vendor.mkdir()
    for item in items:
        shutil.move(str(root / (item["name"] + "-" + item["version"])), str(vendor / (item["name"] + "-" + item["version"])))
    project = root / "project"
    shutil.copytree(base / "src-tauri", project)
    shutil.copytree(base / "ui", root / "ui")
    repo = base.parents[2]
    local_source = repo / "apps/desktop/local-dev/sidecar.py"
    native_source = subprocess.run(["/bin/cat", str(local_source)], capture_output=True, check=True, timeout=10).stdout  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    if native_source != local_source.read_bytes():
        raise ValueError("local Runtime source byte views differ")
    local_folder = project / "local-dev"
    runtime_sources = {
        str(p.relative_to(repo)): digest(p)
        for folder in [repo / "services/runtime/src", repo / "packages/contract/src"]
        for p in folder.rglob("*.py")
    }
    python_path = (repo / ".venv/bin/python").resolve()
    report["local_dev_inputs"] = {
        "repo": str(repo),
        "entry": str(local_source),
        "entry_sha256": digest(local_source),
        "python_resolved": str(python_path),
        "python_sha256": digest(python_path),
        "runtime_sources": runtime_sources,
    }
    report["first_party_snapshot_sha256"] = {
        str(p.relative_to(root)): digest(p)
        for folder in [project, root / "ui"]
        for p in folder.rglob("*")
        if p.is_file() and not p.is_relative_to(local_folder)
    }
    cargo_home = root / "cargo-home"
    cargo_home.mkdir()
    for item, entry in zip(items, report["packages"], strict=True):
        (vendor / (item["name"] + "-" + item["version"]) / ".cargo-checksum.json").write_text(
            json.dumps({"package": item["sha256"], "files": entry["file_sha256"]})
        )
    (cargo_home / "config.toml").write_text(
        '[source.crates-io]\nreplace-with = "local-cache"\n[source.local-cache]\ndirectory = ' + json.dumps(str(vendor)) + "\n"
    )
    (root / "tmp").mkdir()
    tool = Path.home() / ".rustup/toolchains/stable-aarch64-apple-darwin/bin"
    env = {
        "HOME": str(cargo_home),
        "CARGO_HOME": str(cargo_home),
        "PATH": str(tool) + ":/usr/bin:/bin",
        "RUSTC": str(tool / "rustc"),
        "CARGO_NET_OFFLINE": "true",
        "TMPDIR": str(root / "tmp"),
        "F8_DEV_REPO": str(repo),
        "F8_DEV_PYTHON_RESOLVED": str(python_path),
        "F8_DEV_PYTHON_SHA256": digest(python_path),
        "F8_DEV_ENTRY_SHA256": digest(local_source),
        "F8_DEV_SOURCES_JSON": json.dumps(runtime_sources, sort_keys=True),
    }
    cmd = [
        str(tool / "cargo"),
        "metadata",
        "--offline",
        "--locked",
        "--format-version",
        "1",
        "--filter-platform",
        "aarch64-apple-darwin",
        "--manifest-path",
        str(project / "Cargo.toml"),
    ]
    result = subprocess.run(cmd, env=env, cwd=project, capture_output=True, timeout=120)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
    report["metadata_command"] = cmd
    report["metadata_exit"] = result.returncode
    report["metadata_stderr"] = result.stderr.decode(errors="replace")
    if result.returncode:
        raise ValueError("offline metadata failed")
    metadata = json.loads(result.stdout)
    (base / "g2-a-evidence/g2b-build-metadata.json").write_text(json.dumps(metadata, indent=2))
    report["metadata_nodes"] = len(metadata["resolve"]["nodes"])
    from g2b_build_stage import run_stage

    run_stage(base=base, project=project, vendor=vendor, root=root, tool=tool, cargo_home=cargo_home, env=env, report=report, image=image)
    report["status"] = "BUILD_COMPLETE" if report.get("build_exit") == 0 else "BUILD_FAILED"
except Exception as e:
    report["status"] = "PAUSED_EXCEPTION"
    report["error"] = str(e)
    if isinstance(e, subprocess.CalledProcessError):
        report["command_exit_code"] = e.returncode
        report["command_stderr"] = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr
    print(report["error"], flush=True)
finally:
    report["allocated_image_bytes"] = image.stat().st_blocks * 512 if image.exists() else 0
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    if mounted:
        try:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(root)], check=True, capture_output=True, timeout=60)  # noqa: S603 -- reviewed fixed tools/owned paths, bounded argument arrays, no shell
            mounted = False
        except Exception as e:
            report["cleanup_error"] = str(e)
            report["status"] = "PAUSED_CLEANUP_UNKNOWN"
    if not mounted:
        shutil.rmtree(owner)
        report["cleanup_verified"] = not owner.exists()
    else:
        report["cleanup_verified"] = False
    (base / "g2-a-evidence/g2b-build-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "packages"}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report["status"] == "BUILD_COMPLETE" else 1)
