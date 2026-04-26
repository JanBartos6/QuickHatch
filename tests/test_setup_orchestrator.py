from __future__ import annotations

import json
import threading
import time

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.setup.orchestrator import SetupOrchestrator, SetupRuntime
from quickhatch.setup.types import DesktopRuntimeState, SetupFacts


def test_extract_created_user_from_chrooted_useradd():
    command = "arch-chroot /mnt useradd -m -G wheel,video -s /bin/bash jan"

    assert SetupOrchestrator._extract_created_user(command) == "jan"


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

    assert "unanswered required questions" in summary.lower()
    setup_run = bridge.snapshot_setup_run()
    assert setup_run["status"] == "blocked"
    pending = bridge.get_pending_questions()
    assert pending == []


def test_orchestrator_continues_after_answered_required_planning_question():
    profile = UserProfile()
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
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [
            {
                "question": "Proceed with KDE?",
                "required": True,
                "choices": ["yes", "no"],
                "timeout_seconds": 2,
            }
        ],
        "verification_commands": {},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def answer_pending_question():
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = bridge.get_pending_questions()
            if pending:
                bridge.answer_question(pending[0].id, "yes")
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for planning question.")

    helper = threading.Thread(target=answer_pending_question)
    helper.start()
    try:
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
    finally:
        helper.join(timeout=2)

    assert "Arch workstation" in summary


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


def test_live_install_failed_pre_reboot_checks_block_before_reconnect():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    plan_payload = {
        "target_summary": "Arch live install.",
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
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Install base system",
                "commands": ["echo install base", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install"],
        "optional_items": [],
        "planner_revision": 1,
    }
    reconnect_calls = {"count": 0}
    reboot_commands: list[str] = []

    def probe_facts(**kwargs):
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root",
            login_user="root",
            is_live=kwargs.get("is_live", True),
            live_marker=kwargs.get("live_marker", "LIVE"),
        )

    def ssh_exec(command, _timeout):
        if "reboot" in command:
            reboot_commands.append(command)
        return (0, "", "")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (True, "LIVE"),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=lambda reconnect_user: (
            reconnect_calls.__setitem__("count", 1) or (True, "fresh info", reconnect_user)
        ),
        run_pre_reboot_checks=lambda: (False, ["[kernel] missing"], "kernel missing"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert "kernel missing" in summary
    assert reconnect_calls["count"] == 0
    assert reboot_commands == []


def test_live_install_passes_guard_then_reboots_and_reconnects_once():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    live_state = {"is_live": True}
    reconnect_calls = {"count": 0}
    commands: list[str] = []
    plan_payload = {
        "target_summary": "Arch live install.",
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
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Install base system",
                "commands": ["echo install base", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
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
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install", "desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def probe_facts(**kwargs):
        is_live = bool(kwargs.get("is_live", live_state["is_live"]))
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root" if is_live else "jan",
            login_user="root" if is_live else "jan",
            is_live=is_live,
            live_marker="LIVE" if is_live else "INSTALLED",
            session_type="" if is_live else "wayland",
            desktop_runtime_state=(
                DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
                if is_live
                else DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            ),
        )

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        reconnect_calls["count"] += 1
        live_state["is_live"] = False
        return (True, "fresh info", "jan")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (
            live_state["is_live"],
            "LIVE" if live_state["is_live"] else "INSTALLED",
        ),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect,
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    assert reconnect_calls["count"] == 1
    assert commands.count("reboot") == 0
    assert any("umount -R /mnt" in command and "reboot" in command for command in commands)
    assert "Arch live install" in summary


def test_live_install_injects_disk_cleanup_before_destructive_partitioning():
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    live_state = {"is_live": True}
    commands: list[str] = []
    plan_payload = {
        "target_summary": "Arch live install.",
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
                "id": "install-1",
                "stage": "install_or_skip",
                "title": "Partition and install",
                "commands": ["parted -s /dev/sda mklabel gpt", "reboot"],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            },
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
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {},
        "required_items": ["install", "desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def probe_facts(**kwargs):
        is_live = bool(kwargs.get("is_live", live_state["is_live"]))
        return SetupFacts(
            distro_family="arch",
            package_manager="pacman",
            community_helper="none",
            current_user="root" if is_live else "jan",
            login_user="root" if is_live else "jan",
            is_live=is_live,
            live_marker="LIVE" if is_live else "INSTALLED",
            session_type="" if is_live else "wayland",
            desktop_runtime_state=(
                DesktopRuntimeState.BLOCKED_NO_GUI_SESSION
                if is_live
                else DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
            ),
        )

    def ssh_exec(command, _timeout):
        commands.append(command)
        return (0, "", "")

    def wait_for_reconnect(reconnect_user):
        live_state["is_live"] = False
        return (True, "fresh info", "jan")

    runtime = SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=AIConfig(provider="api_openai", endpoint="http://example", model_name="test"),
        host="127.0.0.1",
        user="root",
        ssh_port=22,
        key_path=None,
        system_info_fn=lambda: "system info",
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
        probe_facts=probe_facts,
        detect_live_env=lambda: (
            live_state["is_live"],
            "LIVE" if live_state["is_live"] else "INSTALLED",
        ),
        ssh_exec=ssh_exec,
        repo_search=lambda query: "",
        community_search=lambda query: "",
        web_search=lambda query, max_results: "",
        wait_for_reconnect=wait_for_reconnect,
        run_pre_reboot_checks=lambda: (True, [], "ok"),
        smoke_test_preboot_script=lambda: None,
        bootstrap_nopasswd_sudo=lambda current: current,
        runtime_prompt_block=lambda current, is_live: "",
        migration_plan_md="",
        distro_primer_md="",
        native_search_active=False,
    )

    summary = SetupOrchestrator(runtime).run()

    parted_index = commands.index("parted -s /dev/sda mklabel gpt")
    assert commands[parted_index - 2] == "mountpoint -q /mnt && umount -R /mnt || true"
    assert commands[parted_index - 1] == "swapoff -a || true"
    messages = [message["content"] for message in bridge.messages]
    assert "`parted -s /dev/sda mklabel gpt`" in messages
    assert "Arch live install" in summary


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
        if command == "pacman -Qq":
            return (0, "brave-bin\n", "")
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


def test_orchestrator_does_not_run_stage_verifications_per_group():
    profile = UserProfile()
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
                "commands": [
                    "kwriteconfig6 --file kdeglobals --group Icons --key Theme candy-icons"
                ],
                "verification": [],
                "rationale": "",
                "source": "repo",
                "packages": [],
            }
        ],
        "apps": [],
        "unresolved_questions": [],
        "verification_commands": {"desktop": ["bad global verifier"]},
        "required_items": ["desktop"],
        "optional_items": [],
        "planner_revision": 1,
    }

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad global verifier":
            return (1, "", "should not run per command group")
        return (0, "", "")

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

    assert "bad global verifier" not in commands_seen
    assert "Arch workstation" in summary


