"""Input/path guards only. Does not retry the paused real interpreter staging."""

import importlib.util
from pathlib import Path

import pytest

source = Path(__file__).resolve().parents[2] / "apps/desktop/runtime-bundle/build_local_bundle.py"
spec = importlib.util.spec_from_file_location("local_bundle_input_guards", source)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize(
    "path",
    ["../../bin/python", "/private/tmp/unrelated", "__editable___runtime.pth", "pkg/__pycache__/x.pyc", "pkg-1.dist-info/direct_url.json"],
)
def test_external_and_editable_paths_are_not_staged(path):
    assert builder.package_relative(path) is None


def test_staging_cannot_escape_through_path_or_symlink(tmp_path):
    stage = builder.Staging(tmp_path / "owned")
    fixture = tmp_path / "input.bin"
    fixture.write_bytes(b"public fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (stage.root / "link").symlink_to(outside)
    for name in [Path("../outside/escape.bin"), Path("link/escape.bin")]:
        with pytest.raises(ValueError):
            stage.copy(fixture, name)
    assert not (outside / "escape.bin").exists()


def test_staging_budget_and_existing_output_fail_before_overwrite(tmp_path):
    stage = builder.Staging(tmp_path / "owned", limit_bytes=3)
    fixture = tmp_path / "input.bin"
    fixture.write_bytes(b"larger than budget")
    with pytest.raises(ValueError, match="budget"):
        stage.copy(fixture, Path("payload.bin"))
    assert not (stage.root / "payload.bin").exists()
    with pytest.raises(ValueError, match="overwrite"):
        builder.Staging(stage.root)


def test_native_dependency_closure_never_downloads_missing_packages(monkeypatch):
    def missing(name):
        raise builder.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(builder.metadata, "distribution", missing)
    with pytest.raises(builder.metadata.PackageNotFoundError):
        builder.dependency_closure(["public-missing-fixture"])
