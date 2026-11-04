"""Stage installed macOS Runtime inputs into a relocatable, local-only candidate.

No download, pip install, production configuration or model call is performed.
Only copied Mach-O files are rewritten and ad-hoc signed. The input environment
and existing native application are never modified by this separate prototype.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[3]
FIRST_PARTY = {"ai-runtime": REPO / "services/runtime/src", "ai-contract": REPO / "packages/contract/src"}
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dependency_closure(roots: list[str]) -> dict[str, metadata.Distribution]:
    """Resolve only already-installed distributions, including requested extras."""
    pending: list[tuple[str, frozenset[str]]] = [(name, frozenset()) for name in roots]
    requested: dict[str, set[str]] = {}
    result: dict[str, metadata.Distribution] = {}
    while pending:
        name, extras = pending.pop()
        name = canonicalize_name(name)
        if name in requested and extras <= requested[name]:
            continue
        requested.setdefault(name, set()).update(extras)
        distribution = metadata.distribution(name)
        result[name] = distribution
        for raw in distribution.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in {"", *requested[name]}):
                continue
            installed = metadata.distribution(requirement.name)
            if requirement.specifier and installed.version not in requirement.specifier:
                raise ValueError(f"Installed version does not satisfy {requirement.name}: {installed.version}")
            pending.append((requirement.name, frozenset(requirement.extras)))
    return result


def package_relative(record: str) -> Path | None:
    """Exclude editable hooks, installer scripts, bytecode and external paths."""
    path = Path(record)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    if "__pycache__" in path.parts or path.suffix in {".pyc", ".pth"}:
        return None
    if path.name.startswith("__editable__") or path.name in {"direct_url.json", "RECORD"}:
        return None
    return path


class Staging:
    def __init__(self, destination: Path, *, limit_bytes: int = 1024**3) -> None:
        if destination.exists() or destination.is_symlink():
            raise ValueError("Refuse to overwrite an existing bundle")
        self.root = destination.resolve()
        self.root.mkdir(parents=True, mode=0o700)
        self.limit_bytes = limit_bytes
        self.bytes = 0
        self.sources: dict[str, str] = {}
        self.copied_inputs: dict[str, dict[str, str | int]] = {}

    def copy(self, source: Path, relative: Path) -> None:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe staging path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("Only regular input files may be copied")
        target = self.root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(self.root):
            raise ValueError("Staging destination escaped its owned directory")
        if target.exists():
            if sha256(target) != sha256(source):
                raise ValueError(f"Conflicting package files: {relative}")
            return
        size = source.stat().st_size
        if self.bytes + size > self.limit_bytes:
            raise ValueError("Bundle staging exceeds its 1 GiB budget")
        target.parent.mkdir(parents=True, exist_ok=True)
        before = sha256(source)
        shutil.copyfile(source, target)
        target.chmod(0o755 if os.access(source, os.X_OK) else 0o644)
        if sha256(source) != before or sha256(target) != before:
            raise ValueError(f"Input changed during copy: {relative}")
        self.bytes += size
        self.sources[str(source)] = str(relative)
        self.copied_inputs[str(relative)] = {"source_sha256": before, "bytes": size}


def stage_framework(stage: Staging, base: Path) -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    prefix = Path("Python.framework/Versions") / version
    for relative in [Path("Python"), Path(f"bin/python{version}"), Path("Resources/Python.app/Contents/MacOS/Python")]:
        stage.copy(base / relative, prefix / relative)
    plist = Path("Resources/Python.app/Contents/Info.plist")
    if (base / plist).is_file():
        stage.copy(base / plist, prefix / plist)
    for source in (base / "lib").glob("*.dylib"):
        if source.name.startswith(("libtcl", "libtk")):
            continue
        stage.copy(source, prefix / "lib" / source.name)
    library = base / "lib" / f"python{version}"
    for source in library.rglob("*"):
        relative = source.relative_to(library)
        if any(
            part in {"site-packages", "__pycache__", "test", "tests", "tkinter", "idlelib", "turtledemo", "ensurepip"}
            for part in relative.parts
        ):
            continue
        if source.name.startswith("_tkinter") or source.suffix == ".pyc" or not source.is_file():
            continue
        if not source.resolve().is_relative_to(base):
            raise ValueError("Standard library input escaped the interpreter prefix")
        stage.copy(source, prefix / "lib" / f"python{version}" / relative)
    return prefix / f"bin/python{version}"


def stage_packages(stage: Staging, distributions: dict[str, metadata.Distribution]) -> dict[str, list[Path]]:
    records: dict[str, list[Path]] = {}
    for name, distribution in sorted(distributions.items()):
        copied: list[Path] = []
        site = Path(str(distribution.locate_file("."))).resolve()
        for record in distribution.files or []:
            relative = package_relative(str(record))
            if relative is None:
                continue
            source = Path(str(distribution.locate_file(record))).resolve()
            if not source.is_relative_to(site):
                raise ValueError(f"Distribution file escaped site-packages: {name}")
            stage.copy(source, Path("site-packages") / relative)
            copied.append(relative)
        if name in FIRST_PARTY:
            folder = FIRST_PARTY[name]
            for source in folder.rglob("*"):
                relative = source.relative_to(folder)
                if "__pycache__" in relative.parts or source.suffix == ".pyc" or not source.is_file():
                    continue
                if not source.resolve().is_relative_to(folder):
                    raise ValueError("First-party input escaped its source directory")
                stage.copy(source, Path("site-packages") / relative)
                copied.append(relative)
        records[name] = copied
    return records


def tool(executable: str, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed Apple tools and owned staging paths; no shell or user executable
        [executable, *arguments], check=True, capture_output=True, text=True, timeout=30
    ).stdout


def relocate_native(stage: Staging, source_base: Path) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    for path in sorted(stage.root.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) not in MAGIC:
                continue
        dependencies = {
            line.strip().split(" (", 1)[0] for line in tool("/usr/bin/otool", "-L", str(path)).splitlines() if line.startswith("\t")
        }
        replacements = []
        for dependency in sorted(dependencies):
            if not dependency.startswith("/") or dependency.startswith(("/System/Library/", "/usr/lib/")):
                continue
            original = Path(dependency)
            if original.is_relative_to(source_base):
                relative = Path("Python.framework/Versions") / source_base.name / original.relative_to(source_base)
            elif str(original.resolve()) in stage.sources:
                relative = Path(stage.sources[str(original.resolve())])
            else:
                raise ValueError(f"Unreviewed absolute native dependency: {dependency}")
            target = stage.root / relative
            if not target.is_file():
                raise ValueError(f"Native dependency missing from bundle: {relative}")
            replacement = "@loader_path/" + os.path.relpath(target, path.parent)
            if target == path:
                tool("/usr/bin/install_name_tool", "-id", replacement, str(path))
            else:
                tool("/usr/bin/install_name_tool", "-change", dependency, replacement, str(path))
            replacements.append({"from": dependency, "to": replacement})
        # Absolute interpreter-prefix rpaths must not provide a fallback to the host.
        lines = tool("/usr/bin/otool", "-l", str(path)).splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "cmd LC_RPATH":
                continue
            old = lines[index + 2].strip().removeprefix("path ").split(" (offset", 1)[0]
            if old.startswith(str(source_base)):
                target = stage.root / "Python.framework/Versions" / source_base.name / Path(old).relative_to(source_base)
                new = "@loader_path/" + os.path.relpath(target, path.parent)
                tool("/usr/bin/install_name_tool", "-rpath", old, new, str(path))
                replacements.append({"rpath_from": old, "rpath_to": new})
            elif old.startswith("/") and not old.startswith(("/System/Library/", "/usr/lib/")):
                raise ValueError(f"Unreviewed absolute native rpath: {old}")
        tool("/usr/bin/codesign", "--force", "--sign", "-", str(path))
        tool("/usr/bin/codesign", "--verify", "--strict", str(path))
        changes.append({"path": str(path.relative_to(stage.root)), "rewrites": replacements, "signature": "ad-hoc-local-only"})
    return changes


def write_records(stage: Staging, distributions: dict[str, metadata.Distribution], records: dict[str, list[Path]]) -> None:
    for name, paths in records.items():
        original = distributions[name]
        metadata_files = [p for p in paths if p.name == "METADATA" and p.parent.name.endswith(".dist-info")]
        if len(metadata_files) != 1:
            raise ValueError(f"Missing unique distribution metadata: {original.metadata['Name']}")
        record = metadata_files[0].parent / "RECORD"
        with (stage.root / "site-packages" / record).open("w", newline="") as stream:
            writer = csv.writer(stream)
            for relative in sorted(set(paths)):
                target = stage.root / "site-packages" / relative
                encoded = base64.urlsafe_b64encode(bytes.fromhex(sha256(target))).decode().rstrip("=")
                writer.writerow([str(relative), "sha256=" + encoded, target.stat().st_size])
            writer.writerow([str(record), "", ""])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This local candidate targets macOS arm64 only")
    started = time.monotonic()
    base = Path(sys.base_prefix).resolve()
    stage = Staging(args.output)
    report: dict[str, object] = {"status": "staging", "distribution_ready": False, "no_downloads_or_installs": True}
    try:
        distributions = dependency_closure(["ai-runtime", "ai-contract"])
        python = stage_framework(stage, base)
        records = stage_packages(stage, distributions)
        native = relocate_native(stage, base)
        write_records(stage, distributions, records)
        report.update(
            status="staged-unverified",
            python=str(python),
            architecture=platform.machine(),
            distributions={name: distribution.version for name, distribution in distributions.items()},
            native_files=native,
            input_files=stage.copied_inputs,
            excluded_features=["Tk GUI", "ensurepip/automatic dependency installation", "optional non-mock harness extras"],
            files={str(path.relative_to(stage.root)): sha256(path) for path in stage.root.rglob("*") if path.is_file()},
        )
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report.update(elapsed_seconds=round(time.monotonic() - started, 2), input_bytes=stage.bytes)
        (stage.root / "bundle-manifest.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