def test_orchestrator_retries_malformed_repair_json():
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
                        },
                        {
                            "id": "apps-1",
                            "stage": "apps",
                            "title": "Install Brave",
                            "commands": ["bad install brave"],
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
        if model_calls["count"] == 2:
            return '{"action": "retry_group", "group": {"id": "broken", "commands": ["unterminated'
        assert "invalid JSON" in _messages[-1]["content"]
        return json.dumps(
            {
                "action": "retry_group",
                "group": {
                    "id": "apps-1-repair",
                    "stage": "apps",
                    "title": "Install Brave after malformed repair retry",
                    "commands": ["echo repaired brave install"],
                    "verification": [],
                    "packages": ["brave-bin"],
                },
            }
        )

    def ssh_exec(command, _timeout):
        commands_seen.append(command)
        if command == "bad install brave":
            return (1, "", "target not found: brave")
        if command == "pacman -Qq":
            return (0, "brave-bin\n", "")
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

    assert model_calls["count"] == 3
    assert "echo repaired brave install" in commands_seen
    assert "Installed/configured: Brave" in summary


def test_final_audit_blocks_when_completed_app_package_is_missing():
    profile = UserProfile(apps_to_install=["Brave"])
    bridge = BridgeState(profile, persist_path=None)
    facts = SetupFacts(
        distro_family="arch",
        package_manager="pacman",
        community_helper="yay",
        init_system="unknown",
        current_user="jan",
        login_user="jan",
        session_type="wayland",
        desktop_runtime_state=DesktopRuntimeState.LIVE_APPLY_SUCCEEDED,
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
                "commands": ["echo fake brave install"],
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

    def ssh_exec(command, _timeout):
        if command == "pacman -Qq":
            return (0, "plasma-desktop\n", "")
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
        call_model_text=lambda cfg, messages: json.dumps(plan_payload),
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

    assert "final package verification failed" in summary.lower()
    assert "Brave" in summary


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
    assert pending == []


def test_orchestrator_stop_interrupts_blocking_repair_question():
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
                "question": "Try a manual package build?",
                "choices": ["yes", "no"],
                "required": True,
                "timeout_seconds": 30,
            }
        )

    def ssh_exec(command, _timeout):
        if command == "bad install brave":
            return (1, "", "target not found")
        return (0, "", "")

    def stop_when_question_appears():
        deadline = time.time() + 2
        while time.time() < deadline:
            if bridge.get_pending_questions():
                bridge.request_stop()
                return
            time.sleep(0.01)
        raise AssertionError("Timed out waiting for blocking repair question.")

    helper = threading.Thread(target=stop_when_question_appears)
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

    assert "stopped by user while waiting for an answer" in summary.lower()
