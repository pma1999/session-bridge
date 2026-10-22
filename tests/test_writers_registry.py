import re

import pytest

from session_bridge import i18n
from session_bridge.models import Profile
from session_bridge.writers import registry


@pytest.mark.parametrize("tool,prefix", [("claude", ""), ("opencode", "ses_"), ("codex", "")])
def test_ids_allocated_before_any_native_effect(tool, prefix, tmp_path):
    profile = Profile(id=tool, tool=tool, home=str(tmp_path / "absent"))
    first, second = registry.allocate_id(profile), registry.allocate_id(profile)
    assert first != second
    assert first.startswith(prefix)
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", first)
    assert not (tmp_path / "absent").exists()


def test_unknown_native_version_is_explicitly_unsupported(tmp_path):
    profile = Profile(id="unknown", tool="claude", home=str(tmp_path), version="999.0.0")
    result = registry.capability(profile)
    assert result.native is False
    # The reason is a catalog key, so it reads in the language the result is shown in.
    assert result.reason == "capability.unverified_version"
    assert "versión" in i18n.text(result.reason).lower()


@pytest.fixture
def windows_x64(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(registry, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(registry, "platform", SimpleNamespace(machine=lambda: "AMD64"))


@pytest.mark.parametrize(
    "tool,version",
    [("claude", "2.1.999"), ("codex", "codex-cli 0.199.0"), ("opencode", "2.9.0")],
)
def test_newer_version_with_the_same_major_is_compatible(tmp_path, windows_x64, tool, version):
    profile = Profile(id=tool, tool=tool, home=str(tmp_path), version=version)
    result = registry.capability(profile)
    assert result.native is True
    assert result.reason == "capability.compatible"
    if tool == "opencode":
        assert result.max_messages == 9362


@pytest.mark.parametrize(
    "tool,version",
    [("claude", "2.1.100"), ("claude", "3.0.0"), ("codex", "0.100.0"), ("opencode", "1.19.4"), ("codex", "latest")],
)
def test_older_or_other_major_version_stays_unsupported(tmp_path, windows_x64, tool, version):
    profile = Profile(id=tool, tool=tool, home=str(tmp_path), version=version)
    result = registry.capability(profile)
    assert result.native is False
    assert result.reason == "capability.unverified_version"


@pytest.mark.parametrize("tool", ["claude", "codex", "opencode"])
def test_every_verified_version_reads_as_verified(tmp_path, windows_x64, tool):
    for version in registry.VERIFIED[tool]:
        result = registry.capability(Profile(id=tool, tool=tool, home=str(tmp_path), version=version))
        assert (result.native, result.reason) == (True, "capability.verified")


def test_compatible_versions_still_need_windows_x64(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(registry, "sys", SimpleNamespace(platform="linux"))
    profile = Profile(id="claude", tool="claude", home=str(tmp_path), version="2.1.999")
    result = registry.capability(profile)
    assert (result.native, result.reason) == (False, "capability.windows_x64_only")
