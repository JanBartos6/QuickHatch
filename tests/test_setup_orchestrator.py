from __future__ import annotations

import json
import threading
import time

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import DesktopRuntimeState, SetupFacts


def test_orchestrator_blocks_on_required_planning_question():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    plan_payload = {
        "target_summary": "Arch workstation.",
        "distro_family": "arch",
        "package_manager": "pacman",
        "driver_strategy": {
            "strategy": "no_special_driver",
            "confidence": "resolved_official",
            "source": "official",
            "packages": [],
            "services": [],
            "notes": "",
        },
        "desktop_strategy": {
            "strategy": "plasma",
            "confidence": "resolved_official",
            "source": "official",
            "packages": ["plasma-desktop"],
            "services": ["sddm"],
            "notes": "",
        },
        "command_groups": [
            {
                "id": "desktop-1",
                "stage": "desktop",
                "title": "Persist desktop config",
                "commands": ["echo desktop"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [
            {
                "question": "Install the optional ChatGPT wrapper?",
                "required": True,
                "choices": ["yes", "no"],
                "timeout_seconds": 0.01,
            }
        ],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }
    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda command, timeout: (0, "", ""),
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "unresolved required questions" in summary.lower()
    setup_run = bridge.snapshot_setup_run()
    assert setup_run["status"] == "idle"
    pending = bridge.get_pending_questions()
    assert len(pending) == 1
    assert pending[0].blocking is True


def test_orchestrator_uses_harness_search_during_non_native_planning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
    )
    seen_searches: list[str] = []
    model_calls = {"count": 0}

    def model_call(_cfg, messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {"action": "repo_search", "query": "nvidia open", "reason": "resolve package"}
            )
        return json.dumps(
            {
                "action": "plan",
                "plan": {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "official_nvidia_open",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["nvidia-open"],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": ["echo desktop"],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        }
                    ],
                    "apps": [],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop"],
                    "optional_items": [],
                    "planner_revision": 1,
                },
            }
        )

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=lambda command, timeout: (0, "", ""),
        repo_search=lambda query: (
            seen_searches.append(query) or "[repo_search results]\nnvidia-open"
        ),
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert seen_searches == ["nvidia open"]
    assert "desktop runtime state is still unknown" in summary.lower()


def test_orchestrator_repairs_failed_command_group_with_search_and_retry():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    commands_seen: list[str] = []
    repo_searches: list[str] = []
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "verification": [],
                            "rationale": "",
                            "source": "community",
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                            "optional_slow": False,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        if model_calls["count"] == 2:
            return json.dumps(
                {
                    "action": "repo_search",
                    "query": "brave-bin arch package",
                    "reason": "resolve package",
                }
            )
        return json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "apps-1-repair",
                    "stage": "apps",
                    "title": "Install Brave with resolved package",
                    "commands": ["echo repaired brave install"],
                    "verification": [],
                    "rationale": "Use the resolved package path.",
                    "source": "community",
                    "packages": ["brave-bin"],
                },
            }
        )

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad install brave":
            return (1, "", "target not found: brave")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: repo_searches.append(query) or "brave-bin",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert repo_searches == ["brave-bin arch package"]
    assert "echo repaired brave install" in commands_seen
    assert "Installed/configured: Brave" in summary


