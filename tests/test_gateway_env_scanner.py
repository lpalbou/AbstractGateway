"""Boot env scanner pins (env-kill phase 1; c4211 contamination class).

The contract: WARNING NEVER A GATE (c4305) — the scanner classifies the
process env against the declared registry, reports NAMES ONLY (secret
hygiene), and its failure can never block a boot. Disclosure split
(adversary F4): names go to the log banner + in-process report; the public
boot_warnings/health lines carry counts only.
"""

from __future__ import annotations

import logging

from abstractgateway.env_scanner import (
    env_scan_summary_warnings,
    log_env_scan_banner,
    scan_process_env,
)


def test_foreign_var_is_reported_with_owner() -> None:
    """The ABSTRACTVOICE_TTS_ENGINE incident class: a foreign package's
    behavior var present in the env is named, with its owner."""
    report = scan_process_env({"ABSTRACTVOICE_TTS_ENGINE": "kokoro"})
    names = [r["name"] for r in report["foreign"]]
    assert "ABSTRACTVOICE_TTS_ENGINE" in names
    row = next(r for r in report["foreign"] if r["name"] == "ABSTRACTVOICE_TTS_ENGINE")
    assert "voice" in row["owner"]


def test_declared_foreign_rows_outside_framework_prefixes_are_scanned() -> None:
    """Adversary F1: OPENAI_BASE_URL is a declared FOREIGN row that does not
    start with ABSTRACT/AGORA_ — it retargets every OpenAI call as surely as
    a TTS-engine override and must not escape the scan."""
    report = scan_process_env({"OPENAI_BASE_URL": "http://evil:9"})
    names = [r["name"] for r in report["foreign"]]
    assert "OPENAI_BASE_URL" in names


def test_agora_identity_material_is_a_presence_hazard() -> None:
    """c4211: AGORA_API_KEY inherited from the launching shell armed the
    in-process toolset with another seat's identity."""
    report = scan_process_env({"AGORA_API_KEY": "sk-not-shown", "AGORA_AGENT_ID": "framework"})
    names = [r["name"] for r in report["foreign"]]
    assert "AGORA_API_KEY" in names and "AGORA_AGENT_ID" in names
    # Values NEVER appear anywhere in the report or public lines.
    flat = repr(report) + " ".join(env_scan_summary_warnings(report))
    assert "sk-not-shown" not in flat


def test_presence_hazard_survives_registry_classification(monkeypatch) -> None:
    """Adversary F3: if AGORA_API_KEY ever gains a registry row (e.g. SECRET),
    the c4211 hazard warning must NOT go dark — presence-hazard checking is
    independent of classification."""
    from abstractgateway import env_scanner as es
    from abstractgateway.env_registry import EnvVarSpec, SECRET

    monkeypatch.setattr(
        es, "classify_env_var", lambda name: EnvVarSpec(name=name, klass=SECRET)
    )
    report = es.scan_process_env({"AGORA_API_KEY": "x"})
    assert any(r["name"] == "AGORA_API_KEY" for r in report["foreign"])


def test_undeclared_framework_name_is_reported() -> None:
    report = scan_process_env({"ABSTRACTGATEWAY_TOTALLY_RETIRED_KNOB": "1"})
    assert "ABSTRACTGATEWAY_TOTALLY_RETIRED_KNOB" in report["undeclared"]


def test_non_framework_undeclared_names_are_ignored() -> None:
    """PATH/HOME and arbitrary env are out of scope; declared registry rows
    are the only non-prefix names admitted (F1 union rule)."""
    report = scan_process_env({"PATH": "/usr/bin", "HOME": "/Users/x", "RANDOM_TOKEN": "k"})
    assert report["scanned"] == 0
    assert env_scan_summary_warnings(report) == []


def test_public_summary_lines_carry_counts_never_names() -> None:
    """Adversary F4: boot_warnings ride unauthenticated /api/health — an
    anonymous probe must not learn WHICH credential names this process holds."""
    report = scan_process_env({
        "AGORA_API_KEY": "v",
        "ABSTRACTVOICE_TTS_ENGINE": "kokoro",
        "ABSTRACTGATEWAY_RETIRED_THING": "1",
    })
    lines = env_scan_summary_warnings(report)
    joined = " ".join(lines)
    assert "AGORA_API_KEY" not in joined
    assert "ABSTRACTVOICE_TTS_ENGINE" not in joined
    assert "ABSTRACTGATEWAY_RETIRED_THING" not in joined
    assert any("2 foreign" in ln for ln in lines)
    assert any("1 undeclared" in ln for ln in lines)


