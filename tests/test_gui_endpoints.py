"""Tests for the GUI HTTP endpoints.

Spins up the real GUIServer on a random local port and hits it with urllib.
Does NOT cover endpoints that need real SSH or a real LLM — those have their
own unit tests elsewhere.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import GUIState, _resolve_ssh_private_key_path, make_gui_server


@pytest.fixture
def server():
    """Start a real GUI server on an ephemeral port. Cleans up after the test."""
    profile = UserProfile()
    bridge = BridgeState(profile, persist_path=None)
    gui_state = GUIState(profile, bridge)
    srv, port = make_gui_server(gui_state, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"server": srv, "port": port, "gui_state": gui_state, "bridge": bridge}
    finally:
        srv.shutdown()
        srv.server_close()


def _get(port, path, timeout=5):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read().decode("utf-8")
        return r.status, body


def _post(port, path, payload=None, timeout=5):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
        return r.status, body


def _post_error(port, path, payload=None, timeout=5):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post(port, path, payload=payload, timeout=timeout)
    return exc_info.value.code, exc_info.value.read().decode("utf-8")


class TestStaticRoutes:
    def test_serves_html_at_root(self, server):
        status, body = _get(server["port"], "/")
        assert status == 200
        assert "<!DOCTYPE html>" in body or "<html" in body.lower()
        assert "QuickHatch" in body

    def test_serves_html_at_index(self, server):
        status, body = _get(server["port"], "/index.html")
        assert status == 200
        assert "QuickHatch" in body

    def test_remote_setup_html_exposes_wipe_mode_button(self, server):
        status, body = _get(server["port"], "/")
        assert status == 200
        assert "Wipe + reinstall" in body
        assert "setRemoteSetupMode" in body


class TestProfileEndpoint:
    def test_returns_profile_json(self, server):
        status, body = _get(server["port"], "/wizard/profile")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert "profile" in data

    def test_redacts_api_key(self, server):
        server["gui_state"].profile.ai_config.api_key = "sk-secret-xyz"
        status, body = _get(server["port"], "/wizard/profile")
        assert "sk-secret-xyz" not in body
        assert "***SET***" in body

    def test_reflects_update_via_wizard_update(self, server):
        _post(
            server["port"],
            "/wizard/update",
            {"linux_ip": "1.2.3.4", "linux_user": "bob", "remote_setup_mode": "configure_only"},
        )
        status, body = _get(server["port"], "/wizard/profile")
        data = json.loads(body)
        assert data["profile"]["linux_ip"] == "1.2.3.4"
        assert data["profile"]["linux_user"] == "bob"
        assert data["profile"]["remote_setup_mode"] == "configure_only"

    def test_update_reports_changed_false_for_noop(self, server, monkeypatch):
        calls = []
        monkeypatch.setattr(server["gui_state"].profile, "save_checkpoint", lambda: calls.append(1))
        status, body = _post(server["port"], "/wizard/update", {"linux_user": ""})
        data = json.loads(body)
        assert status == 200
        assert data["changed"] is False
        assert calls == []

    def test_update_reports_changed_true_for_real_mutation(self, server, monkeypatch):
        calls = []
        monkeypatch.setattr(server["gui_state"].profile, "save_checkpoint", lambda: calls.append(1))
        status, body = _post(server["port"], "/wizard/update", {"linux_user": "alice"})
        data = json.loads(body)
        assert status == 200
        assert data["changed"] is True
        assert calls == [1]

    def test_update_returns_authoritative_normalized_profile(self, server):
        status, body = _post(server["port"], "/wizard/update", {"linux_ip": "10.0.0.5/24"})
        data = json.loads(body)
        assert status == 200
        assert data["profile"]["linux_ip"] == "10.0.0.5"

    def test_invalid_remote_setup_mode_falls_back_to_auto(self, server):
        status, body = _post(
            server["port"],
            "/wizard/update",
            {"remote_setup_mode": "definitely-not-valid"},
        )
        data = json.loads(body)
        assert status == 200
        assert data["profile"]["remote_setup_mode"] == "auto"

    def test_restore_profile_rehydrates_sparse_state_and_preserves_live_api_key(self, server):
        server["gui_state"].profile.ai_config.api_key = "sk-secret-xyz"
        status, body = _post(
            server["port"],
            "/wizard/restore-profile",
            {
                "profile": {
                    "preferences": {"migration_type": "fresh_install"},
                    "ai_config": {"provider": "api", "api_key": "***SET***"},
                    "hardware": {},
                    "apps": [],
                    "files_summary": {},
                    "windows_settings": {},
                    "ssh_public_key_path": "C:\\\\Users\\\\honzi\\\\.ssh\\\\quickhatch_ed25519.pub",
                    "additional_info": "restored",
                    "apps_to_install": ["Brave"],
                    "data_description": "copied over",
                    "data_transfer_method": "external_drive",
                    "linux_ip": "192.168.50.162",
                    "linux_user": "jan",
                    "target_hardware_override": "single RTX 3080 target",
                    "default_account_password": "password",
                    "completed_steps": ["welcome", "ai_setup", "scan"],
                }
            },
        )
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "ok"
        assert data["profile"]["linux_ip"] == "192.168.50.162"
        assert data["profile"]["linux_user"] == "jan"
        assert data["profile"]["completed_steps"] == ["welcome", "ai_setup", "scan"]
        assert server["gui_state"].profile.additional_info == "restored"
        assert server["bridge"].profile.linux_user == "jan"
        assert server["gui_state"].profile.ai_config.api_key == "sk-secret-xyz"

    def test_restore_profile_reloads_persisted_bridge_state_for_restored_profile(self, tmp_path):
        wanted = UserProfile()
        wanted.preferences.distro_preference = "arch"
        persist_path = tmp_path / "bridge.json"
        fingerprint = BridgeState(wanted, persist_path=None)._profile_fingerprint()
        persist_path.write_text(
            json.dumps(
                {
                    "profile_fingerprint": fingerprint,
                    "suggestions": [
                        {
                            "id": "s1",
                            "category": "plan",
                            "title": "Migration Plan",
                            "content": "persisted blueprint",
                        }
                    ],
                    "messages": [{"role": "agent", "content": "persisted message"}],
                    "questions": [],
                }
            ),
            encoding="utf-8",
        )

        wrong = UserProfile()
        wrong.preferences.distro_preference = "fedora"
        bridge = BridgeState(wrong, persist_path=persist_path)
        gui_state = GUIState(wrong, bridge)
        srv, port = make_gui_server(gui_state, port=0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            assert bridge.suggestions == []
            status, body = _post(
                port,
                "/wizard/restore-profile",
                {
                    "profile": {
                        "preferences": {"distro_preference": "arch"},
                        "ai_config": {},
                        "hardware": {},
                        "apps": [],
                        "files_summary": {},
                        "windows_settings": {},
                        "ssh_public_key_path": None,
                        "additional_info": "",
                        "apps_to_install": [],
                        "data_description": "",
                        "data_transfer_method": "",
                        "linux_ip": "",
                        "linux_user": "",
                        "target_hardware_override": "",
                        "default_account_password": "password",
                        "completed_steps": [],
                    }
                },
            )
            data = json.loads(body)
            assert status == 200
            assert data["status"] == "ok"

            status, body = _get(port, "/api/suggestions")
            suggestions = json.loads(body)["suggestions"]
            assert [s["title"] for s in suggestions] == ["Migration Plan"]

            status, body = _get(port, "/api/messages")
            messages = json.loads(body)["messages"]
            assert [m["content"] for m in messages] == ["persisted message"]
        finally:
            srv.shutdown()
            srv.server_close()

    def test_reset_all_clears_quickhatch_owned_data_but_preserves_user_keys(self):
        home = Path.home()
        checkpoint = home / ".quickhatch-checkpoint.json"
        persist_path = home / ".quickhatch-suggestions.json"
        export_dir = home / "Downloads" / "QuickHatch-Migration"
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "quickhatch-profile.json").write_text("{}", encoding="utf-8")

        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        generated_key = ssh_dir / "quickhatch_ed25519"
        generated_pub = ssh_dir / "quickhatch_ed25519.pub"
        generated_key.write_text("generated-private", encoding="utf-8")
        generated_pub.write_text("generated-public", encoding="utf-8")
        own_key = ssh_dir / "id_ed25519"
        own_pub = ssh_dir / "id_ed25519.pub"
        own_key.write_text("user-private", encoding="utf-8")
        own_pub.write_text("user-public", encoding="utf-8")

        profile = UserProfile()
        profile.linux_ip = "192.168.50.162"
        profile.linux_user = "jan"
        profile.ssh_public_key_path = str(own_pub)
        profile.completed_steps = ["welcome", "ssh", "export"]
        profile.save_checkpoint(checkpoint)

        bridge = BridgeState(profile, persist_path=persist_path)
        bridge.add_suggestion("plan", "Migration Plan", "persisted plan")
        bridge.add_message("agent", "persisted transcript")

        gui_state = GUIState(profile, bridge)
        srv, port = make_gui_server(gui_state, port=0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _post(port, "/wizard/reset-all", {})
            data = json.loads(body)
            assert status == 200
            assert data["status"] == "ok"
            assert gui_state.profile.linux_ip == ""
            assert gui_state.profile.linux_user == ""
            assert gui_state.profile.completed_steps == []
            assert bridge.suggestions == []
            assert bridge.messages == []
            assert bridge.questions == {}
            assert bridge.agent_done is False
            assert bridge.stop_requested is False
            assert not checkpoint.exists()
            assert not persist_path.exists()
            assert not export_dir.exists()
            assert not generated_key.exists()
            assert not generated_pub.exists()
            assert own_key.exists()
            assert own_pub.exists()
        finally:
            srv.shutdown()
            srv.server_close()


class TestSSHKeyResolution:
    def test_prefers_quickhatch_key_when_profile_path_is_missing(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / "quickhatch_ed25519").write_text("quickhatch-private", encoding="utf-8")
        (ssh_dir / "id_ed25519").write_text("default-private", encoding="utf-8")
        monkeypatch.setattr("quickhatch.gui.server.Path.home", lambda: home)
        profile = UserProfile()
        profile.ssh_public_key_path = ""
        assert _resolve_ssh_private_key_path(profile) == str(ssh_dir / "quickhatch_ed25519")

    def test_test_ssh_uses_quickhatch_key_when_profile_lost_public_path(
        self, server, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True)
        quickhatch_key = ssh_dir / "quickhatch_ed25519"
        quickhatch_key.write_text("quickhatch-private", encoding="utf-8")
        (ssh_dir / "id_ed25519").write_text("default-private", encoding="utf-8")
        monkeypatch.setattr("quickhatch.gui.server.Path.home", lambda: home)
        server["gui_state"].profile.ssh_public_key_path = None
        seen = {}

        def fake_ssh_exec(host, user, cmd, key_path=None, timeout=120, port=22):
            seen["key_path"] = key_path
            return (0, "Linux archiso 6.19", "")

        monkeypatch.setattr("quickhatch.gui.server._ssh_exec", fake_ssh_exec)
        status, body = _post(
            server["port"],
            "/wizard/test-ssh",
            {"ip": "192.168.50.162", "user": "root"},
        )
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "ok"
        assert seen["key_path"] == str(quickhatch_key)


class TestCompleteStepEndpoint:
    """/wizard/steps was removed (dead endpoint — the SPA drives step
    navigation from STEPS_MIGRATION/STEPS_CUSTOMIZE in app.html).
    Completed-step tracking now lives on the profile itself, reachable
    via /wizard/profile.
    """

    def _completed(self, port):
        status, body = _get(port, "/wizard/profile")
        data = json.loads(body)
        return data["profile"]["completed_steps"]

    def test_completed_list_starts_empty(self, server):
        assert self._completed(server["port"]) == []

    def test_marks_step_complete(self, server):
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        assert "welcome" in self._completed(server["port"])

    def test_idempotent(self, server):
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        assert self._completed(server["port"]).count("welcome") == 1


class TestBridgeEndpoints:
    def test_status_reports_counts(self, server):
        server["bridge"].add_suggestion("distro", "Arch", "use pacman")
        status, body = _get(server["port"], "/api/status")
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["suggestions"] == 1

    def test_suggestions_endpoint(self, server):
        server["bridge"].add_suggestion("distro", "Arch", "content")
        server["bridge"].add_suggestion("apps", "Firefox", "native")
        status, body = _get(server["port"], "/api/suggestions")
        data = json.loads(body)
        assert len(data["suggestions"]) == 2
        cats = [s["category"] for s in data["suggestions"]]
        assert "distro" in cats and "apps" in cats

    def test_messages_endpoint(self, server):
        server["bridge"].add_message("agent", "hello")
        server["bridge"].add_message("user", "hi back")
        status, body = _get(server["port"], "/api/messages")
        data = json.loads(body)
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "agent"
        assert data["messages"][1]["content"] == "hi back"

    def test_post_message_adds_to_bridge(self, server):
        _post(server["port"], "/api/message", {"role": "user", "content": "poke"})
        assert len(server["bridge"].messages) == 1
        assert server["bridge"].messages[0]["content"] == "poke"

    def test_post_suggest_adds_suggestion(self, server):
        _post(
            server["port"],
            "/api/suggest",
            {"category": "plan", "title": "Install", "content": "step 1..."},
        )
        assert len(server["bridge"].suggestions) == 1
        assert server["bridge"].suggestions[0].category == "plan"


class TestStopEndpoint:
    def test_stop_agent_sets_flag(self, server):
        assert server["bridge"].stop_requested is False
        _post(server["port"], "/wizard/stop-agent")
        assert server["bridge"].stop_requested is True

    def test_stop_adds_status_message(self, server):
        _post(server["port"], "/wizard/stop-agent")
        msgs = [m["content"] for m in server["bridge"].messages]
        assert any("Stop requested" in m for m in msgs)


class TestChatEndpoint:
    def test_chat_adds_user_message(self, server):
        _post(server["port"], "/wizard/chat", {"message": "use arch instead"})
        assert len(server["bridge"].messages) == 1
        m = server["bridge"].messages[0]
        assert m["role"] == "user"
        assert m["content"] == "use arch instead"

    def test_empty_chat_rejected(self, server):
        # Server returns 400 for empty messages — urllib raises HTTPError
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(server["port"], "/wizard/chat", {"message": ""})
        assert exc_info.value.code == 400


class TestDoneEndpoint:
    def test_done_marks_agent_done(self, server):
        assert server["bridge"].agent_done is False
        _post(server["port"], "/api/done", {"summary": "All set"})
        assert server["bridge"].agent_done is True

    def test_done_posts_finished_message(self, server):
        _post(server["port"], "/api/done", {"summary": "All set"})
        msgs = [m["content"] for m in server["bridge"].messages]
        assert any("Finished" in m and "All set" in m for m in msgs)


class TestAnswerEndpoint:
    def test_unknown_question_returns_404(self, server):
        status, body = _post_error(
            server["port"],
            "/wizard/answer",
            {"question_id": "missing", "answer": "hello"},
        )
        data = json.loads(body)
        assert status == 404
        assert data["status"] == "error"
        assert "unknown question_id" in data["message"]


class TestAgentLaunchAdmission:
    def test_launch_rejection_does_not_mutate_bridge_state(self, server):
        bridge = server["bridge"]
        gui_state = server["gui_state"]
        server["gui_state"].profile.ai_config.endpoint = "http://127.0.0.1:9"
        bridge.add_suggestion("distro", "Keep", "existing analysis")
        q = bridge.add_question("Keep pending?")
        bridge.agent_done = True
        bridge.stop_requested = True
        blocker = threading.Event()
        worker = threading.Thread(target=blocker.wait, daemon=True)
        worker.start()
        gui_state._agent_thread = worker
        try:
            status, body = _post(server["port"], "/wizard/launch-agent", {"phase": "analysis"})
        finally:
            blocker.set()
            worker.join(timeout=1)
            gui_state._agent_thread = None
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "error"
        assert bridge.agent_done is True
        assert bridge.stop_requested is True
        assert [s.category for s in bridge.suggestions] == ["distro"]
        assert q.id in bridge.questions

    def test_rerun_rejection_keeps_existing_analysis_and_feedback(self, server):
        bridge = server["bridge"]
        gui_state = server["gui_state"]
        server["gui_state"].profile.ai_config.endpoint = "http://127.0.0.1:9"
        bridge.add_suggestion("plan", "Keep", "existing plan")
        bridge.add_message("user", "existing feedback")
        bridge.agent_done = True
        bridge.stop_requested = True
        blocker = threading.Event()
        worker = threading.Thread(target=blocker.wait, daemon=True)
        worker.start()
        gui_state._agent_thread = worker
        try:
            status, body = _post(
                server["port"],
                "/wizard/rerun-agent",
                {"message": "new feedback"},
            )
        finally:
            blocker.set()
            worker.join(timeout=1)
            gui_state._agent_thread = None
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "error"
        assert bridge.agent_done is True
        assert bridge.stop_requested is True
        assert [s.category for s in bridge.suggestions] == ["plan"]
        assert [m["content"] for m in bridge.messages] == ["existing feedback"]

    def test_rerun_section_invalidates_downstream_sections(self, server, monkeypatch):
        import quickhatch.gui.server as gui_server_mod

        bridge = server["bridge"]
        for category in ["distro", "apps", "drivers", "desktop", "plan", "summary"]:
            bridge.add_suggestion(category, category.title(), "content")

        def fake_launch(gui_state, port, user_feedback="", phase="analysis", prelaunch=None):
            if prelaunch is not None:
                prelaunch()
            return {"status": "ok", "message": "stubbed"}

        monkeypatch.setattr(gui_server_mod, "_launch_agent", fake_launch)
        status, body = _post(
            server["port"],
            "/wizard/rerun-section",
            {"section": "apps", "message": "redo apps"},
        )
        data = json.loads(body)
        assert status == 200
        assert data["status"] == "ok"
        assert [s.category for s in bridge.suggestions] == ["distro"]
        assert bridge.messages[-1]["role"] == "user"
        assert bridge.messages[-1]["content"] == "redo apps"
