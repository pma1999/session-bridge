import re

import pytest

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
    assert "versión" in result.reason.lower()
