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

    bridge_state = gui_state.bridge

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
            if not body:
                return {}
            # Decode with fallback for non-UTF-8 bytes (e.g. Windows-1252 em dashes)
            text = body.decode("utf-8", errors="replace")
            return json.loads(text)

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

        # ── Bridge API GET handlers (inline, not delegated) ──

        def _handle_bridge_get(self):
            if self.path == "/api/profile":
                bridge_state.agent_connected = True
                profile_dict = asdict(bridge_state.profile)
                if profile_dict.get("ai_config", {}).get("api_key"):
                    profile_dict["ai_config"]["api_key"] = "***REDACTED***"
                self._send_json({"status": "ok", "profile": profile_dict})

            elif self.path == "/api/status":
                # Only mark connected if called by the agent (not the GUI polling)
                # The GUI uses fetch with default headers; agents use curl
                ua = self.headers.get("User-Agent", "")
                if "curl" in ua.lower() or "python" in ua.lower():
                    bridge_state.agent_connected = True
                self._send_json({
                    "status": "ok",
                    "agent_connected": bridge_state.agent_connected,
                    "pending_questions": len(bridge_state.get_pending_questions()),
                    "suggestions": len(bridge_state.suggestions),
                    "agent_done": bridge_state.agent_done,
                })

            elif self.path.startswith("/api/response/"):
                qid = self.path.split("/")[-1]
                with bridge_state.lock:
                    q = bridge_state.questions.get(qid)
                if not q:
                    self._send_json({"status": "error", "message": "Question not found"}, 404)
                elif q.answered.is_set():
                    self._send_json({"status": "ok", "answer": q.answer})
                else:
                    self._send_json({"status": "waiting", "message": "User hasn't answered yet"})

            elif self.path == "/api/suggestions":
                with bridge_state.lock:
                    data = [
                        {"id": s.id, "category": s.category, "title": s.title,
                         "content": s.content, "accepted": s.accepted}
                        for s in bridge_state.suggestions
                    ]
                self._send_json({"status": "ok", "suggestions": data})

            elif self.path == "/api/messages":
                with bridge_state.lock:
                    msgs = list(bridge_state.messages)
                self._send_json({"status": "ok", "messages": msgs})

            else:
                self._send_json({"status": "error", "message": "Not found"}, 404)

        # ── Bridge API POST handlers (inline) ──

        def _handle_bridge_post(self):
            bridge_state.agent_connected = True
            data = self._read_json()

            if self.path == "/api/suggest":
                category = data.get("category", "message")
                title = data.get("title", "")
                content = data.get("content", "")
                sid = bridge_state.add_suggestion(category, title, content)
                self._send_json({"status": "ok", "suggestion_id": sid})

            elif self.path == "/api/ask":
                question = data.get("question", "")
                choices = data.get("choices")
                timeout = data.get("timeout", 300)
                q = bridge_state.add_question(question, choices)
                answered = q.answered.wait(timeout=timeout)
                if answered:
                    self._send_json({"status": "ok", "answer": q.answer})
                else:
                    self._send_json({"status": "timeout", "message": "User did not respond in time"})

            elif self.path == "/api/ask/async":
                question = data.get("question", "")
                choices = data.get("choices")
                q = bridge_state.add_question(question, choices)
                self._send_json({"status": "ok", "question_id": q.id})

            elif self.path == "/api/message":
                content = data.get("content", "")
                role = data.get("role", "agent")
                bridge_state.add_message(role, content)
                self._send_json({"status": "ok"})

            elif self.path == "/api/done":
                bridge_state.agent_done = True
                summary = data.get("summary", "")
                bridge_state.add_message("agent", f"Migration setup complete! {summary}")
                self._send_json({"status": "ok"})

            else:
                self._send_json({"status": "error", "message": "Not found"}, 404)

        # ── Main route dispatch ──

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
                self._handle_bridge_get()

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

            elif self.path == "/wizard/launch-agent":
                data = self._read_json()
                result = _launch_agent(gui_state, server.server_address[1])
                self._send_json(result)

            elif self.path.startswith("/api/"):
                self._handle_bridge_post()

            else:
                self._send_json({"error": "Not found"}, 404)

    server = HTTPServer(("127.0.0.1", port), GUIHandler)
    actual_port = server.server_address[1]
    return server, actual_port


