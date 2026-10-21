"""vtai-cert discipline: the suite must pass on a clean tree and fail closed
when a guardrail is weakened. Also pins the certificate schema and the
stability of the suite digest."""

from pathlib import Path

from vtai_agent.certify import PROFILE, run_certification
from vtai_agent.guardrails import Guardrails


def test_clean_tree_passes_every_requirement(tmp_path):
    cert = run_certification(tmp_path)
    assert cert["all_passed"] is True
    assert cert["profile"] == PROFILE
    assert {c["id"] for c in cert["checks"]} == {"R1", "R2", "R3", "R4", "R5",
                                                "R6", "R7", "R8"}
    assert cert["digest"].startswith("sha256:")
    for c in cert["checks"]:
        assert c["passed"] and c["evidence"]


def test_suite_digest_stable_across_runs(tmp_path):
    a = run_certification(tmp_path / "a")
    b = run_certification(tmp_path / "b")
    assert a["suite_digest"] == b["suite_digest"]


def test_certificate_digest_binds_the_payload(tmp_path):
    import hashlib
    import json

    cert = run_certification(tmp_path)
    payload = {k: v for k, v in cert.items() if k != "digest"}
    expected = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
    assert cert["digest"] == expected


def test_weakened_guardrails_fail_the_suite(tmp_path, monkeypatch):
    """The point of the suite: if a guardrail stops enforcing, the
    certificate must say so (fail closed), not paper over it."""
    monkeypatch.setattr(
        Guardrails, "check_readable",
        lambda self, p: Path(p).expanduser().resolve(),  # deny check removed
    )
    cert = run_certification(tmp_path)
    assert cert["all_passed"] is False
    r1 = next(c for c in cert["checks"] if c["id"] == "R1")
    assert r1["passed"] is False and "MISSED" in r1["evidence"]


def test_kill_switch_probe_cleans_up(tmp_path):
    kill = tmp_path / "data" / "KILL"
    run_certification(tmp_path)
    assert not kill.exists()
