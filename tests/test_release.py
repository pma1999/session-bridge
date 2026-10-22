import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def release(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "verify_release", Path(__file__).parents[1] / "scripts/verify_release.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.3.1"\n', encoding="utf-8")
    for name in (
        "LICENSE",
        "README.md",
        "README.es.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
    ):
        (tmp_path / name).write_text("Synthetic release fixture\n", encoding="utf-8")
    module = tmp_path / "src/session_bridge/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text('__version__ = "0.3.1"\n', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()

    def build(*, license=True, extra=None, wheel_extra=None, package_name="session-bridge"):
        wheel = dist / "session_bridge-0.3.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("session_bridge/__init__.py", module.read_bytes())
            archive.writestr(
                "session_bridge-0.3.1.dist-info/METADATA",
                f"Name: {package_name}\nVersion: 0.3.1\nLicense-Expression: MIT\n",
            )
            for name in ("WHEEL", "entry_points.txt", "RECORD"):
                archive.writestr(f"session_bridge-0.3.1.dist-info/{name}", "Synthetic fixture\n")
            if license:
                archive.writestr(
                    "session_bridge-0.3.1.dist-info/licenses/LICENSE", (tmp_path / "LICENSE").read_bytes()
                )
            if wheel_extra:
                archive.writestr(wheel_extra, b"unexpected wheel content")
        output = dist / "session-bridge-0.3.1-portable.zip"
        with zipfile.ZipFile(output, "w") as archive:
            for file in tmp_path.rglob("*"):
                if file.is_file() and not file.is_relative_to(dist):
                    archive.write(file, "session-bridge/" + file.relative_to(tmp_path).as_posix())
            archive.write(wheel, "session-bridge/dist/" + wheel.name)
            if extra:
                archive.writestr(extra, b"private or unexpected content")
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        output.with_suffix(".zip.sha256").write_text(checksum + "  " + output.name + "\n")
        (dist / "SHA256SUMS").write_text(
            checksum
            + "  "
            + output.name
            + "\n"
            + hashlib.sha256(wheel.read_bytes()).hexdigest()
            + "  "
            + wheel.name
            + "\n"
        )
        return verifier, tmp_path, output

    return build


def test_release_requires_license_in_wheel(release):
    verifier, root, _ = release(license=False)
    with pytest.raises(ValueError, match="license|LICENSE"):
        verifier.verify(root)


@pytest.mark.parametrize(
    "extra", ["session-bridge/work/private.json", "session-bridge/tests/private.jsonl", "../private.txt"]
)
def test_release_rejects_unexpected_or_private_members(release, extra):
    verifier, root, _ = release(extra=extra)
    with pytest.raises(ValueError):
        verifier.verify(root)


def test_release_checks_published_checksum(release):
    verifier, root, output = release()
    output.with_suffix(".zip.sha256").write_text("0" * 64 + "  " + output.name + "\n")
    with pytest.raises(ValueError, match="checksum"):
        verifier.verify(root)


def test_valid_release_keeps_source_license_and_version(release):
    verifier, root, _ = release()
    assert verifier.verify(root)["version"] == "0.3.1"


@pytest.mark.parametrize(
    "wheel_extra",
    ["session_bridge/private-user-data.txt", "unrelated_package/__init__.py"],
)
def test_release_rejects_unexpected_wheel_members(release, wheel_extra):
    verifier, root, _ = release(wheel_extra=wheel_extra)
    with pytest.raises(ValueError, match="wheel member"):
        verifier.verify(root)


def test_release_rejects_unrelated_wheel_package_name(release):
    verifier, root, _ = release(package_name="unrelated-package")
    with pytest.raises(ValueError, match="package name"):
        verifier.verify(root)
