"""macOS polish: per-OS codec default, mac cache dirs, vm_stat policy."""

import sys

from vtai_agent.config import Media
from vtai_agent.guardrails import Guardrails
from vtai_agent.tools import disk
from vtai_agent.tools.media import MediaTranscodeTool


def _g(sandbox) -> Guardrails:
    return Guardrails(sandbox.cfg, sandbox.settings)


def test_default_video_codec_matches_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert Media().video_codec == "libx264"
    monkeypatch.setattr(sys, "platform", "linux")
    assert Media().video_codec == "h264_vaapi"


def test_vaapi_never_available_off_linux(sandbox, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    tool = MediaTranscodeTool(_g(sandbox))
    assert tool._vaapi_available() is False


def test_cache_dirs_include_macos_locations():
    assert "~/Library/Caches" in disk.CACHE_DIRS
    assert "~/.Trash" in disk.CACHE_DIRS
    assert "~/.cache" in disk.CACHE_DIRS  # Linux entries kept


def test_vm_stat_has_sandbox_policy(sandbox):
    assert _g(sandbox)._mode_for("vm_stat") == "none"


def test_starter_toml_allowlist_matches_platform():
    from vtai_agent import cli

    toml = cli._starter_toml()
    if sys.platform == "darwin":
        assert '"vm_stat"' in toml
        assert '"free"' not in toml
    else:
        assert '"free"' in toml


def test_starter_binaries_all_have_policies(sandbox):
    """Fail-closed sandbox requires a policy per allowlisted binary."""
    import re

    from vtai_agent import cli
    from vtai_agent.config import Guardrails as GuardrailsConfig

    from vtai_agent.guardrails import Guardrails

    toml = cli._starter_toml()
    section = toml.split("shell_allowlist")[1].split("]")[0]
    bins = re.findall(r'"([a-z0-9_-]+)"', section)
    assert bins, "starter allowlist parsed empty"
    cfg = GuardrailsConfig(
        shell_allowlist=bins,
        trusted_bin_dirs=["/usr/bin"],
        shell_extra_modes={"pytest": "write"},
    )
    g = Guardrails(cfg, sandbox.settings)
    assert [b for b in bins if g._mode_for(b) is None] == []
