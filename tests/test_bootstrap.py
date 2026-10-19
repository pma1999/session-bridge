from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_bridge import bootstrap
from session_bridge.errors import BridgeError


def test_pinned_pip_identity_is_fixed_and_bounded():
    assert bootstrap.PIP_VERSION == "26.2.1"
    assert bootstrap.PIP_WHEEL_NAME == "pip-26.2.1-py3-none-any.whl"
    assert bootstrap.PIP_WHEEL_SHA256 == "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e"
    assert bootstrap.PIP_WHEEL_SIZE == 1_816_632
    assert bootstrap.PIP_WHEEL_URL.startswith("https://files.pythonhosted.org/")


def test_requirements_lock_is_readable_from_wheel_zipimport(monkeypatch, tmp_path):
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("session_bridge/resources/requirements.lock", "typer==0.27.3\n")
    with zipfile.ZipFile(wheel) as archive:
        root = zipfile.Path(archive)
        monkeypatch.setattr(bootstrap.importlib.resources, "files", lambda _: root / "session_bridge")
        assert bootstrap._requirements_lock() == b"typer==0.27.3\n"


def test_prepare_venv_bootstraps_pip_in_target_python_only(monkeypatch, tmp_path):
    venv_dir = tmp_path / "venv"
    target_python = venv_dir / "bin" / "python"
    calls = []

    class FakeEnvBuilder:
        def __init__(self, *, with_pip):
            assert with_pip is False

        def create(self, path):
            (Path(path) / "bin").mkdir(parents=True)
            target_python.touch()

    pip_wheel = tmp_path / "cache" / bootstrap.PIP_WHEEL_NAME
    pip_wheel.parent.mkdir()
    pip_wheel.write_bytes(b"verified pip wheel")
    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(bootstrap, "_pip_wheel", lambda cache: pip_wheel)
    monkeypatch.setattr(bootstrap, "_has_pip", lambda python: False)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    assert bootstrap.prepare_venv(venv_dir) == target_python
    assert all(call[0][0] == str(target_python) for call in calls)
    assert calls[0][1]["shell"] is False
    assert str(pip_wheel) in calls[1][0]


def test_cached_pip_wheel_must_match_size_and_hash(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / bootstrap.PIP_WHEEL_NAME
    cached.write_bytes(b"tampered")
    with pytest.raises(BridgeError) as error:
        bootstrap._pip_wheel(cache)
    assert error.value.code == "bootstrap_failed"


def test_download_is_https_fixed_url_bounded_and_exclusive(monkeypatch, tmp_path):
    payload = b"x" * bootstrap.PIP_WHEEL_SIZE

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    requested = []

    def open_url(request, timeout):
        requested.append((request.full_url, timeout))
        return Response(payload)

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(bootstrap, "PIP_WHEEL_SHA256", hashlib.sha256(payload).hexdigest())
    path = bootstrap._pip_wheel(tmp_path / "cache")
    assert path.read_bytes() == payload
    assert requested == [(bootstrap.PIP_WHEEL_URL, bootstrap.DOWNLOAD_TIMEOUT)]
    assert not list(path.parent.glob("*.tmp"))


def test_prepare_venv_reports_stage_without_leaking_subprocess_output(monkeypatch, tmp_path):
    class FakeEnvBuilder:
        def __init__(self, *, with_pip):
            pass

        def create(self, path):
            (Path(path) / "bin").mkdir(parents=True)
            (Path(path) / "bin" / "python").touch()

    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", FakeEnvBuilder)
    monkeypatch.setattr(bootstrap, "_pip_wheel", lambda cache: tmp_path / "pip.whl")
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0 if bootstrap._CHECK_VENV in a[0] else 2,
            stdout=b"secret output",
            stderr=b"https://user:pass@example.invalid",
        ),
    )
    with pytest.raises(BridgeError) as error:
        bootstrap.prepare_venv(tmp_path / "venv")
    assert error.value.code == "bootstrap_failed"
    assert error.value.details == {"stage": "pip_bootstrap"}
    assert "secret" not in str(error.value)


