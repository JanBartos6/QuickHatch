from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "live_model_gauntlet.py"
MAIN_FIXTURE = ROOT / "tests" / "fixtures" / "live_model_gauntlet_requests.yaml"
HOLDOUT_FIXTURE = ROOT / "tests" / "fixtures" / "live_model_gauntlet_holdout_requests.yaml"
spec = importlib.util.spec_from_file_location("live_model_gauntlet", RUNNER_PATH)
assert spec is not None and spec.loader is not None
live_model_gauntlet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_model_gauntlet)


def _cases(path: Path = MAIN_FIXTURE) -> list[dict[str, Any]]:
    return live_model_gauntlet.load_cases(path)


def _case(case_id: str) -> dict[str, Any]:
    for item in _cases():
        if item["id"] == case_id:
            return item
    raise AssertionError(f"missing gauntlet case {case_id}")


def test_live_gauntlet_fixture_has_30_plan_only_cases() -> None:
    cases = _cases()

    assert len(cases) == 30
    assert {case["kind"] for case in cases} == {"plan", "repair"}
    assert all(case.get("mode") for case in cases)


def test_live_gauntlet_builds_all_prompts_without_provider_calls() -> None:
    prompts = [live_model_gauntlet.build_messages_for_case(case)[0]["content"] for case in _cases()]

    assert all("JSON" in prompt for prompt in prompts)
    assert all(
        "Do not mark the run as complete" in prompt or "repairing ONE" in prompt
        for prompt in prompts
    )


def test_live_gauntlet_holdout_fixture_builds_without_provider_calls() -> None:
    cases = _cases(HOLDOUT_FIXTURE)
    prompts = [live_model_gauntlet.build_messages_for_case(case)[0]["content"] for case in cases]

    assert len(cases) == 15
    assert {case["kind"] for case in cases} == {"plan", "repair"}
    assert any(case["mode"] == "fresh_install" for case in cases)
    assert any(case["mode"] == "configure_existing" for case in cases)
    assert any(case.get("needs_search") for case in cases)
    assert any(case.get("expects_manual_followup") for case in cases)
    assert all("JSON" in prompt for prompt in prompts)
    assert all(
        "Do not mark the run as complete" in prompt or "repairing ONE" in prompt
        for prompt in prompts
    )