def _launch_agent(gui_state: GUIState, port: int) -> dict:
    """Launch the AI agent in a new terminal window."""
    import subprocess
    import sys

    provider = gui_state.profile.ai_config.provider or "claude_code"
    base = f"http://localhost:{port}"

    prompt = (
        f"You are helping me migrate from Windows to Linux. "
        f"A QuickHatch bridge API is running at {base}.\n\n"
        f"STEP 1: Send a status message so I know you're working:\n"
        f'  curl -s -X POST {base}/api/message -H "Content-Type: application/json" '
        f'-d \'{{"role":"agent","content":"Connected! Reading your system profile..."}}\'\n\n'
        f"STEP 2: Fetch my full system profile:\n"
        f"  curl -s {base}/api/profile\n\n"
        f"STEP 3: Analyze everything, sending status messages as you go:\n"
        f'  curl -s -X POST {base}/api/message -H "Content-Type: application/json" '
        f'-d \'{{"role":"agent","content":"Analyzing your hardware and apps..."}}\'\n\n'
        f"STEP 4: Post your recommendations using /api/suggest (one per category):\n"
        f'  curl -s -X POST {base}/api/suggest -H "Content-Type: application/json" '
        f'-d \'{{"category":"distro","title":"Recommended distro","content":"..."}}\'\n'
        f"  Categories: distro, apps, drivers, desktop, plan\n\n"
        f"STEP 5: If you need to ask me something:\n"
        f'  curl -s -X POST {base}/api/ask -H "Content-Type: application/json" '
        f'-d \'{{"question":"your question here"}}\'\n\n'
        f"STEP 6: When done, signal completion:\n"
        f'  curl -s -X POST {base}/api/done -H "Content-Type: application/json" '
        f'-d \'{{"summary":"All recommendations posted."}}\'\n\n'
        f"IMPORTANT: Send /api/message updates frequently so I know you're working. "
        f"Think freely. Use your full knowledge. Provide links, reasoning, pros/cons. "
        f"Do not limit yourself to a few options. Consider the full Linux ecosystem."
    )

    # Write prompt to file so we don't have to pass it inline (avoids quoting hell)
    prompt_file = Path.home() / ".quickhatch-agent-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    try:
        if sys.platform == "win32":
            # Write a PowerShell script that reads prompt from file
            ps_file = Path.home() / ".quickhatch-launch-agent.ps1"
            if provider == "claude_code":
                ps_content = (
                    f'$p = Get-Content "{prompt_file}" -Raw\n'
                    f'claude -p $p --allowedTools "Bash"\n'
                    f'Read-Host "Press Enter to close"\n'
                )
            elif provider == "codex":
                ps_content = (
                    f'$p = Get-Content "{prompt_file}" -Raw\n'
                    f'codex $p\n'
                    f'Read-Host "Press Enter to close"\n'
                )
            else:
                return {"status": "error", "message": f"Point your AI tool at {base}/api/profile"}

            ps_file.write_text(ps_content, encoding="utf-8")
            # Launch PowerShell in a new window with the script
            subprocess.Popen([
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", f'Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","{ps_file}"'
            ])
        else:
            # Linux - write prompt to file, launch in terminal
            prompt_file = Path.home() / ".quickhatch-agent-prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")

            if provider == "claude_code":
                shell_cmd = f'claude -p "$(cat {prompt_file})" --allowedTools "Bash"'
            elif provider == "codex":
                shell_cmd = f'codex "$(cat {prompt_file})"'
            else:
                return {"status": "error", "message": f"Point your AI tool at {base}/api/profile"}

            # Try common terminal emulators
            for term_cmd in [
                ["x-terminal-emulator", "-e", "bash", "-c", shell_cmd],
                ["gnome-terminal", "--", "bash", "-c", shell_cmd],
                ["konsole", "-e", "bash", "-c", shell_cmd],
                ["xfce4-terminal", "-e", f"bash -c '{shell_cmd}'"],
                ["xterm", "-e", "bash", "-c", shell_cmd],
            ]:
                try:
                    subprocess.Popen(term_cmd)
                    break
                except FileNotFoundError:
                    continue
            else:
                # Fallback: spawn directly
                subprocess.Popen(["bash", "-c", shell_cmd])

        return {"status": "ok", "message": "Agent launched! Check the new terminal window."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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
