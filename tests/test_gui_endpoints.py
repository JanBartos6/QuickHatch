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
    bridge = BridgeState(profile)
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


class TestStepsEndpoint:
    def test_returns_step_list(self, server):
        status, body = _get(server["port"], "/wizard/steps")
        assert status == 200
        data = json.loads(body)
        assert "steps" in data
        assert len(data["steps"]) > 0
        ids = [s["id"] for s in data["steps"]]
        assert "welcome" in ids
        assert "bridge" in ids

    def test_completed_list_starts_empty(self, server):
        status, body = _get(server["port"], "/wizard/steps")
        data = json.loads(body)
        assert data["completed"] == []


class TestCompleteStepEndpoint:
    def test_marks_step_complete(self, server):
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        status, body = _get(server["port"], "/wizard/steps")
        data = json.loads(body)
        assert "welcome" in data["completed"]

    def test_idempotent(self, server):
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        _post(server["port"], "/wizard/complete_step", {"step": "welcome"})
        status, body = _get(server["port"], "/wizard/steps")
        data = json.loads(body)
        # No duplicates
        assert data["completed"].count("welcome") == 1


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
