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

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import GUIState, make_gui_server


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
        _post(server["port"], "/wizard/update", {"linux_ip": "1.2.3.4", "linux_user": "bob"})
        status, body = _get(server["port"], "/wizard/profile")
        data = json.loads(body)
        assert data["profile"]["linux_ip"] == "1.2.3.4"
        assert data["profile"]["linux_user"] == "bob"

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