def test_orchestrator_can_convert_failed_verification_into_manual_followup_after_question():
    profile = UserProfile(apps_to_install=["ChatGPT"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install ChatGPT",
                            "commands": ["echo install chatgpt"],
                            "verification": ["test -f /opt/chatgpt"],
                            "rationale": "",
                            "source": "community",
                            "packages": ["pake-chatgpt"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "ChatGPT",
                            "package_name": "pake-chatgpt",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                            "optional_slow": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        if model_calls["count"] == 2:
            return json.dumps(
                {
                    "action": "question",
                    "question": "ChatGPT verification failed. Leave it as manual follow-up?",
                    "choices": ["manual follow-up", "retry"],
                    "required": True,
                    "timeout_seconds": 2,
                }
            )
        return json.dumps(
            {
                "action": "manual_followup",
                "reason": "User chose to leave ChatGPT as manual follow-up after verification failed.",
                "app_names": ["ChatGPT"],
            }
        )

    def ssh_exec(command, _timeout):
        if command == "test -f /opt/chatgpt":
            return (1, "", "missing")
        return (0, "", "")

    def answer_pending_question():
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = bridge.get_pending_questions()
            if pending:
                bridge.answer_question(pending[0].id, "manual follow-up")
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for blocking repair question.")

    helper = threading.Thread(target=answer_pending_question)
    helper.start()
    try:
        runtime = SetupRuntime(
            profile=profile,
            bridge=bridge,
            config=AIConfig(
                provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
            ),
            host="127.0.0.1",
            user="jan",
            ssh_port=22,
            key_path=None,
            system_info_fn=lambda: "system info",
            call_model_text=model_call,
            probe_facts=lambda **kwargs: facts,
            detect_live_env=lambda: (False, "INSTALLED"),
            ssh_exec=ssh_exec,
            repo_search=lambda query: "",
            community_search=lambda query: "",
            web_search=lambda query, max_results: "",
            wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
            run_pre_reboot_checks=lambda: (True, [], ""),
            smoke_test_preboot_script=lambda: None,
            bootstrap_nopasswd_sudo=lambda current: current,
            runtime_prompt_block=lambda current, is_live: "",
            migration_plan_md="",
            distro_primer_md="",
            native_search_active=False,
        )

        summary = SetupOrchestrator(runtime).run()
    finally:
        helper.join(timeout=2)

    assert "Manual follow-up: ChatGPT" in summary


def test_orchestrator_blocks_when_required_repair_question_times_out():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
    )
    model_calls = {"count": 0}

    def model_call(_cfg, _messages):
        model_calls["count"] += 1
        if model_calls["count"] == 1:
            return json.dumps(
                {
                    "target_summary": "Arch workstation.",
                    "distro_family": "arch",
                    "package_manager": "pacman",
                    "driver_strategy": {
                        "strategy": "no_special_driver",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": [],
                        "services": [],
                        "notes": "",
                    },
                    "desktop_strategy": {
                        "strategy": "plasma",
                        "confidence": "resolved_official",
                        "source": "official",
                        "packages": ["plasma-desktop"],
                        "services": ["sddm"],
                        "notes": "",
                    },
                    "command_groups": [
                        {
                            "id": "desktop-1",
                            "stage": "desktop",
                            "title": "Persist desktop config",
                            "commands": [
                                "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                            ],
                            "verification": [],
                            "rationale": "",
                            "source": "repo",
                            "packages": [],
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
                            "verification": [],
                            "rationale": "",
                            "source": "community",
                            "packages": ["brave-bin"],
                        },
                    ],
                    "apps": [
                        {
                            "name": "Brave",
                            "package_name": "brave-bin",
                            "source": "community",
                            "status": "pending",
                            "required": True,
                        }
                    ],
                    "unresolved_questions": [],
                    "verification_commands": {},
                    "required_items": ["desktop", "apps"],
                    "optional_items": [],
                    "planner_revision": 1,
                }
            )
        return json.dumps(
            {
                "action": "question",
                "question": "Should I try a manual package build for Brave?",
                "choices": ["yes", "no"],
                "required": True,
                "timeout_seconds": 0.01,
            }
        )

    def ssh_exec(command, _timeout):
        if command == "bad install brave":
            return (1, "", "target not found")
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(
            provider="local_lmstudio", endpoint="http://localhost:1234/v1", model_name="test"
        ),
        host="127.0.0.1",
        user="jan",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=model_call,
        probe_facts=lambda **kwargs: facts,
        detect_live_env=lambda: (False, "INSTALLED"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (True, "fresh info", reconnect_user),
        run_pre_reboot_checks=lambda: (True, [], ""),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "required item" in summary.lower()
    pending = bridge.get_pending_questions()
    assert len(pending) == 1
    assert pending[0].blocking is True