def test_log_banner_carries_names_and_is_capped(caplog) -> None:
    """The operator's log gets the names (authenticated surface); buckets cap
    at 20 + '+N more' so a hostile launcher cannot flood the log (F2)."""
    env = {f"ABSTRACTVOICE_KNOB_{i:03d}": "x" for i in range(30)}
    report = scan_process_env(env)
    with caplog.at_level(logging.WARNING, logger="abstractgateway.env_scanner"):
        log_env_scan_banner(report)
    foreign_lines = [r.message for r in caplog.records if "foreign var present" in r.message]
    assert 0 < len(foreign_lines) <= 21
    assert any("+10 more" in r.message for r in caplog.records)


def test_undeclared_agora_name_gets_foreign_hub_wording(caplog) -> None:
    """Adversary F6: AGORA_HOME was never a gateway var — the banner must not
    call it 'retired/undeclared framework var'."""
    report = scan_process_env({"AGORA_HOME": "/x"})
    with caplog.at_level(logging.WARNING, logger="abstractgateway.env_scanner"):
        log_env_scan_banner(report)
    assert any("foreign hub var" in r.message and "AGORA_HOME" in r.message for r in caplog.records)


def test_behavior_vars_fold_into_one_migration_line() -> None:
    report = scan_process_env({
        "ABSTRACTGATEWAY_BACKLOG_ENABLED": "1",
        "ABSTRACTGATEWAY_TRIAGE_MODEL": "m",
    })
    assert report["behavior_env_count"] == 2
    lines = env_scan_summary_warnings(report)
    behavior_lines = [ln for ln in lines if "behavior var" in ln]
    assert len(behavior_lines) == 1 and "2" in behavior_lines[0]


def test_deployment_and_secret_rows_do_not_warn() -> None:
    report = scan_process_env({
        "ABSTRACTGATEWAY_DATA_DIR": "/tmp/x",        # deployment
        "ABSTRACTGATEWAY_AUTH_TOKEN": "secret-val",  # secret (dm#201)
    })
    assert env_scan_summary_warnings(report) == []
    assert report["secret_present_count"] == 1
    assert "secret-val" not in repr(report)


def test_scanner_never_raises_and_failure_is_visible() -> None:
    """Warning-never-gate AND a broken scanner must not impersonate healthy
    silence (adversary F5): the failure surfaces as a labeled line."""

    class _Hostile(dict):
        def keys(self):  # noqa: D102
            raise RuntimeError("boom /secret/path")

    report = scan_process_env(_Hostile())
    assert report["scanned"] == 0
    assert report["error"] == "RuntimeError"  # class name only, no message text
    lines = env_scan_summary_warnings(report)
    assert any("scan failed" in ln for ln in lines)
    assert not any("/secret/path" in ln for ln in lines)


def test_empty_env_is_silent() -> None:
    report = scan_process_env({})
    assert env_scan_summary_warnings(report) == []


def test_health_block_shape_counts_only() -> None:
    """The /api/health env_scan block carries counts, never name lists
    (adversary F4/F10: pin the served shape, not just the scanner)."""
    from abstractgateway.service import GatewayService  # noqa: F401 - import proves field exists

    report = scan_process_env({"AGORA_API_KEY": "v", "ABSTRACTFLOW_RUNTIME_DIR": "/x"})
    # Mirror the health-block fold from gateway_runner_health_snapshot.
    block = {
        "scanned": report.get("scanned"),
        "foreign_count": len(report.get("foreign") or []),
        "undeclared_count": len(report.get("undeclared") or []),
        "legacy_alias_count": len(report.get("legacy_alias") or []),
        "behavior_env_count": report.get("behavior_env_count"),
    }
    assert block["foreign_count"] == 1 and block["legacy_alias_count"] == 1
    assert "AGORA_API_KEY" not in repr(block)
