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
            if "apps_to_install" in updates:
                self.profile.apps_to_install = updates["apps_to_install"]
            if "data_description" in updates:
                self.profile.data_description = updates["data_description"]
            if "linux_ip" in updates:
                self.profile.linux_ip = updates["linux_ip"]
            if "linux_user" in updates:
                self.profile.linux_user = updates["linux_user"]


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
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Try fixing invalid escape sequences (e.g. from LLM-generated JSON)
                try:
                    return json.loads(text.encode("utf-8").decode("unicode_escape", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}

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

            elif self.path == "/api/pending-questions":
                pending = bridge_state.get_pending_questions()
                self._send_json({"status": "ok", "questions": [
                    {"id": q.id, "question": q.question, "choices": q.choices}
                    for q in pending
                ]})

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
                result = _run_export(gui_state)
                self._send_json(result)

            elif self.path == "/wizard/answer":
                data = self._read_json()
                qid = data.get("question_id", "")
                answer = data.get("answer", "")
                if qid and answer:
                    bridge_state.answer_question(qid, answer)
                    self._send_json({"status": "ok"})
                else:
                    self._send_json({"status": "error", "message": "Missing question_id or answer"}, 400)

            elif self.path == "/wizard/chat":
                data = self._read_json()
                msg = data.get("message", "").strip()
                if msg:
                    bridge_state.add_message("user", msg)
                    self._send_json({"status": "ok"})
                else:
                    self._send_json({"status": "error", "message": "Empty message"}, 400)

            elif self.path == "/wizard/generate-ssh":
                result = _generate_ssh_key(gui_state)
                self._send_json(result)

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
        f"== API Reference ==\n"
        f"GET  {base}/api/profile      — my full system profile\n"
        f"GET  {base}/api/messages      — all messages (check for my replies)\n"
        f"POST {base}/api/message       — send status update: {{\"role\":\"agent\",\"content\":\"...\"}}\n"
        f"POST {base}/api/suggest       — post recommendation: {{\"category\":\"distro|apps|drivers|desktop|plan\",\"title\":\"...\",\"content\":\"...\"}}\n"
        f"POST {base}/api/ask           — ask me a question (blocks until I answer): {{\"question\":\"...\"}}\n"
        f"POST {base}/api/done          — signal you're finished: {{\"summary\":\"...\"}}\n\n"
        f"== Instructions ==\n"
        f"1. Send a status message: \"Connected! Reading your profile...\"\n"
        f"2. Fetch my profile with GET /api/profile\n"
        f"3. Send status updates as you analyze (\"Checking hardware...\", \"Reviewing apps...\" etc)\n"
        f"4. IMPORTANT: After posting your distro recommendation, use /api/ask to check:\n"
        f"   \"I'm recommending [distro]. Does that sound good, or would you prefer something else?\"\n"
        f"   Wait for my answer before continuing. Respect my preference if I picked a specific distro.\n"
        f"5. Post recommendations for each category (distro, apps, drivers, desktop, plan)\n"
        f"   Include download links where possible.\n"
        f"6. After posting all suggestions, check GET /api/messages for any replies from me.\n"
        f"   If I posted feedback (role: \"user\"), address it and update your suggestions.\n"
        f"7. Call /api/done when finished.\n\n"
        f"== Guidelines ==\n"
        f"- Send /api/message updates frequently so I know you're working\n"
        f"- Think freely. Use your full knowledge. Provide links, reasoning, pros/cons\n"
        f"- If I specified a distro preference, prioritize it unless there's a strong reason not to\n"
        f"- Include ISO download URLs in your distro recommendation\n"
        f"- Be thorough but don't take forever — aim for quality over quantity"
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
                    f'claude -p $p --dangerously-skip-permissions\n'
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
                shell_cmd = f'claude -p "$(cat {prompt_file})" --dangerously-skip-permissions'
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


def _generate_ssh_key(gui_state: GUIState) -> dict:
    """Generate an ed25519 SSH key pair for the migration."""
    import subprocess
    key_path = Path.home() / ".ssh" / "quickhatch_ed25519"
    pub_path = key_path.with_suffix(".pub")

    if pub_path.exists():
        gui_state.profile.ssh_public_key_path = str(pub_path)
        gui_state.profile.save_checkpoint()
        return {
            "status": "ok",
            "message": "SSH key already exists",
            "public_key": pub_path.read_text().strip(),
            "path": str(pub_path),
        }

    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-C", "quickhatch", "-f", str(key_path), "-N", ""],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        # Secure private key permissions
        try:
            key_path.chmod(0o600)
        except OSError:
            pass

        gui_state.profile.ssh_public_key_path = str(pub_path)
        gui_state.profile.save_checkpoint()
        return {
            "status": "ok",
            "message": "SSH key generated",
            "public_key": pub_path.read_text().strip(),
            "path": str(pub_path),
        }
    except FileNotFoundError:
        return {"status": "error", "message": "ssh-keygen not found. Install OpenSSH first."}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"ssh-keygen failed: {e.stderr or e.stdout}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _run_export(gui_state: GUIState) -> dict:
    """Run the full export: instructions, profile, SSH key, quickstart, zip."""
    import zipfile
    from dataclasses import asdict

    import yaml

    from quickhatch.ai.instruction_gen import generate_instructions, save_instructions

    output_dir = Path.home() / "QuickHatch-Migration"
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []

    try:
        # Instructions
        inst_path = save_instructions(gui_state.profile, output_dir)
        files.append(inst_path.name)

        # Profile JSON (redact API key)
        profile_dict = asdict(gui_state.profile)
        if profile_dict.get("ai_config", {}).get("api_key"):
            profile_dict["ai_config"]["api_key"] = "***STORED_SEPARATELY***"
        json_path = output_dir / "quickhatch-profile.json"
        json_path.write_text(json.dumps(profile_dict, indent=2))
        files.append(json_path.name)

        # Profile YAML
        yaml_path = output_dir / "quickhatch-profile.yaml"
        yaml_path.write_text(yaml.dump(profile_dict, default_flow_style=False, sort_keys=False))
        files.append(yaml_path.name)

        # SSH public key
        if gui_state.profile.ssh_public_key_path:
            ssh_pub = Path(gui_state.profile.ssh_public_key_path)
            if ssh_pub.exists():
                dest = output_dir / ssh_pub.name
                dest.write_text(ssh_pub.read_text())
                files.append(dest.name)

        # Quickstart script
        from quickhatch.wizard.export import _generate_quickstart
        qs_path = output_dir / "quickstart.sh"
        qs_path.write_text(_generate_quickstart(gui_state.profile))
        files.append(qs_path.name)

        # ZIP bundle
        zip_path = output_dir / "quickhatch-migration-bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in output_dir.iterdir():
                if file.name != zip_path.name and file.is_file():
                    zf.write(file, file.name)
        files.append(zip_path.name)

        return {
            "status": "ok",
            "output_dir": str(output_dir),
            "files": files,
        }
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
