"""Tests for gallery-specific GUI endpoints."""

from __future__ import annotations

import json
import threading
import urllib.request
from unittest.mock import patch

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gui.server import GUIState, make_gui_server


@pytest.fixture
def gallery_server():
    profile = UserProfile()
    bridge = BridgeState(profile)
    gui_state = GUIState(profile, bridge)
    srv, port = make_gui_server(gui_state, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"server": srv, "port": port, "gui_state": gui_state}
    finally:
        srv.shutdown()
        srv.server_close()


def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(port: int, path: str, payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_gallery_endpoint_returns_entries_and_picks(gallery_server):
    sample_entries = [
        {
            "id": "unixporn-demo",
            "preview_image": "gallery/unixporn-demo.webp",
            "de": "hyprland",
            "title": "Demo",
            "verified": False,
        }
    ]
    gallery_server["gui_state"].profile.preferences.gallery_picks = ["unixporn-demo"]

    with patch("quickhatch.gui.server.load_gallery_entries", return_value=sample_entries):
        status, payload = _get(gallery_server["port"], "/wizard/gallery")

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["picks"] == ["unixporn-demo"]
    assert payload["entries"][0]["preview_image_url"].endswith(
        "/wizard/gallery-assets/gallery/unixporn-demo.webp"
    )


def test_gallery_picks_round_trip_into_profile(gallery_server):
    sample_entries = [{"id": "unixporn-a"}, {"id": "unixporn-b"}, {"id": "unixporn-c"}]

    with patch("quickhatch.gui.server.load_gallery_entries", return_value=sample_entries):
        status, payload = _post(
            gallery_server["port"],
            "/wizard/gallery-picks",
            ["unixporn-a", "unixporn-c"],
        )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["picks"] == ["unixporn-a", "unixporn-c"]
    assert gallery_server["gui_state"].profile.preferences.gallery_picks == [
        "unixporn-a",
        "unixporn-c",
    ]
