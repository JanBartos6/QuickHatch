from __future__ import annotations

import json

from quickhatch.setup.debugtools import (
    adapter_spine_payload,
    classify_debug_payload,
    fuzz_harness_payload,
    harness_health_payload,
    load_facts_payload,
    preflight_debug_payload,
)
from quickhatch.setup.types import SetupFacts


def test_preflight_debug_payload_is_zero_ssh_fast_loop():
    payload = preflight_debug_payload(
        "pacman -S --noconfirm nvidia-open",
        facts=SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            current_user="jan",
            login_user="jan",
            is_root=False,
        ),
    )

    assert payload["blocked"] is False
    assert payload["decisions"][0]["kind"] == "shadow"
    assert payload["decisions"][0]["category"] == "privilege_precondition"


def test_classify_debug_payload_matches_package_missing_failure():
    payload = classify_debug_payload(
        command="sudo pacman -S nope",
        rc=1,
        stderr="error: target not found: nope",
    )

    assert payload["category"] == "package_missing"


def test_adapter_spine_debug_payload_exposes_family_skeleton():
    payload = adapter_spine_payload(family="fedora")

    assert payload["family"] == "fedora"
    assert any("dnf --installroot" in command for command in payload["commands"])


def test_load_facts_payload_accepts_nested_facts(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text(
        json.dumps({"facts": {"distro_family": "ubuntu", "package_manager": "apt"}}),
        encoding="utf-8",
    )

    facts = load_facts_payload(path)

    assert facts.distro_family == "ubuntu"
    assert facts.package_manager == "apt"


def test_harness_health_payload_runs_fast_gate(tmp_path):
    payload = harness_health_payload(root=tmp_path / "health", keep_artifacts=True)

    assert payload["success"] is True
    assert payload["failed_count"] == 0


def test_fuzz_harness_payload_runs_without_ssh():
    payload = fuzz_harness_payload()

    assert payload["success"] is True
    assert payload["case_count"] >= 10
    assert payload["false_blocks"] == []
    assert payload["missed_blocks"] == []
    assert any(
        result["name"] == "vpn_activation_blocks_ssh_continuity" for result in payload["results"]
    )
