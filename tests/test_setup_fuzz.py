from __future__ import annotations

from quickhatch.setup.fuzz import generate_harness_fuzz_cases, run_harness_fuzz


def test_harness_fuzzer_has_cross_family_cases():
    cases = generate_harness_fuzz_cases()
    families = {case.facts.distro_family for case in cases}

    assert {"arch", "ubuntu", "debian", "fedora"} <= families
    assert any(case.expect_block for case in cases)
    assert any(not case.expect_block for case in cases)


def test_harness_fuzzer_detects_false_blocks_and_missed_blocks():
    payload = run_harness_fuzz()

    assert payload["success"] is True
    assert payload["failed_count"] == 0
    assert payload["false_blocks"] == []
    assert payload["missed_blocks"] == []