def test_install_wsl_uses_locked_requirements_and_owned_launcher(monkeypatch, tmp_path):
    root = tmp_path / ".local" / "share" / "session-bridge"
    venv_dir = root / "venv"
    python = venv_dir / "bin" / "python"
    launcher_dir = tmp_path / ".local" / "bin"
    launcher_dir.mkdir(parents=True)
    wheel = tmp_path / "session_bridge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    requirements = tmp_path / "requirements.lock"
    requirements.write_text("typer==1.2.3\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "prepare_venv", lambda path: python)
    monkeypatch.setattr(bootstrap, "_requirements_lock", lambda: requirements.read_bytes())
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    monkeypatch.setattr(bootstrap, "_profile_version", lambda tool, home: None)
    monkeypatch.setattr(bootstrap, "_install_launcher", lambda launcher, target: "added")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    result = bootstrap.install_wsl(str(wheel), "Ubuntu")
    assert [call[0][0] for call in calls] == [str(python), str(python)]
    assert "--requirement" in calls[0][0] and "--no-deps" in calls[1][0]
    assert calls[0][1]["shell"] is False
    assert result["path_status"] == "added"
    assert result["helper"] == str(python)
    assert result["on_path"] == (str(launcher_dir) in os.environ.get("PATH", "").split(os.pathsep))


def test_install_wsl_never_overwrites_unmanaged_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / ".local" / "bin" / "sessionbridge"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("user-owned", encoding="utf-8")
    wheel = tmp_path / "session_bridge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    python = tmp_path / ".local/share/session-bridge/venv/bin/python"

    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "prepare_venv", lambda path: python)
    (tmp_path / "requirements.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_requirements_lock", lambda: b"locked\n")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    monkeypatch.setattr(bootstrap, "_profile_version", lambda tool, home: None)
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    )
    result = bootstrap.install_wsl(str(wheel), "Ubuntu")
    assert result["path_status"] == "conflict"
    assert launcher.read_text(encoding="utf-8") == "user-owned"


def test_normal_linux_venv_interpreter_symlink_is_accepted(monkeypatch, tmp_path):
    directory = tmp_path / "venv"
    python = directory / "bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: True if p == python else original(p))
    monkeypatch.setattr(bootstrap, "_has_pip", lambda p: True)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert bootstrap.prepare_venv(directory) == python


def test_existing_interpreter_with_wrong_runtime_prefix_is_rejected(monkeypatch, tmp_path):
    directory = tmp_path / "venv"
    python = directory / "bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(BridgeError) as exc:
        bootstrap.prepare_venv(directory)
    assert exc.value.details["stage"] == "venv_identity"


def test_portable_upgrade_excludes_old_runtime_wheels(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "portable_builder", Path(__file__).parents[1] / "scripts/build_portable.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: None)
    public_files = [
        "pyproject.toml",
        "README.md",
        "requirements.lock",
        "requirements-dev.lock",
        ".gitignore",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
    ]
    for name in public_files:
        (tmp_path / name).write_text("fixture\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion="{builder.__version__}"\n', encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/private.jsonl").write_text("private captured conversation\n", encoding="utf-8")
    (tmp_path / "tests/private.sqlite3").write_bytes(b"private database")
    (tmp_path / "tests/private.sqlite3-wal").write_bytes(b"private uncheckpointed records")
    destination = tmp_path / "dist"
    destination.mkdir()
    old = destination / "session_bridge-0.1.0-py3-none-any.whl"
    current = destination / f"session_bridge-{builder.__version__}-py3-none-any.whl"
    old.write_bytes(b"old runtime")
    current.write_bytes(b"current runtime")
    builder.main()
    with zipfile.ZipFile(destination / f"session-bridge-{builder.__version__}-portable.zip") as archive:
        wheels = [name for name in archive.namelist() if name.endswith(".whl")]
        assert wheels == ["session-bridge/dist/" + current.name]
        assert archive.read(wheels[0]) == b"current runtime"
        for name in public_files:
            assert archive.read("session-bridge/" + name) == (tmp_path / name).read_bytes()
        assert not any("private" in name for name in archive.namelist())


def test_portable_version_comes_from_checkout_not_installed_runtime(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "source_version_builder", Path(__file__).parents[1] / "scripts/build_portable.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: None)
    for name in (
        "README.md",
        "requirements.lock",
        "requirements-dev.lock",
        ".gitignore",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
    ):
        (tmp_path / name).write_text("fixture\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="9.1.0"\n', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / f"session_bridge-{builder.__version__}-py3-none-any.whl").write_bytes(b"installed old runtime")
    (dist / "session_bridge-9.1.0-py3-none-any.whl").write_bytes(b"source runtime")
    builder.main()
    with zipfile.ZipFile(dist / "session-bridge-9.1.0-portable.zip") as archive:
        assert archive.read("session-bridge/dist/session_bridge-9.1.0-py3-none-any.whl") == b"source runtime"