def test_evaluate_plan_search_action_counts_structured_and_search() -> None:
    case = _case("plan-configure-arch-stale-app-hints")
    response = json.dumps(
        {
            "action": "repo_search",
            "query": "arch discord spotify obsidian package names",
            "reason": "Verify current package names before planning app commands.",
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["search_used_when_needed"] is True
    assert result["wrong_mode_command"] is False


def test_evaluate_configure_existing_wrong_mode_is_measured() -> None:
    case = _case("plan-configure-fedora-apps-fonts")
    bad_plan = {
        "target_summary": "Existing Fedora host, but model wrongly emits install work.",
        "distro_family": "fedora",
        "package_manager": "dnf",
        "driver_strategy": {
            "strategy": "skip",
            "confidence": "high",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "No driver change requested.",
            "evidence": [{"source_type": "local_facts", "summary": "Synthetic case."}],
        },
        "desktop_strategy": {
            "strategy": "skip",
            "confidence": "high",
            "source": "local_facts",
            "packages": [],
            "services": [],
            "notes": "No desktop change requested.",
            "evidence": [{"source_type": "local_facts", "summary": "Synthetic case."}],
        },
        "command_groups": [
            {
                "id": "bad-install-spine",
                "stage": "install_or_skip",
                "title": "Incorrectly bootstrap base system",
                "commands": ["sudo pacstrap -K /mnt base linux"],
                "operations": [],
                "verification": [],
                "rationale": "Wrong-mode regression fixture.",
                "source": "repo",
                "packages": ["base", "linux"],
                "evidence": [{"source_type": "local_facts", "summary": "Synthetic case."}],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": [],
        "optional_items": [],
        "planner_revision": 1,
    }

    result = live_model_gauntlet.evaluate_response(case, json.dumps(bad_plan))

    assert result["valid_structured_output"] is False
    assert result["wrong_mode_command"] is True


def test_evaluate_repair_manual_followup_quality() -> None:
    case = _case("repair-manual-followup-specific-app")
    response = json.dumps(
        {
            "action": "manual_followup",
            "reason": (
                "Adobe Acrobat Reader is unavailable in Debian apt and the user has "
                "not accepted Okular as an alternative."
            ),
            "app_names": ["Adobe Acrobat Reader"],
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["manual_followup_quality"] is True
    assert result["repair_unique"] is True


def test_evaluate_repair_identical_retry_is_not_unique() -> None:
    case = _case("repair-pacstrap-noconfirm")
    response = json.dumps(
        {
            "action": "retry_group",
            "group": {
                "id": "arch-bootstrap-retry",
                "stage": "install_or_skip",
                "title": "Repeat bootstrap without changing strategy",
                "commands": ["sudo pacstrap --noconfirm -K /mnt base linux linux-firmware"],
                "source": "repo",
                "packages": ["base", "linux", "linux-firmware"],
            },
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["repair_unique"] is False


def test_evaluate_repair_removed_invalid_flag_is_unique() -> None:
    case = _case("repair-pacstrap-noconfirm")
    response = json.dumps(
        {
            "action": "retry_group",
            "group": {
                "id": "arch-bootstrap-retry",
                "stage": "install_or_skip",
                "title": "Retry bootstrap after removing invalid flag",
                "commands": ["sudo pacstrap -K /mnt base linux linux-firmware"],
                "source": "repo",
                "packages": ["base", "linux", "linux-firmware"],
            },
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["repair_unique"] is True


def test_evaluate_repair_removed_unrecognized_argument_is_unique() -> None:
    case = next(
        item
        for item in _cases(HOLDOUT_FIXTURE)
        if item["id"] == "holdout-repair-fedora-dnf-invalid-flag"
    )
    response = json.dumps(
        {
            "action": "retry_group",
            "group": {
                "id": "fedora-installroot-base",
                "stage": "install_or_skip",
                "title": "Retry Fedora installroot with valid dnf flag",
                "commands": [
                    "sudo dnf --installroot=/mnt -y install fedora-release kernel NetworkManager"
                ],
                "source": "repo",
                "packages": ["fedora-release", "kernel", "NetworkManager"],
            },
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["repair_unique"] is True


def test_evaluate_repair_typed_operation_replacement_is_unique() -> None:
    case = next(
        item for item in _cases(HOLDOUT_FIXTURE) if item["id"] == "holdout-repair-fstab-raw-blocked"
    )
    response = json.dumps(
        {
            "action": "retry_group",
            "group": {
                "id": "fstab-write",
                "stage": "install_or_skip",
                "title": "Write fstab with typed operation",
                "commands": [],
                "operations": [
                    {
                        "kind": "write_fstab",
                        "target": "/mnt/etc/fstab",
                        "options": {"root": "UUID=abc"},
                    }
                ],
                "source": "repo",
                "packages": [],
            },
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["dangerous_raw_command_count"] == 0
    assert result["repair_unique"] is True


def test_repair_search_first_does_not_score_manual_followup_quality_yet() -> None:
    case = _case("repair-manual-followup-specific-app")
    response = json.dumps(
        {
            "action": "web_search",
            "query": "Adobe Acrobat Reader Debian Linux availability",
            "max_results": 3,
            "reason": "Verify whether the requested app still has an official Linux build.",
        }
    )

    result = live_model_gauntlet.evaluate_response(case, response)

    assert result["valid_structured_output"] is True
    assert result["search_used_when_needed"] is True
    assert result["manual_followup_quality"] is None


def test_gemini_native_helpers_extract_text_and_strip_openai_endpoint() -> None:
    url = live_model_gauntlet._gemini_native_url(
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-3.1-flash-lite",
        "secret-key",
    )
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": '{"ok":'},
                        {"text": "true}"},
                    ]
                }
            }
        ]
    }

    assert url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.1-flash-lite:generateContent?"
    )
    assert "/openai/" not in url
    assert live_model_gauntlet._extract_gemini_native_text(payload) == '{"ok":true}'


def test_call_model_auto_falls_back_to_native_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def openai_fail(**_kwargs: Any) -> str:
        calls.append("openai")
        raise RuntimeError("compat unavailable")

    def native_ok(**_kwargs: Any) -> str:
        calls.append("native")
        return '{"ok":true}'

    monkeypatch.setattr(live_model_gauntlet, "_call_openai_compat_model", openai_fail)
    monkeypatch.setattr(live_model_gauntlet, "_call_gemini_native_model", native_ok)

    result = live_model_gauntlet.call_model(
        provider="api_gemini",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.1-flash-lite",
        api_key="secret-key",
        messages=[{"role": "user", "content": "Return JSON."}],
        timeout=1,
        api_mode="auto",
    )

    assert result == '{"ok":true}'
    assert calls == ["openai", "native"]


def test_call_model_openai_mode_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def openai_fail(**_kwargs: Any) -> str:
        raise RuntimeError("compat unavailable")

    def native_unexpected(**_kwargs: Any) -> str:
        raise AssertionError("native fallback should not run in explicit openai mode")

    monkeypatch.setattr(live_model_gauntlet, "_call_openai_compat_model", openai_fail)
    monkeypatch.setattr(live_model_gauntlet, "_call_gemini_native_model", native_unexpected)

    with pytest.raises(RuntimeError, match="compat unavailable"):
        live_model_gauntlet.call_model(
            provider="api_gemini",
            endpoint="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.1-flash-lite",
            api_key="secret-key",
            messages=[{"role": "user", "content": "Return JSON."}],
            timeout=1,
            api_mode="openai_compat",
        )


def test_load_existing_case_results_keeps_latest_case_result(tmp_path: Path) -> None:
    output_dir = tmp_path / "gauntlet"
    output_dir.mkdir()
    (output_dir / "cases.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "case-a", "valid_structured_output": False}),
                json.dumps({"id": "case-b", "valid_structured_output": True}),
                json.dumps({"id": "case-a", "valid_structured_output": True}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    results = live_model_gauntlet.load_existing_case_results(output_dir)

    assert set(results) == {"case-a", "case-b"}
    assert results["case-a"]["valid_structured_output"] is True


def test_retryable_provider_error_detects_rate_limits() -> None:
    assert live_model_gauntlet.retryable_provider_error("HTTP 429 Too Many Requests")
    assert live_model_gauntlet.retryable_provider_error("RESOURCE_EXHAUSTED quota exceeded")
    assert not live_model_gauntlet.retryable_provider_error("Planner returned invalid JSON")
