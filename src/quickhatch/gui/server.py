"""Web GUI server — serves the SPA and handles wizard API calls.

Combines the bridge API (for LLM communication) with a wizard API
(for the web frontend to drive the wizard steps).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from quickhatch.bridge.server import BridgeState, _make_handler as _make_bridge_handler
from quickhatch.config import UserProfile


class GUIState:
    """State for the web GUI wizard."""

    def __init__(self, profile: UserProfile, bridge_state: BridgeState) -> None:
        self.profile = profile
        self.bridge = bridge_state
        self.lock = threading.Lock()

    def update_profile(self, updates: dict) -> None:
        """Apply partial updates to the profile from the web UI."""
        with self.lock:
            prefs = updates.get("preferences", {})
            for k, v in prefs.items():
                if hasattr(self.profile.preferences, k):
                    setattr(self.profile.preferences, k, v)

            ai = updates.get("ai_config", {})
            for k, v in ai.items():
                if hasattr(self.profile.ai_config, k):
                    setattr(self.profile.ai_config, k, v)

            if "additional_info" in updates:
                self.profile.additional_info = updates["additional_info"]


def _load_html() -> bytes:
    """Load the SPA HTML file."""
    html_path = Path(__file__).parent / "app.html"
    return html_path.read_bytes()


def make_gui_server(gui_state: GUIState, port: int = 0) -> tuple[HTTPServer, int]:
    """Create and return the GUI HTTP server."""

    # Get the bridge handler for /api/* routes
    BridgeHandler = _make_bridge_handler(gui_state.bridge)

    class GUIHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, data: Any, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            return json.loads(body) if body else {}

        def _send_html(self, content: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self._send_html(_load_html())

            elif self.path == "/wizard/profile":
                profile_dict = asdict(gui_state.profile)
                if profile_dict.get("ai_config", {}).get("api_key"):
                    profile_dict["ai_config"]["api_key"] = "***SET***"
                self._send_json({"status": "ok", "profile": profile_dict})

            elif self.path == "/wizard/steps":
                self._send_json({
                    "status": "ok",
                    "completed": gui_state.profile.completed_steps,
                    "steps": [
                        {"id": "welcome", "title": "Welcome"},
                        {"id": "ai_setup", "title": "AI Assistant"},
                        {"id": "questionnaire", "title": "Preferences"},
                        {"id": "scan", "title": "System Scan"},
                        {"id": "additional", "title": "Additional Info"},
                        {"id": "bridge", "title": "AI Analysis"},
                        {"id": "usb", "title": "Bootable USB"},
                        {"id": "ssh", "title": "SSH Setup"},
                        {"id": "export", "title": "Export"},
                    ],
                })

            elif self.path.startswith("/api/"):
                # Delegate to bridge handler
                bridge = BridgeHandler(self.request, self.client_address, self.server)
                bridge.path = self.path
                bridge.headers = self.headers
                bridge.rfile = self.rfile
                bridge.wfile = self.wfile
                bridge.do_GET()

            else:
                self._send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.path == "/wizard/update":
                data = self._read_json()
                gui_state.update_profile(data)
                gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok"})

            elif self.path == "/wizard/complete_step":
                data = self._read_json()
                step = data.get("step", "")
                if step and step not in gui_state.profile.completed_steps:
                    gui_state.profile.completed_steps.append(step)
                    gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok", "completed": gui_state.profile.completed_steps})

            elif self.path == "/wizard/scan":
                # Trigger system scan
                results = _run_scan()
                with gui_state.lock:
                    if results.get("apps"):
                        from quickhatch.config import AppInfo
                        gui_state.profile.apps = [AppInfo(**a) for a in results["apps"]]
                    if results.get("hardware"):
                        from quickhatch.config import HardwareInfo, GPUInfo, DiskInfo
                        hw = results["hardware"]
                        gui_state.profile.hardware = HardwareInfo(
                            **{k: v for k, v in hw.items() if k not in ("gpus", "disks")},
                            gpus=[GPUInfo(**g) for g in hw.get("gpus", [])],
                            disks=[DiskInfo(**d) for d in hw.get("disks", [])],
                        )
                    if results.get("settings"):
                        gui_state.profile.windows_settings = results["settings"]
                    if results.get("files"):
                        gui_state.profile.files_summary = results["files"]
                gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok", "results": results})

            elif self.path == "/wizard/export":
                from quickhatch.ai.instruction_gen import save_instructions
                output_dir = Path.home() / "QuickHatch-Migration"
                output_dir.mkdir(parents=True, exist_ok=True)
                inst_path = save_instructions(gui_state.profile, output_dir)
                self._send_json({
                    "status": "ok",
                    "output_dir": str(output_dir),
                    "instruction_file": str(inst_path),
                })

            elif self.path.startswith("/api/"):
                bridge = BridgeHandler(self.request, self.client_address, self.server)
                bridge.path = self.path
                bridge.headers = self.headers
                bridge.rfile = self.rfile
                bridge.wfile = self.wfile
                bridge.do_POST()

            else:
                self._send_json({"error": "Not found"}, 404)

    server = HTTPServer(("127.0.0.1", port), GUIHandler)
    actual_port = server.server_address[1]
    return server, actual_port


def _run_scan() -> dict:
    """Run system scanners and return results as dicts."""
    from dataclasses import asdict
    results = {}

    try:
        from quickhatch.scanners.apps import scan as scan_apps
        apps = scan_apps()
        results["apps"] = [asdict(a) for a in apps]
    except Exception:
        results["apps"] = []

    try:
        from quickhatch.scanners.hardware import scan as scan_hw
        hw = scan_hw()
        results["hardware"] = asdict(hw)
    except Exception:
        results["hardware"] = {}

    try:
        from quickhatch.scanners.settings import scan as scan_settings
        results["settings"] = scan_settings()
    except Exception:
        results["settings"] = {}

    try:
        from quickhatch.scanners.files import scan as scan_files
        results["files"] = scan_files()
    except Exception:
        results["files"] = {}

    return results
