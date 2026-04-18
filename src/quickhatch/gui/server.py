"""Web GUI server — serves the SPA and handles wizard API calls.

Combines the bridge API (for LLM communication) with a wizard API
(for the web frontend to drive the wizard steps).
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile
from quickhatch.gallery.library import (
    find_gallery_entries,
    gallery_asset_path,
    load_gallery_entries,
)

# ── Tool schemas for function-calling ──

_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo. Use this to find Linux compatibility info, "
                "driver support, application alternatives, or any current information needed "
                "for the migration analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (1-10)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_search",
            "description": (
                "Search for images using DuckDuckGo. Use this to find screenshots of Linux "
                "desktop environments, themes, icon packs, or wallpapers to show the user "
                "what their desktop will look like."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The image search query (e.g. 'KDE Plasma dark theme desktop screenshot')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of images to return (1-5)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    },
]

_TOOLS_ANTHROPIC = [
    {
        "name": "web_search",
        "description": (
            "Search the web using DuckDuckGo. Use this to find Linux compatibility info, "
            "driver support, application alternatives, or any current information needed "
            "for the migration analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "image_search",
        "description": (
            "Search for images using DuckDuckGo. Use this to find screenshots of Linux "
            "desktop environments, themes, icon packs, or wallpapers to show the user "
            "what their desktop will look like."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The image search query (e.g. 'KDE Plasma dark theme desktop screenshot')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of images to return (1-5)",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
]


class GUIState:
    """State for the web GUI wizard."""

    def __init__(self, profile: UserProfile, bridge_state: BridgeState) -> None:
        self.profile = profile
        self.bridge = bridge_state
        self.lock = threading.Lock()
        # VM-verify (offline single-machine users can opt-in to have
        # the analysis agent generate a VM-aware install.sh and then
        # test-boot it in a disposable VirtualBox/QEMU VM before
        # running it on real hardware).
        self.vm_verify_enabled: bool = False
        # Latest VM-verify result dict (populated by /wizard/vm-verify).
        # Shape: {status, success, summary, stages, exit_code, tail, backend}
        self.vm_verify_result: dict | None = None
        # True while a VM verify run is in progress — used to lock the button.
        self.vm_verify_running: bool = False
        # User-in-loop assisted session (boot VM in GUI, user pastes key,
        # we SSH in). None when idle. While non-None, the VM is running
        # and waiting for the user to click Ready.
        self.vm_assisted_session = None  # type: AssistedSession | None
        # Instructions shown to the user during the assisted flow —
        # {"public_key": ..., "paste_snippet": ...}. Cleared after finish.
        self.vm_assisted_prompt: dict | None = None

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
            if "data_transfer_method" in updates:
                self.profile.data_transfer_method = updates["data_transfer_method"]
            if "linux_ip" in updates:
                self.profile.linux_ip = updates["linux_ip"]
            if "linux_user" in updates:
                self.profile.linux_user = updates["linux_user"]


def _load_html() -> bytes:
    """Load the SPA HTML file."""
    html_path = Path(__file__).parent / "app.html"
    return html_path.read_bytes()


def _gallery_entries_for_response() -> list[dict[str, Any]]:
    """Load gallery entries and add absolute asset URLs for the SPA."""
    entries = []
    for entry in load_gallery_entries():
        item = dict(entry)
        preview_image = item.get("preview_image", "")
        item["preview_image_url"] = (
            f"/wizard/gallery-assets/{preview_image}" if preview_image else ""
        )
        entries.append(item)
    return entries


def _format_recipe_short(entry: dict[str, Any]) -> str:
    """Render a single recipe as a compact one-paragraph line for library injection.

    Used when we want to expose the whole curated library to the LLM as
    reference/inspiration. Keeps the per-entry footprint small (~200-300
    chars) so the full 100+ library fits in a single prompt.
    """
    from quickhatch.gallery.curate import build_digest

    d = build_digest(entry)
    header_bits = [f"`{d.get('id', 'unknown')}`"]
    if d.get("de"):
        header_bits.append(d["de"])
    if d.get("theme"):
        header_bits.append(f"theme={d['theme']}")
    if d.get("mood"):
        header_bits.append(d["mood"])
    desc = d.get("description") or ""
    best_for = d.get("best_for") or []

    # Core layout shorthand (panel+launcher+terminal most relevant)
    comps = d.get("components") or {}
    comp_shorts = []
    for key in ("panel", "launcher", "terminal"):
        val = comps.get(key)
        if val:
            comp_shorts.append(f"{key}={val}")
    # Font shorthand (mono tends to be the more distinctive signal)
    font_bits = []
    if d.get("font_mono"):
        font_bits.append(f"mono={d['font_mono']}")
    if d.get("font_ui"):
        font_bits.append(f"ui={d['font_ui']}")

    line = f"- {' / '.join(header_bits)}"
    if desc:
        line += f" — {desc}"
    extras = []
    if comp_shorts:
        extras.append(", ".join(comp_shorts))
    if font_bits:
        extras.append(", ".join(font_bits))
    if best_for:
        extras.append("best_for: " + ", ".join(best_for))
    if extras:
        line += " [" + " | ".join(extras) + "]"
    return line


def _gallery_library_prompt_block(limit: int | None = None) -> str:
    """Inject the full curated gallery library as reference/inspiration data.

    The harness LLM gets this on every desktop-section prompt so it has a
    rich bank of community-verified, visually beautiful Linux desktops
    to match against, borrow from, or blend when the user's preferences
    don't land cleanly on any single one.
    """
    from quickhatch.gallery.library import load_gallery_entries

    try:
        entries = load_gallery_entries()
    except Exception:
        return ""
    if not entries:
        return ""
    if limit is not None and limit > 0:
        entries = entries[:limit]
    lines = [
        "== CURATED DESIGN LIBRARY ==",
        "",
        "The block below is a highly curated reference library of beautiful, "
        "community-verified Linux desktop designs. Every entry has been upvoted "
        "heavily on r/unixporn, GitHub, or DE-specific subreddits, and each has "
        "been programmatically parsed down to its theme, icon pack, fonts, panel, "
        "launcher, and window-manager decorations. The human-written description "
        "after each entry summarizes what living with that desktop feels like, "
        "and `best_for` lists the user intents it fits.",
        "",
        "HOW TO USE THIS LIBRARY:",
        "1. If the user's preferences map cleanly to one of these recipes, "
        "recommend it directly by id — it is already a proven good-looking design.",
        "2. If no single recipe matches, BORROW from them: pick the theme from "
        "one, the panel choice from another, the font stack from a third. These "
        "are reference designs known to look great; combining their pieces is a "
        "safer bet than inventing a new aesthetic from scratch.",
        "3. If the user asks for something specific that IS NOT in the library "
        '(e.g. "I want a tiling WM with a macOS-style centered dock in '
        'Everforest colors"), still look here for inspiration — use the closest '
        "recipes' component choices, font pairings, decoration values, and "
        "palettes as your starting point, then adapt only what needs adapting.",
        "4. Default to picking from this library over inventing. A beautiful "
        "reference desktop adapted slightly beats a from-scratch design every "
        "time.",
        "",
    ]
    for entry in entries:
        lines.append(_format_recipe_short(entry))
    lines.append("")
    lines.append(f"(End of library — {len(entries)} recipes total.)")
    return "\n".join(lines) + "\n\n"


def _format_recipe_digest(entry: dict[str, Any]) -> str:
    """Render a single gallery recipe as a compact markdown block for prompts.

    Aimed at LLM consumption — dense, structured, easy for the model to
    grep for specific fields. Leaves out heavy fields (palette, packages,
    install scripts) that would bloat the prompt.
    """
    from quickhatch.gallery.curate import build_digest

    d = build_digest(entry)
    lines = [f"### Recipe `{d.get('id', 'unknown')}`"]
    # Core identity line
    core_bits = []
    for key in ("de", "theme", "icons", "cursor", "font_ui", "font_mono", "accent_color"):
        val = d.get(key)
        if val:
            core_bits.append(f"**{key}**: {val}")
    if core_bits:
        lines.append("- " + " · ".join(core_bits))
    # Layout components (panel/dock/launcher/terminal/shell/...)
    comps = d.get("components") or {}
    comp_bits = []
    for key in (
        "panel",
        "dock",
        "launcher",
        "notifications",
        "lockscreen",
        "terminal",
        "shell",
        "prompt",
    ):
        val = comps.get(key)
        if val:
            comp_bits.append(f"{key}={val}")
    if comp_bits:
        lines.append("- **layout**: " + ", ".join(comp_bits))
    # Decorations (blur/gaps/rounding/animations)
    dec = d.get("decorations") or {}
    dec_bits = []
    for key in (
        "blur",
        "rounded_corners_px",
        "shadows",
        "gaps_in_px",
        "gaps_out_px",
        "border_size_px",
        "animations",
    ):
        val = dec.get(key)
        if val is not None:
            dec_bits.append(f"{key}={val}")
    if dec_bits:
        lines.append("- **decorations**: " + ", ".join(dec_bits))
    # Mood/density signals
    vibe_bits = []
    for key in ("mood", "density", "wallpaper_hint"):
        val = d.get(key)
        if val:
            vibe_bits.append(f"{key}={val}")
    if vibe_bits:
        lines.append("- **vibe**: " + ", ".join(vibe_bits))
    # Signature features
    sig = d.get("signature_features") or []
    if sig:
        lines.append("- **signature**: " + ", ".join(sig))
    # Human-written description + best_for intents
    if d.get("description"):
        lines.append(f"- **description**: {d['description']}")
    if d.get("best_for"):
        lines.append("- **best_for**: " + ", ".join(d["best_for"]))
    # Source attribution
    src_bits = []
    if d.get("source_url"):
        src_bits.append(d["source_url"])
    if d.get("dotfiles_url"):
        src_bits.append(f"dotfiles: {d['dotfiles_url']}")
    if src_bits:
        lines.append("- **source**: " + " · ".join(src_bits))
    return "\n".join(lines)


def _gallery_prompt_block(profile: UserProfile) -> str:
    """Format pinned gallery picks for prompt context."""
    picks = profile.preferences.gallery_picks
    if not picks:
        return ""
    entries = find_gallery_entries(picks)
    if not entries:
        joined = ", ".join(picks)
        return (
            "== PINNED GALLERY RECIPES ==\n"
            f"The user pinned these recipe IDs: {joined}\n"
            "Treat the first ID as primary and personalize within that recipe.\n\n"
        )

    lines = [
        "== PINNED GALLERY RECIPES ==",
        "",
        "These recipes have been pinned by the user. They are authoritative design "
        "references — do not invent a conflicting setup. The first recipe is the "
        "PRIMARY; any others are alternates for inspiration only.",
        "",
        "Each recipe contains structured design data (theme, icons, fonts, components, "
        "decorations) and a short human-written description. When generating desktop "
        "steps, match the primary recipe exactly for DE/theme/icons/fonts, and only "
        "personalize around it: wallpaper choice, accent within the palette, and "
        "app-specific additions the user scanned from Windows.",
        "",
    ]
    for index, entry in enumerate(entries, start=1):
        label = "PRIMARY" if index == 1 else f"ALTERNATE {index}"
        lines.append(f"--- {label} ---")
        lines.append(_format_recipe_digest(entry))
        lines.append("")
    return "\n".join(lines) + "\n"


def make_gui_server(
    gui_state: GUIState, port: int = 0, host: str = "127.0.0.1"
) -> tuple[HTTPServer, int]:
    """Create and return the GUI HTTP server."""

    bridge_state = gui_state.bridge

    class GUIHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, data: Any, status: int = 200) -> None:
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

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
                    return json.loads(
                        text.encode("utf-8").decode("unicode_escape", errors="replace")
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}

        def _send_html(self, content: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)

        def _send_bytes(self, content: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
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
                self._send_json(
                    {
                        "status": "ok",
                        "agent_connected": bridge_state.agent_connected,
                        "pending_questions": len(bridge_state.get_pending_questions()),
                        "suggestions": len(bridge_state.suggestions),
                        "agent_done": bridge_state.agent_done,
                    }
                )

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
                        {
                            "id": s.id,
                            "category": s.category,
                            "title": s.title,
                            "content": s.content,
                            "accepted": s.accepted,
                        }
                        for s in bridge_state.suggestions
                    ]
                self._send_json({"status": "ok", "suggestions": data})

            elif self.path == "/api/messages":
                with bridge_state.lock:
                    msgs = list(bridge_state.messages)
                self._send_json({"status": "ok", "messages": msgs})

            elif self.path == "/api/pending-questions":
                pending = bridge_state.get_pending_questions()
                self._send_json(
                    {
                        "status": "ok",
                        "questions": [
                            {"id": q.id, "question": q.question, "choices": q.choices}
                            for q in pending
                        ],
                    }
                )

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
                    self._send_json(
                        {"status": "timeout", "message": "User did not respond in time"}
                    )

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
                summary = data.get("summary", "").strip()
                if summary:
                    bridge_state.add_message("agent", f"Finished: {summary}")
                else:
                    bridge_state.add_message("agent", "Finished.")
                self._send_json({"status": "ok"})

            else:
                self._send_json({"status": "error", "message": "Not found"}, 404)

        # ── Main route dispatch ──

        def do_GET(self):
            parsed_path = urlparse(self.path)
            route = parsed_path.path

            if route == "/" or route == "/index.html":
                self._send_html(_load_html())

            elif route == "/wizard/profile":
                profile_dict = asdict(gui_state.profile)
                if profile_dict.get("ai_config", {}).get("api_key"):
                    profile_dict["ai_config"]["api_key"] = "***SET***"
                self._send_json({"status": "ok", "profile": profile_dict})

            elif route == "/wizard/version":
                from quickhatch import __version__

                self._send_json({"status": "ok", "version": __version__})

            elif route == "/wizard/pubkey":
                # Plain-text SSH public key so a live VM / target machine
                # can curl it straight from the wizard (e.g. via the NAT
                # gateway at 10.0.2.2 in VirtualBox) and append to
                # /root/.ssh/authorized_keys. No auth — this is a public
                # key, not a secret.
                pub_path = gui_state.profile.ssh_public_key_path
                if pub_path and Path(pub_path).exists():
                    content = Path(pub_path).read_text(encoding="utf-8").strip()
                else:
                    default = Path.home() / ".ssh" / "id_ed25519.pub"
                    content = default.read_text(encoding="utf-8").strip() if default.exists() else ""
                if not content:
                    self._send_bytes(
                        b"# No SSH key generated yet. Run /wizard/generate-ssh first.\n",
                        "text/plain",
                        404,
                    )
                else:
                    # Ensure trailing newline so >> append doesn't join lines
                    body = (content + "\n").encode("utf-8")
                    self._send_bytes(body, "text/plain; charset=utf-8")

            elif route == "/wizard/steps":
                self._send_json(
                    {
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
                    }
                )

            elif route == "/wizard/gallery":
                self._send_json(
                    {
                        "status": "ok",
                        "entries": _gallery_entries_for_response(),
                        "picks": list(gui_state.profile.preferences.gallery_picks),
                    }
                )

            elif route.startswith("/wizard/gallery-assets/"):
                relative_path = unquote(route.removeprefix("/wizard/gallery-assets/")).lstrip("/")
                asset_path = gallery_asset_path(relative_path)
                package_data_root = (Path(__file__).resolve().parents[1] / "data").resolve()
                try:
                    asset_path.relative_to(package_data_root)
                except ValueError:
                    self._send_json({"status": "error", "message": "Not found"}, 404)
                    return
                if not asset_path.exists() or not asset_path.is_file():
                    self._send_json({"status": "error", "message": "Not found"}, 404)
                    return
                content_type = (
                    mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
                )
                self._send_bytes(asset_path.read_bytes(), content_type)

            elif route == "/wizard/vm-verify/backends":
                from quickhatch.wizard.vm_verify import detect_backends, pick_backend

                backends = detect_backends()
                self._send_json(
                    {
                        "status": "ok",
                        "virtualbox": bool(backends.get("virtualbox")),
                        "qemu": bool(backends.get("qemu")),
                        "preferred": pick_backend(),
                    }
                )

            elif route == "/wizard/vm-verify/status":
                self._send_json(
                    {
                        "status": "ok",
                        "enabled": gui_state.vm_verify_enabled,
                        "running": gui_state.vm_verify_running,
                        "result": gui_state.vm_verify_result,
                        "assisted_active": gui_state.vm_assisted_session is not None,
                        "assisted_prompt": gui_state.vm_assisted_prompt,
                    }
                )

            elif route == "/wizard/lmstudio-models":
                models = []
                try:
                    import requests

                    resp = requests.get("http://localhost:1234/v1/models", timeout=5)
                    if resp.status_code == 200:
                        raw = [m["id"] for m in resp.json().get("data", []) if m.get("id")]
                        # Drop embedding-only models — they can't answer chat
                        # completions and confuse the picker.
                        models = [
                            m for m in raw if "embed" not in m.lower() and "embedding" not in m.lower()
                        ]
                except Exception:
                    pass
                self._send_json({"status": "ok", "models": models})

            elif route.startswith("/api/"):
                self._handle_bridge_get()

            else:
                self._send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.path == "/wizard/update":
                data = self._read_json()
                gui_state.update_profile(data)
                gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok"})

            elif self.path == "/wizard/gallery-picks":
                data = self._read_json()
                picks = data if isinstance(data, list) else data.get("picks", [])
                if not isinstance(picks, list):
                    self._send_json(
                        {"status": "error", "message": "Expected an array of recipe IDs."}, 400
                    )
                    return
                deduped_picks = list(
                    dict.fromkeys(str(item) for item in picks if str(item).strip())
                )
                if len(deduped_picks) > 10:
                    self._send_json({"status": "error", "message": "Pick up to 10 recipes."}, 400)
                    return
                valid_ids = {entry.get("id") for entry in load_gallery_entries()}
                invalid = [recipe_id for recipe_id in deduped_picks if recipe_id not in valid_ids]
                if invalid:
                    self._send_json(
                        {
                            "status": "error",
                            "message": "Unknown gallery recipe IDs.",
                            "invalid": invalid,
                        },
                        400,
                    )
                    return
                gui_state.profile.preferences.gallery_picks = deduped_picks
                gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok", "picks": deduped_picks})

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
                        from quickhatch.analysis.app_mapper import map_apps
                        from quickhatch.config import AppInfo

                        apps = [AppInfo(**a) for a in results["apps"]]
                        try:
                            apps = map_apps(apps)
                        except Exception:
                            pass
                        gui_state.profile.apps = apps
                        # Reflect the enriched apps in the response
                        results["apps"] = [asdict(a) for a in apps]
                    if results.get("hardware"):
                        from quickhatch.config import DiskInfo, GPUInfo, HardwareInfo

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

            elif self.path == "/wizard/vm-verify/enable":
                data = self._read_json()
                gui_state.vm_verify_enabled = bool(data.get("enabled", True))
                self._send_json({"status": "ok", "enabled": gui_state.vm_verify_enabled})

            elif self.path == "/wizard/vm-verify/run":
                data = self._read_json()
                iso_path = (data.get("iso_path") or "").strip() or None
                if gui_state.vm_verify_running:
                    self._send_json({"status": "error", "message": "VM verify already running"})
                else:
                    t = threading.Thread(
                        target=_run_vm_verify,
                        args=(gui_state, iso_path),
                        daemon=True,
                    )
                    t.start()
                    self._send_json({"status": "ok", "message": "VM verify started"})

            elif self.path == "/wizard/vm-verify/prep":
                # Phase A of user-in-loop flow: boot VM in GUI mode,
                # return the paste snippet for the user.
                data = self._read_json()
                iso_path = (data.get("iso_path") or "").strip()
                if not iso_path:
                    self._send_json(
                        {"status": "error", "message": "ISO path required for boot test"}
                    )
                elif gui_state.vm_assisted_session is not None:
                    self._send_json(
                        {
                            "status": "error",
                            "message": "A VM session is already active — cancel it first",
                        }
                    )
                else:
                    result = _start_vm_assisted(gui_state, iso_path)
                    self._send_json(result)

            elif self.path == "/wizard/vm-verify/finish":
                # Phase B of user-in-loop flow: user pasted the key,
                # SSH in, run the script, tear down.
                self._read_json()
                if gui_state.vm_assisted_session is None:
                    self._send_json({"status": "error", "message": "No active assisted session"})
                elif gui_state.vm_verify_running:
                    self._send_json({"status": "error", "message": "Script already running"})
                else:
                    t = threading.Thread(
                        target=_finish_vm_assisted,
                        args=(gui_state,),
                        daemon=True,
                    )
                    t.start()
                    self._send_json({"status": "ok", "message": "Running script inside VM"})

            elif self.path == "/wizard/vm-verify/cancel":
                # Tear down an active assisted session without running the script.
                self._read_json()
                sess = gui_state.vm_assisted_session
                if sess is not None and getattr(sess, "_vm", None) is not None:
                    try:
                        sess._vm.teardown()
                    except Exception:
                        pass
                gui_state.vm_assisted_session = None
                gui_state.vm_assisted_prompt = None
                self._send_json({"status": "ok"})

            elif self.path == "/wizard/answer":
                data = self._read_json()
                qid = data.get("question_id", "")
                answer = data.get("answer", "")
                if qid and answer:
                    bridge_state.answer_question(qid, answer)
                    self._send_json({"status": "ok"})
                else:
                    self._send_json(
                        {"status": "error", "message": "Missing question_id or answer"}, 400
                    )

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
                phase = data.get("phase", "analysis")
                # Clear stop flag + reset done, but KEEP any successfully-
                # generated suggestions. The analysis loop itself skips
                # sections that already have a successful suggestion, so
                # re-launching after a failure resumes from the failed
                # section instead of redoing 3 minutes of distro work.
                with bridge_state.lock:
                    bridge_state.stop_requested = False
                    bridge_state.agent_done = False
                    bridge_state._persist()
                result = _launch_agent(gui_state, server.server_address[1], phase=phase)
                self._send_json(result)

            elif self.path == "/wizard/stop-agent":
                self._read_json()  # drain request body so Windows doesn't RST
                bridge_state.request_stop()
                bridge_state.add_message(
                    "agent", "Stop requested — finishing current step and exiting."
                )
                self._send_json({"status": "ok"})

            elif self.path == "/wizard/test-ssh":
                data = self._read_json()
                host_in = (data.get("ip") or "").strip()
                user_in = (data.get("user") or "").strip()
                if not host_in or not user_in:
                    self._send_json({"status": "error", "message": "Enter IP and username first."})
                else:
                    port_num = 22
                    host = host_in
                    if ":" in host_in:
                        h, _, p = host_in.rpartition(":")
                        host = h
                        try:
                            port_num = int(p)
                        except ValueError:
                            pass
                    key_path = None
                    if gui_state.profile.ssh_public_key_path:
                        priv = gui_state.profile.ssh_public_key_path.replace(".pub", "")
                        if Path(priv).exists():
                            key_path = priv
                    if not key_path:
                        default_key = Path.home() / ".ssh" / "id_ed25519"
                        if default_key.exists():
                            key_path = str(default_key)
                    rc, out, err = _ssh_exec(
                        host,
                        user_in,
                        # `hostname` isn't shipped on minimal live ISOs (e.g. Arch),
                        # so `uname -n` which comes with coreutils is portable.
                        "uname -srn",
                        key_path,
                        timeout=15,
                        port=port_num,
                    )
                    if rc == 0:
                        self._send_json({"status": "ok", "info": out.strip().replace("\n", " · ")})
                    else:
                        self._send_json(
                            {
                                "status": "error",
                                "message": (err.strip() or "SSH connection failed")[:200],
                            }
                        )

            elif self.path == "/wizard/rerun-agent":
                data = self._read_json()
                user_msg = data.get("message", "")
                # Clear old suggestions, reset done state
                bridge_state.reset_for_rerun()
                # Add user message to bridge messages
                if user_msg:
                    bridge_state.add_message("user", user_msg)
                # Re-launch agent with user feedback context
                result = _launch_agent(gui_state, server.server_address[1], user_feedback=user_msg)
                self._send_json(result)

            elif self.path.startswith("/api/"):
                self._handle_bridge_post()

            else:
                self._send_json({"error": "Not found"}, 404)

    server = HTTPServer((host, port), GUIHandler)
    actual_port = server.server_address[1]
    return server, actual_port


def _execute_web_search(query: str, max_results: int = 5) -> str:
    """Execute a DuckDuckGo web search and return formatted results as JSON."""
    try:
        from ddgs import DDGS

        max_results = max(1, min(10, max_results))
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return json.dumps({"results": [], "message": "No results found"})
        formatted = []
        for r in results:
            formatted.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                }
            )
        return json.dumps({"results": formatted})
    except ImportError:
        return json.dumps({"error": "duckduckgo-search package not installed"})
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"})


def _execute_image_search(query: str, max_results: int = 3) -> str:
    """Execute a DuckDuckGo image search and return image URLs with metadata."""
    try:
        from ddgs import DDGS

        max_results = max(1, min(5, max_results))
        results = DDGS().images(query, max_results=max_results)
        if not results:
            return json.dumps({"images": [], "message": "No images found"})
        formatted = []
        for r in results:
            formatted.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("image", ""),
                    "source": r.get("source", ""),
                }
            )
        return json.dumps({"images": formatted})
    except ImportError:
        return json.dumps({"error": "ddgs package not installed"})
    except Exception as e:
        return json.dumps({"error": f"Image search failed: {e}"})


def _call_openai_with_tools(
    endpoint: str,
    model: str,
    messages: list[dict],
    api_key: str | None,
    bridge: BridgeState,
    max_rounds: int = 10,
) -> str:
    """Call an OpenAI-compatible API with tool-use loop (LM Studio, OpenAI)."""
    import requests

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{endpoint.rstrip('/')}/chat/completions"

    tools_supported = True

    for _round in range(max_rounds):
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        if tools_supported:
            body["tools"] = _TOOLS_OPENAI

        try:
            resp = requests.post(url, json=body, headers=headers, timeout=900)
            # If model doesn't support tools, retry without
            if resp.status_code in (400, 422) and tools_supported:
                tools_supported = False
                bridge.add_message(
                    "agent", "Model doesn't support tool use, proceeding without web search."
                )
                body.pop("tools", None)
                resp = requests.post(url, json=body, headers=headers, timeout=900)
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            if tools_supported:
                tools_supported = False
                body.pop("tools", None)
                resp = requests.post(url, json=body, headers=headers, timeout=900)
                resp.raise_for_status()
            else:
                raise

        data = resp.json()
        msg = data["choices"][0]["message"]

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content", "")

        # Append assistant message (with tool_calls) to conversation
        messages.append(msg)

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                fn_args = {}

            if fn_name == "web_search":
                query = fn_args.get("query", "")
                max_results = fn_args.get("max_results", 5)
                bridge.add_message("agent", f"Searching: {query}")
                result = _execute_web_search(query, max_results)
            elif fn_name == "image_search":
                query = fn_args.get("query", "")
                max_results = fn_args.get("max_results", 3)
                bridge.add_message("agent", f"Finding images: {query}")
                result = _execute_image_search(query, max_results)
            else:
                result = json.dumps({"error": f"Unknown function: {fn_name}"})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )

    # Exceeded max rounds — force a final response without tools
    body = {"model": model, "messages": messages, "temperature": 0.7, "max_tokens": 4096}
    resp = requests.post(url, json=body, headers=headers, timeout=900)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_anthropic_with_tools(
    api_key: str,
    model: str,
    messages: list[dict],
    system_prompt: str,
    bridge: BridgeState,
    max_rounds: int = 10,
) -> str:
    """Call the Anthropic Messages API with tool-use loop."""
    import requests

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    for _round in range(max_rounds):
        body = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": _TOOLS_ANTHROPIC,
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=900,
        )
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content", [])

        # Extract text and tool_use blocks
        text_parts = []
        tool_uses = []
        for block in content_blocks:
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            return "\n".join(text_parts)

        # Append assistant's full response to messages
        messages.append({"role": "assistant", "content": content_blocks})

        # Execute tools and build tool_result blocks
        tool_results = []
        for tu in tool_uses:
            if tu["name"] == "web_search":
                query = tu["input"].get("query", "")
                max_results = tu["input"].get("max_results", 5)
                bridge.add_message("agent", f"Searching: {query}")
                result = _execute_web_search(query, max_results)
            elif tu["name"] == "image_search":
                query = tu["input"].get("query", "")
                max_results = tu["input"].get("max_results", 3)
                bridge.add_message("agent", f"Finding images: {query}")
                result = _execute_image_search(query, max_results)
            else:
                result = json.dumps({"error": f"Unknown tool: {tu['name']}"})

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Force final response without tools
    body = {"model": model, "max_tokens": 4096, "system": system_prompt, "messages": messages}
    resp = requests.post(
        "https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=900
    )
    resp.raise_for_status()
    data = resp.json()
    return "\n".join(b["text"] for b in data["content"] if b["type"] == "text")


def _prefetch_search_context(
    profile: UserProfile, bridge: BridgeState
) -> tuple[str, list[dict[str, str]]]:
    """Run targeted searches based on the user's profile.

    Returns (search_context_text, desktop_images) where desktop_images is a list
    of {"title": ..., "url": ...} dicts to be injected directly into the desktop section.
    """
    context_parts: list[str] = []
    desktop_images: list[dict[str, str]] = []

    # Build search queries from profile data
    queries: list[tuple[str, str]] = []  # (label, query)

    # Use the current year so searches don't drift stale. Using the
    # literal year ensures DuckDuckGo returns recent results for rapidly
    # evolving topics like GPU driver support.
    from datetime import date as _date

    year = _date.today().year

    # GPU driver search
    gpus = getattr(profile, "hardware", None) and profile.hardware.gpus or []
    for gpu in gpus:
        if gpu.name:
            queries.append(("GPU driver", f"Linux driver support {gpu.name} {year}"))

    # WiFi search
    hw = getattr(profile, "hardware", None)
    if hw and hw.wifi_adapters:
        for adapter in hw.wifi_adapters[:1]:
            queries.append(("WiFi", f"Linux driver {adapter} compatibility"))

    # Distro search based on use cases
    prefs = getattr(profile, "preferences", None)
    use_cases = prefs.use_cases if prefs else []
    if use_cases:
        queries.append(
            ("Distro", f"best Linux distro for {' and '.join(use_cases[:3])} {year}")
        )

    # Desktop environment search based on look preference
    look = prefs.desktop_look if prefs else ""
    if look:
        look_desc = {
            "windows_like": "Windows-like taskbar",
            "macos_like": "macOS-like dock",
            "minimal": "minimal clean",
            "tiling": "tiling window manager",
        }.get(look, look)
        queries.append(("Desktop", f"Linux desktop environment {look_desc} screenshot"))

    # Execute text searches
    for label, query in queries:
        bridge.add_message("agent", f"Searching: {query}")
        result = _execute_web_search(query, max_results=4)
        try:
            data = json.loads(result)
            results = data.get("results", [])
            if results:
                context_parts.append(f'### Search: {label} — "{query}"')
                for r in results:
                    context_parts.append(f"- [{r['title']}]({r['url']}): {r['snippet'][:200]}")
                context_parts.append("")
        except (json.JSONDecodeError, KeyError):
            pass

    # Image searches for desktop visuals — run multiple to get variety
    image_queries: list[tuple[str, str]] = []
    if look:
        de_map = {
            "windows_like": [
                "KDE Plasma Windows 11 theme desktop screenshot",
                "Linux Windows-like desktop customization",
            ],
            "macos_like": [
                "GNOME macOS WhiteSur theme desktop screenshot",
                "Linux macOS style desktop customization",
            ],
            "minimal": [
                "minimal Linux desktop rice screenshot",
                "clean minimal GNOME desktop",
            ],
            "tiling": [
                "i3wm tiling desktop rice screenshot",
                "Hyprland tiling window manager desktop",
            ],
        }
        for q in de_map.get(look, [f"Linux {look} desktop screenshot"]):
            image_queries.append(("Desktop preview", q))

    # Also search for the icon style
    icon_style = prefs.icon_style if prefs else ""
    if icon_style and icon_style not in ("default", "custom"):
        image_queries.append(("Icon preview", f"Linux {icon_style} icon theme pack screenshot"))

    for label, query in image_queries:
        bridge.add_message("agent", f"Finding images: {query}")
        result = _execute_image_search(query, max_results=5)
        try:
            data = json.loads(result)
            images = data.get("images", [])
            for img in images:
                url = img.get("url", "")
                if not url:
                    continue
                # Skip URLs known to block hotlinking
                if any(
                    bad in url.lower()
                    for bad in ["imgur.com", "pinterest.", "instagram.", "facebook."]
                ):
                    continue
                desktop_images.append({"title": img.get("title", label), "url": url})
        except (json.JSONDecodeError, KeyError):
            pass

    # Validate image URLs — keep only those that actually load
    if desktop_images:
        import requests as img_req

        validated: list[dict[str, str]] = []
        for img in desktop_images:
            if len(validated) >= 4:
                break  # enough good images
            try:
                resp = img_req.head(
                    img["url"],
                    timeout=5,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    },
                )
                ct = resp.headers.get("content-type", "")
                if resp.status_code == 200 and "image" in ct:
                    validated.append(img)
            except Exception:
                pass
        desktop_images = validated

    return "\n".join(context_parts), desktop_images


def _build_section_prompt(
    profile: UserProfile,
    section: str,
    search_context: str,
    user_feedback: str = "",
    chosen_distro: str | None = None,
    vm_verify: bool = False,
) -> str:
    """Build a prompt for a single section of the migration analysis.

    ``chosen_distro`` lets the caller pass a distro decided earlier in the
    analysis (e.g. extracted from the ``distro`` section's output) so
    downstream sections (``plan``, ``desktop``, ``summary``) can receive the
    matching install primer. If ``chosen_distro`` is None, falls back to
    the user's stated ``preferences.distro_preference``.
    """
    from quickhatch.ai.install_primer import primer_prompt_block, resolve_primer

    profile_dict = asdict(profile)
    if profile_dict.get("ai_config", {}).get("api_key"):
        profile_dict["ai_config"]["api_key"] = "***REDACTED***"
    # Massive profiles (317 scanned apps = 47KB) choke local models even
    # on sections that don't care about apps. If the user picked a
    # specific apps_to_install list, trim the `apps` dump to only those
    # picks — the model doesn't need the full scan.
    picks = profile.apps_to_install or []
    if picks:
        pick_names = {p.lower() for p in picks}
        profile_dict["apps"] = [
            a for a in profile_dict.get("apps", []) if (a.get("name") or "").lower() in pick_names
        ]
    # Sections that don't need any app data at all can drop the list
    # entirely — it's pure prompt bloat for distro, drivers, desktop.
    if section in ("distro", "drivers", "desktop", "summary"):
        profile_dict["apps"] = []
    profile_json = json.dumps(profile_dict, indent=2, default=str)
    gallery_block = _gallery_prompt_block(profile)
    # Inject the curated library only on design-relevant sections
    # (desktop, summary). Also skip it entirely once the user has pinned
    # their own gallery picks — those go in via _gallery_prompt_block and
    # the 106-entry library just bloats the prompt from there on.
    #
    # With no picks, cap the library to the top N entries so a local
    # 26B model doesn't sit on a 13k-token prompt for 10 minutes.
    library_block = ""
    if section in ("desktop", "summary") and not profile.preferences.gallery_picks:
        library_block = _gallery_library_prompt_block(limit=20)
    # The summary section never needs the full library either way — it's a
    # recap, not a design source. Keep it stripped.
    if section == "summary":
        library_block = ""

    # Distro primer injection — only for sections where install knowledge
    # actually matters (plan = writes install steps; desktop = references
    # package manager + init system; summary = may cite the chosen stack).
    # Priority: explicit chosen_distro arg > user's stated preference.
    primer_block = ""
    resolved_distro = chosen_distro or profile.preferences.distro_preference or ""
    if section in ("plan", "desktop", "summary") and resolved_distro:
        if resolve_primer(resolved_distro) is not None:
            primer_block = primer_prompt_block(resolved_distro)

    primary_recipe = (
        profile.preferences.gallery_picks[0] if profile.preferences.gallery_picks else ""
    )

    section_instructions = {
        "distro": (
            "Recommend a Linux distribution for this user.\n\n"
            "HARD RULE: if the user's profile has `preferences.distro_preference` set\n"
            "to a non-empty value, that IS the distro. You MUST recommend exactly\n"
            "that distribution — not a derivative, fork, or 'superior alternative'.\n"
            "Do not suggest EndeavourOS when the user said Arch. Do not suggest\n"
            "Manjaro when the user said Arch. Do not suggest Linux Mint when the\n"
            "user said Ubuntu. The user's explicit choice is final. Your job in\n"
            "that case is to justify the fit and call out anything they should\n"
            "know, not to redirect them.\n\n"
            "Only recommend a different distro when `distro_preference` is empty\n"
            "or the stated value is not a real Linux distribution.\n\n"
            "Include: why it's a good fit for their hardware + use cases, the key\n"
            "advantages, any caveats (e.g. 'Arch has a rolling release so you need\n"
            "to update carefully'), and a download link to the official ISO.\n"
            "You MAY mention one or two alternatives as a short comparison table,\n"
            "but the primary recommendation must match `distro_preference` when set.\n"
        ),
        "apps": (
            "Map the user's Linux applications.\n\n"
            "SCOPE RULE: if the profile has a non-empty `apps_to_install` list,\n"
            "that is the ENTIRE set of apps you cover. Do not expand it. Do not\n"
            "add 'you'll also want…' suggestions. Do not list every scanned\n"
            "Windows app — the user already filtered. If they chose three apps,\n"
            "your table has three rows.\n\n"
            "If `apps_to_install` is empty, fall back to mapping everything in\n"
            "`apps` (the scan) that has a meaningful Linux equivalent.\n\n"
            "Use a Markdown TABLE with columns: | App | Install Command | Notes |\n"
            "The install command must be the EXACT command for the chosen distro\n"
            "(from the install primer above). Notes are for post-install config\n"
            "only — e.g. 'enable Proton in Steam settings', 'run git config --global\n"
            "user.email ...'. Skip apps that need no post-install setup rather than\n"
            "padding with 'works out of the box'.\n\n"
            "If an app in `apps_to_install` has no native package on the chosen\n"
            "distro, give the AUR/Flatpak/AppImage path — still only that one app,\n"
            "not a survey of packaging options.\n"
        ),
        "drivers": (
            "Provide driver guidance for their specific hardware.\n"
            "Cover: GPU (proprietary vs open-source), WiFi, Bluetooth, and any special peripherals.\n"
            "Include specific commands to install AND verify drivers work.\n"
            "Describe the end-state: what systemctl services need to be enabled, "
            "what kernel modules need to be loaded, what config files need editing.\n"
        ),
        "desktop": (
            "Recommend a desktop environment matching their preferences.\n"
            "Include: theme, icon pack, dock/panel setup, and wallpaper suggestions.\n"
            + (
                f"Recipe `{primary_recipe}` will be applied — your job is to personalize within it, "
                "not replace it with a different design direction.\n"
                "Respect the recipe's DE/theme/icon stack and only adapt wallpaper, accent choices, "
                "and app-specific additions.\n"
                if primary_recipe
                else ""
            )
            + (
                "CRITICAL — describe the full end-state, not just what to install:\n"
                "- How to ACTIVATE the theme (e.g. write /etc/xdg/kdeglobals with [Icons]\\nTheme=Papirus,\n"
                "  or use kwriteconfig6 --file kdeglobals --group Icons --key Theme Papirus)\n"
                "- How to set the wallpaper programmatically\n"
                "- How to enable the display manager (systemctl enable sddm/gdm/lightdm)\n"
                "- Any GTK theme config needed for non-KDE apps (gsettings, ~/.config/gtk-3.0/settings.ini)\n"
                "IMPORTANT: Include image URLs from the search results using Markdown image syntax:\n"
                "![Description](https://example.com/screenshot.png)\n"
            )
        ),
        "plan": (
            "Provide a complete step-by-step migration plan.\n"
            "Cover in order:\n"
            "1. Data backup strategy — MATCH the user's chosen `data_transfer_method`:\n"
            "   - 'external_drive': Windows → USB → Linux. List the specific dirs to copy.\n"
            "   - 'cloud': install the Linux sync client / CLI, authenticate, sync.\n"
            "   - 'keep_windows': give scp/rsync commands from Linux, or NTFS mount steps.\n"
            "   - 'nothing': skip this section entirely.\n"
            "2. Disk partitioning and encryption (specific partition sizes, filesystem choices)\n"
            "3. Installation steps (with EXACT commands where possible)\n"
            "4. User account creation — create the primary user with sudo rights in group `wheel`\n"
            "   (useradd -m -G wheel,video,audio,input -s /bin/bash USERNAME)\n"
            "5. Post-install CONFIGURATION (not just installation):\n"
            "   - Activate theme/icons/wallpaper (actual commands that apply them)\n"
            "   - Enable all required systemd services (NetworkManager, sshd, sddm, bluetooth, etc.)\n"
            "   - Configure GTK theme for non-KDE apps\n"
            "   - Set up shell defaults (.bashrc / .zshrc)\n"
            "   - Configure fonts (fontconfig)\n"
            "6. Verification commands that prove the end-state works\n"
            "Use numbered lists. Be specific about commands.\n"
            "REMEMBER: The goal is a WORKING, CONFIGURED desktop — not just installed packages.\n"
        ),
        "summary": (
            "Write a short, friendly summary card for the user.\n"
            "This is the LAST thing they see — make it feel like a confident handoff.\n"
            "Include:\n"
            "- A quick-reference table: | Item | Choice |\n"
            "  with rows for: Distro, Desktop, Theme, Icons, Filesystem, Encryption\n"
            "- A short 'What to do next' checklist (3-5 items)\n"
            "- An encouraging closing line\n"
            "Keep it SHORT — under 200 words. No long explanations.\n"
        ),
        "install_script": (
            "Generate ONE self-contained bash script that performs the FULL install\n"
            "of the chosen distro + desktop + apps + theming, to be run by the user\n"
            "on a LIVE USB environment with NO active AI agent. This is the offline,\n"
            "single-machine path — the user has no second PC, no agent loop, just\n"
            "this script.\n\n"
            "Respond with ONLY a bash script. No Markdown, no prose, no explanation,\n"
            "no ``` fences. The very first line must be `#!/usr/bin/env bash`.\n\n"
            "HARD REQUIREMENTS:\n"
            "- Fill in placeholders using the USER MIGRATION PROFILE below. Real\n"
            "  values, not templates. Hostname, username, timezone, locale, disk,\n"
            "  SSH public key, gallery recipe url, apps list — all concrete.\n"
            "- Follow the DISTRO INSTALL PRIMER above exactly. Its command sequence\n"
            "  is authoritative. Substitute the primer's <ANGLE_BRACKETS> with real\n"
            "  values from the profile.\n"
            "- Start with a `set -e` and a disk-wipe confirmation: show `lsblk`\n"
            "  output, prompt the user to retype the target disk path (e.g. they\n"
            "  type `/dev/nvme0n1` to confirm). Exit if the user doesn't match.\n"
            "- Prompt interactively ONLY for: disk confirmation, root password,\n"
            "  user password. Everything else is baked in.\n"
            "- Stage post-install configuration as a first-boot systemd oneshot\n"
            "  service that runs after reboot, self-disables when done, and logs\n"
            "  to /var/log/quickhatch-firstboot.log. The script writes that\n"
            "  follow-up script to /mnt/usr/local/bin/quickhatch-firstboot.sh\n"
            "  and enables its service via arch-chroot / nsenter / chroot as\n"
            "  appropriate for the distro.\n"
            "- The first-boot service does: pacman/apt/dnf -S the full DE +\n"
            "  apps + drivers from the plan, enable display manager, apply\n"
            "  gallery recipe dotfiles (clone + run install method), activate\n"
            "  theme/icons/wallpaper with the right config commands.\n"
            "- Reboot at the end of the main script with a 10-second countdown.\n"
            "- Be defensive: every network fetch has a retry loop. Every package\n"
            "  install has a fallback (update mirrors, retry).\n"
            "- Output progress messages the user can read (`echo '[*] Partitioning'`\n"
            "  / `echo '[+] Base install complete'`).\n"
            "- Keep the script under 400 lines. Density matters; verbose = harder\n"
            "  to audit.\n"
            "OUTPUT FORMAT: literally only the bash script. No surrounding text.\n"
        ),
    }

    # VM-aware block is only meaningful for the install_script section
    # (the only output that actually gets executed in the VM).
    vm_block = ""
    if vm_verify and section == "install_script":
        from quickhatch.wizard.vm_verify import vm_aware_prompt_block

        vm_block = vm_aware_prompt_block(True)

    return (
        "You are a Linux migration expert. Write the migration analysis section described below.\n\n"
        "KEY PRINCIPLE: The plan must describe the full END-STATE the user wants to reach,\n"
        "not just a list of packages to install. Installing a theme is not the same as activating it.\n"
        "Installing a package is not the same as configuring it. An automated agent will execute\n"
        "this plan — so it must be prescriptive about every config file, systemctl service,\n"
        "environment variable, and user account needed to reach the working desktop.\n\n"
        "Use rich Markdown formatting: **bold**, `code`, ### headings, bullet points, numbered lists, "
        "[links](url), and ![images](url) where appropriate.\n"
        "Respond with ONLY the section content — no JSON, no wrapping, just Markdown.\n\n"
        f"== SECTION: {section.upper()} ==\n"
        f"{section_instructions.get(section, '')}\n"
        f"== LIVE SEARCH RESULTS (use these for up-to-date info) ==\n{search_context}\n\n"
        + library_block
        + primer_block
        + gallery_block
        + (vm_block + "\n" if vm_block else "")
        + (
            f"== USER FEEDBACK (the user changed their mind — follow this!) ==\n{user_feedback}\n\n"
            if user_feedback
            else ""
        )
        + f"== USER SYSTEM PROFILE ==\n{profile_json}"
    )


def _call_openai_compatible(
    endpoint: str, model: str, prompt: str, api_key: str | None = None
) -> str:
    """Call an OpenAI-compatible chat completions API (works for LM Studio, OpenAI)."""
    import requests

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{endpoint.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    resp = requests.post(url, json=body, headers=headers, timeout=900)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_anthropic(api_key: str, model: str, prompt: str) -> str:
    """Call the Anthropic Messages API."""
    import requests

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=900,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


def _parse_agent_response(text: str) -> dict[str, dict]:
    """Parse the JSON response from the agent, tolerating markdown fences and quirks."""
    text = text.strip()

    # Strip markdown code fences anywhere in the text
    import re

    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract the outermost JSON object using brace counting
    # (handles nested braces in string values better than simple find/rfind)
    start = text.find("{")
    if start < 0:
        return {"plan": {"title": "Migration Analysis", "content": text}}

    depth = 0
    in_string = False
    escape_next = False
    end = -1
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end > start:
        json_str = text[start:end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Models often put literal newlines inside JSON strings — fix them
            # Replace unescaped newlines inside string values
            fixed = re.sub(
                r'(?<=": ")((?:[^"\\]|\\.)*)(?=")',
                lambda m: m.group(0).replace("\n", "\\n"),
                json_str,
            )
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

            # Last resort: try to fix ALL literal newlines between quotes
            # by replacing newlines that are inside JSON string values
            lines = json_str.split("\n")
            joined = ""
            in_str = False
            for line in lines:
                count = line.count('"') - line.count('\\"')
                if in_str:
                    joined += "\\n" + line
                else:
                    joined += ("\n" if joined else "") + line
                if count % 2 == 1:
                    in_str = not in_str
            try:
                return json.loads(joined)
            except json.JSONDecodeError:
                pass

    return {"plan": {"title": "Migration Analysis", "content": text}}


def _call_model_simple(endpoint: str, model: str, prompt: str, api_key: str | None) -> str:
    """Simple single-turn LLM call (no tools). Works for any provider."""
    import requests

    if "anthropic" in (endpoint or ""):
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key or "",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=900,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    else:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(
            f"{endpoint.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 4096,
            },
            headers=headers,
            timeout=900,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _run_agent_background(gui_state: GUIState, port: int, user_feedback: str = "") -> None:
    """Run the AI agent in a background thread, posting results to the bridge.

    Strategy: pre-fetch live search results, then call the model once per section.
    This avoids relying on tool-use support and produces clean Markdown output.
    """
    import requests as req

    base = f"http://localhost:{port}"
    bridge = gui_state.bridge
    config = gui_state.profile.ai_config

    try:
        bridge.add_message("agent", "Connected — reading your profile.")

        # Phase 1: Pre-fetch search results and desktop images
        bridge.add_message("agent", "Gathering live data from the web…")
        search_context, desktop_images = _prefetch_search_context(gui_state.profile, bridge)

        if desktop_images:
            bridge.add_message("agent", f"Found {len(desktop_images)} desktop preview images.")

        endpoint = config.endpoint or ""
        model = config.model_name or ""
        api_key = config.api_key

        # Phase 2: Generate each section with its own model call
        section_titles = {
            "distro": "Recommended Distribution",
            "apps": "Application Mapping",
            "drivers": "Driver Notes",
            "desktop": "Desktop Environment",
            "plan": "Migration Plan",
            "summary": "Your Migration at a Glance",
        }
        # Add a self-install bash script when the user said they don't have
        # a second machine available — the wizard will ship it as
        # install.sh in the export bundle so they can run it offline on
        # the live USB, no agent needed.
        if (gui_state.profile.preferences.another_machine or "").strip().lower() == "no":
            section_titles["install_script"] = "Offline Install Script"

        # ``chosen_distro`` is the distro identifier the agent settled on
        # in the distro section. It gets threaded into subsequent section
        # prompts so they can pull in the right install primer.
        chosen_distro: str | None = None
        # Seed from user preference if they stated one; the distro section's
        # output may confirm or override this.
        if gui_state.profile.preferences.distro_preference:
            chosen_distro = gui_state.profile.preferences.distro_preference

        # Build a map of sections that already have a SUCCESSFUL suggestion
        # on disk from a previous run. If the user is re-running without
        # having cleared state (e.g. after a server crash / restart), we
        # skip those sections entirely and only redo the ones that failed
        # or never ran. Placeholder '(failed)' suggestions don't count.
        already_done: set[str] = set()
        for s in bridge.suggestions:
            cat = getattr(s, "category", "")
            title_ok = not (getattr(s, "title", "") or "").endswith("(failed)")
            if cat and title_ok:
                already_done.add(cat)

        for section_key, title in section_titles.items():
            if bridge.stop_requested:
                bridge.add_message("agent", "Stopped.")
                req.post(
                    f"{base}/api/done",
                    json={"summary": "Analysis stopped by user."},
                    timeout=5,
                )
                return
            if section_key in already_done:
                bridge.add_message(
                    "agent",
                    f"✓ {title} already generated — skipping (you can edit it manually or "
                    f"rerun the whole analysis to regenerate everything).",
                )
                continue
            # If a FAILED placeholder exists for this section, drop it so
            # the new attempt can replace it cleanly.
            bridge.suggestions[:] = [
                s
                for s in bridge.suggestions
                if not (
                    getattr(s, "category", "") == section_key
                    and (getattr(s, "title", "") or "").endswith("(failed)")
                )
            ]
            bridge.add_message("agent", f"Generating {title.lower()}…")

            prompt = _build_section_prompt(
                gui_state.profile,
                section_key,
                search_context,
                user_feedback,
                chosen_distro=chosen_distro,
                vm_verify=bool(getattr(gui_state, "vm_verify_enabled", False)),
            )
            # Retry once on any network/timeout error — a 15 min timeout plus
            # a second attempt is usually enough for even the biggest local
            # models. If the second try still fails, post an error suggestion
            # for this section and continue with the next rather than
            # crashing the whole analysis run.
            try:
                content = _call_model_simple(endpoint, model, prompt, api_key)
            except Exception as first_exc:
                bridge.add_message(
                    "agent",
                    f"⚠️  {title} timed out, retrying once after a short pause…",
                )
                # 10s backoff so a struggling local model has breathing room
                # (LM Studio sometimes hangs briefly while unloading context).
                import time as _t

                _t.sleep(10)
                try:
                    content = _call_model_simple(endpoint, model, prompt, api_key)
                except Exception as second_exc:
                    bridge.add_message(
                        "agent",
                        f"❌  {title} failed twice — skipping. Last error: "
                        f"{str(second_exc)[:200]}",
                    )
                    req.post(
                        f"{base}/api/suggest",
                        json={
                            "category": section_key,
                            "title": f"{title} (failed)",
                            "content": (
                                f"This section couldn't be generated after two "
                                f"attempts.\n\n**Errors:**\n- First: `{str(first_exc)[:150]}`\n"
                                f"- Second: `{str(second_exc)[:150]}`\n\n"
                                f"You can rerun the analysis with this message as "
                                f"feedback to try again."
                            ),
                        },
                        timeout=5,
                    )
                    continue

            # Strip any residual markdown fences the model might wrap around its response
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines)

            # After the distro section runs, scan its output for a known
            # distro identifier. Whichever our primer library recognizes
            # becomes the ``chosen_distro`` for downstream sections (plan,
            # desktop, summary) so they get the matching install primer.
            # Only overrides when the section actually found a match — if
            # the distro section is vague, fall back to whatever was seeded
            # from user preference.
            if section_key == "distro":
                from quickhatch.ai.install_primer import resolve_primer

                match = resolve_primer(content)
                if match is not None:
                    chosen_distro = match["canonical"]
                    bridge.add_message(
                        "agent",
                        f"Distro resolved: {chosen_distro} — loading install primer "
                        "for downstream sections.",
                    )

            # Inject desktop preview images into the desktop section
            if section_key == "desktop" and desktop_images:
                content += "\n\n### Desktop Preview\n"
                content += "Here's what your customized desktop can look like:\n\n"
                for img in desktop_images[:6]:
                    content += f"![{img['title']}]({img['url']})\n\n"

            req.post(
                f"{base}/api/suggest",
                json={
                    "category": section_key,
                    "title": title,
                    "content": content,
                },
                timeout=5,
            )

        req.post(
            f"{base}/api/done",
            json={"summary": "Analysis done. Review the recommendations below."},
            timeout=5,
        )

    except Exception as e:
        bridge.add_message("agent", f"Error: {e}")


def _launch_agent(
    gui_state: GUIState, port: int, user_feedback: str = "", phase: str = "analysis"
) -> dict:
    """Launch the AI agent — calls the configured API in a background thread."""
    config = gui_state.profile.ai_config
    provider = config.provider or "api"

    if provider == "codex":
        import subprocess
        import sys

        prompt = _build_section_prompt(gui_state.profile, "plan", "")
        prompt_file = Path.home() / ".quickhatch-agent-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        try:
            if sys.platform == "win32":
                ps_file = Path.home() / ".quickhatch-launch-agent.ps1"
                ps_content = (
                    f'$p = Get-Content "{prompt_file}" -Raw\n'
                    f"codex $p\n"
                    f'Read-Host "Press Enter to close"\n'
                )
                ps_file.write_text(ps_content, encoding="utf-8")
                subprocess.Popen(
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        f'Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","{ps_file}"',
                    ]
                )
            else:
                shell_cmd = f'codex "$(cat {prompt_file})"'
                for term_cmd in [
                    ["x-terminal-emulator", "-e", "bash", "-c", shell_cmd],
                    ["gnome-terminal", "--", "bash", "-c", shell_cmd],
                    ["konsole", "-e", "bash", "-c", shell_cmd],
                    ["xterm", "-e", "bash", "-c", shell_cmd],
                ]:
                    try:
                        subprocess.Popen(term_cmd)
                        break
                    except FileNotFoundError:
                        continue
                else:
                    subprocess.Popen(["bash", "-c", shell_cmd])
            return {"status": "ok", "message": "Codex launched in a new terminal."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # For API-based providers (LM Studio, Anthropic, OpenAI), run in background
    if not config.endpoint:
        return {"status": "error", "message": "No API endpoint configured. Go back to AI setup."}

    # Check if this is a setup-phase launch (SSH into Linux machine)
    if phase == "setup":
        thread = threading.Thread(
            target=_run_setup_agent,
            args=(gui_state, port),
            daemon=True,
        )
        thread.start()
        return {"status": "ok", "message": "Setup agent started! Connecting via SSH..."}

    thread = threading.Thread(
        target=_run_agent_background,
        args=(gui_state, port),
        kwargs={"user_feedback": user_feedback},
        daemon=True,
    )
    thread.start()
    return {"status": "ok", "message": "Agent started! Analyzing your system..."}


def _extract_json_objects(text: str) -> list[dict]:
    """Extract one or more top-level JSON objects from text.

    Handles cases where the model emits multiple JSON objects in one response
    (e.g. a status followed by an ssh_exec), possibly with text between them.
    """
    objects: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        # Try to find a matching closing brace with proper string handling
        depth = 0
        in_string = False
        escape_next = False
        start = i
        end = -1
        for j in range(i, n):
            c = text[j]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end > start:
            try:
                obj = json.loads(text[start:end])
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                pass
            i = end
        else:
            i += 1
    return objects


def _ssh_exec(
    host: str, user: str, cmd: str, key_path: str | None = None, timeout: int = 120, port: int = 22
) -> tuple[int, str, str]:
    """Execute a command on a remote host via SSH. Returns (returncode, stdout, stderr)."""
    import subprocess

    ssh_args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        "-p",
        str(port),
    ]
    if key_path:
        ssh_args += ["-i", key_path]
    ssh_args += [f"{user}@{host}", cmd]

    try:
        result = subprocess.run(
            ssh_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", "ssh not found — install OpenSSH"


# Shell script shipped to the target to detect whether we're booted from a
# live ISO or an installed system. Prints the first positive signal or
# "INSTALLED" if no live markers matched.
_LIVE_ENV_DETECTION_SCRIPT = r"""
if grep -qE '\\b(boot=live|live-media|toram|rd\\.live\\.image|archisolabel|archisodevice|boot=casper)\\b' /proc/cmdline 2>/dev/null; then
    echo 'LIVE:cmdline'
    exit 0
fi
for marker in /run/live /rofs /cdrom/casper /run/archiso /lib/live /run/calamares; do
    if [ -d "$marker" ]; then
        echo "LIVE:marker=$marker"
        exit 0
    fi
done
if mount 2>/dev/null | grep -qE ' on / type (overlay|aufs|squashfs|tmpfs)\b'; then
    echo 'LIVE:overlay-root'
    exit 0
fi
# Live ISOs commonly auto-login as these users. Distinguish from a real
# installed user account by also checking for an unsquashfs or live-config
# utility, which installed systems usually don't carry.
case "$(whoami)" in
    liveuser|live|kali|parrot|manjaro|endeavour|artix|voidlive)
        if command -v unsquashfs >/dev/null 2>&1 || [ -x /usr/lib/live/boot ] || [ -d /run/initramfs/live ]; then
            echo 'LIVE:user'
            exit 0
        fi
        ;;
esac
echo 'INSTALLED'
"""


def _detect_live_env(
    host: str, user: str, key_path: str | None, port: int = 22
) -> tuple[bool, str]:
    """Probe the target for live-ISO signals over SSH.

    Returns ``(is_live, marker)``. ``marker`` is a short string naming
    which signal triggered (useful for the bridge message), or the
    literal ``"INSTALLED"`` / ``"detect-failed"`` on the negative path.
    """
    rc, out, _ = _ssh_exec(host, user, _LIVE_ENV_DETECTION_SCRIPT, key_path, timeout=30, port=port)
    if rc != 0:
        return False, "detect-failed"
    line = out.strip().splitlines()[-1] if out.strip() else ""
    if line.startswith("LIVE:"):
        return True, line
    return False, "INSTALLED"


def _wait_for_ssh_reconnect(
    host: str,
    user: str,
    key_path: str | None,
    port: int = 22,
    *,
    timeout: int = 300,
    interval: int = 5,
    stop_flag_getter=None,
) -> bool:
    """Poll SSH until the target accepts a connection again, or ``timeout``
    seconds elapse. Returns ``True`` on reconnect.

    ``stop_flag_getter`` is an optional zero-arg callable that returns True
    when the user has pressed Stop; allows abandoning the wait loop.
    """
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if stop_flag_getter is not None and stop_flag_getter():
            return False
        rc, out, _ = _ssh_exec(host, user, "echo __reconnected__", key_path, timeout=10, port=port)
        if rc == 0 and "__reconnected__" in out:
            return True
        _time.sleep(interval)
    return False


# Commands that trigger the reconnect-after-reboot dance. Matched
# conservatively — the agent's "reboot" is distinct from other uses of
# the word (e.g. config file contents mentioning reboot).
_REBOOT_COMMAND_RE = re.compile(
    r"(^|;|&&|\|\||\s|\bsudo\s+)"
    r"(reboot|shutdown\s+-r|systemctl\s+reboot|init\s+6|telinit\s+6)"
    r"(\s|;|&&|$)"
)


def _is_reboot_command(cmd: str) -> bool:
    """True if ``cmd`` will reboot the target (triggering a reconnect loop).

    Pure-comment lines (starting with ``#``) are explicitly rejected — a
    false positive there would leave the agent stuck waiting for a
    reconnect that never comes.
    """
    stripped = cmd.lstrip()
    if stripped.startswith("#"):
        return False
    return bool(_REBOOT_COMMAND_RE.search(cmd))


def _run_setup_agent(gui_state: GUIState, port: int) -> None:
    """Run the setup phase as an agentic loop: SSH into the Linux machine,
    let the LLM decide what to run, execute it, feed results back, repeat.

    The model maintains full conversation history and adapts to errors,
    unexpected state, and changing conditions on the remote machine.
    """
    import requests as req

    base = f"http://localhost:{port}"
    bridge = gui_state.bridge
    config = gui_state.profile.ai_config
    profile = gui_state.profile

    host = getattr(profile, "linux_ip", "") or ""
    user = getattr(profile, "linux_user", "") or ""
    ssh_port = 22

    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        try:
            ssh_port = int(port_str)
        except ValueError:
            pass

    key_path = None
    if profile.ssh_public_key_path:
        priv = profile.ssh_public_key_path.replace(".pub", "")
        if Path(priv).exists():
            key_path = priv
    if not key_path:
        default_key = Path.home() / ".ssh" / "id_ed25519"
        if default_key.exists():
            key_path = str(default_key)

    if not host or not user:
        bridge.add_message("agent", "Error: No Linux IP/username configured.")
        return

    try:
        # Step 1: Test SSH connectivity
        bridge.add_message("agent", f"Connecting to {user}@{host}:{ssh_port}…")
        rc, out, err = _ssh_exec(
            host, user, "echo 'QuickHatch connected!' && uname -a", key_path, port=ssh_port
        )
        if rc != 0:
            bridge.add_message("agent", f"SSH connection failed: {err.strip()}")
            return
        bridge.add_message("agent", f"Connected — {out.strip()}")

        # Step 2: Gather initial system info
        bridge.add_message("agent", "Gathering remote system info…")
        info_cmd = (
            "echo '=== OS ==='; cat /etc/os-release 2>/dev/null; "
            "echo '=== DISK ==='; df -h / 2>/dev/null; "
            "echo '=== MEMORY ==='; free -h 2>/dev/null; "
            "echo '=== LSPCI GPU ==='; lspci 2>/dev/null | grep -i vga; "
            "echo '=== NETWORK ==='; ip link show 2>/dev/null | grep -E '^[0-9]|state'; "
            "echo '=== PACKAGE MANAGER ==='; "
            "command -v pacman >/dev/null && echo 'pacman (Arch)'; "
            "command -v apt >/dev/null && echo 'apt (Debian/Ubuntu)'; "
            "command -v dnf >/dev/null && echo 'dnf (Fedora)'; "
            "echo '=== USER ==='; whoami; id"
        )
        rc, system_info, _ = _ssh_exec(host, user, info_cmd, key_path, port=ssh_port)
        system_info = system_info.strip() if rc == 0 else "Could not gather system info"

        # Step 2b: Detect whether we're in a live ISO or an installed system.
        # If LIVE, the agent's first job is to install the distro using the
        # primer; if INSTALLED, it skips install and goes straight to config.
        bridge.add_message("agent", "Detecting live-ISO vs installed system…")
        is_live, live_marker = _detect_live_env(host, user, key_path, port=ssh_port)
        if is_live:
            bridge.add_message(
                "agent",
                f"Live environment detected ({live_marker}) — agent will install the "
                "distro first, then reboot and continue with configuration.",
            )
        else:
            bridge.add_message("agent", "Installed system — skipping install phase.")

        # Step 3: Build profile context
        profile_dict = asdict(profile)
        if profile_dict.get("ai_config", {}).get("api_key"):
            profile_dict["ai_config"]["api_key"] = "***REDACTED***"
        profile_json = json.dumps(profile_dict, indent=2, default=str)

        endpoint = config.endpoint or ""
        model = config.model_name or ""
        api_key = config.api_key

        # Step 4a: Load the distro-specific install primer.
        #
        # Priority for which distro's primer to load:
        #   1. User's explicitly stated preference (preferences.distro_preference)
        #   2. The distro section output from the analysis phase — if the
        #      user left their preference blank, the agent recommended a
        #      distro during analysis and that recommendation is what the
        #      remote-setup agent should align with.
        from quickhatch.ai.install_primer import primer_prompt_block, resolve_primer

        distro_hint = (profile.preferences.distro_preference or "").strip()
        if not distro_hint and bridge.suggestions:
            # Look for the distro-category suggestion first, then any
            # suggestion whose title hints at a distro recommendation.
            for s in bridge.suggestions:
                if getattr(s, "category", "") == "distro" or "distribut" in (s.title or "").lower():
                    match = resolve_primer(s.content or "")
                    if match is not None:
                        distro_hint = match["canonical"]
                        bridge.add_message(
                            "agent",
                            f"Using distro recommendation from analysis: {distro_hint}",
                        )
                        break
        distro_primer_md = primer_prompt_block(distro_hint)

        # Step 4b: Gather any existing migration plan from the analysis phase
        migration_plan_md = ""
        if bridge.suggestions:
            plan_parts = [
                "The migration plan has already been generated during the analysis phase.\n"
                "Use this as your blueprint — do not re-plan from scratch.\n"
            ]
            for s in bridge.suggestions:
                plan_parts.append(f"\n### {s.title}\n{s.content}")
            migration_plan_md = "\n".join(plan_parts)

        # Step 5: Build system prompt for the agentic loop
        # Live-env mode block — injected only when we detected a live ISO.
        # Tells the LLM its first job is to install the distro from scratch
        # using the primer, then reboot. The reconnect-after-reboot logic
        # in the command loop handles the SSH handoff automatically.
        live_env_block = ""
        if is_live:
            live_env_block = (
                "\n== LIVE ENVIRONMENT MODE ==\n"
                f"Detection signal: {live_marker}\n\n"
                "You are currently in a LIVE USB environment. The target distro is NOT\n"
                "installed on disk yet. Your FIRST job is to do a fresh installation\n"
                "using the DISTRO INSTALL PRIMER above, THEN continue with the post-\n"
                "install configuration. Concretely:\n\n"
                "1. Confirm the target disk with the user (bridge chat) before ANY\n"
                "   partitioning. Show `lsblk -f` output so they can see what's\n"
                "   there. Never wipe without explicit confirmation.\n"
                "2. Follow the install primer step-by-step: partition, mkfs, mount,\n"
                "   base install (pacstrap / debootstrap / dnf --installroot / etc.),\n"
                "   chroot config.\n"
                "3. During chroot config, **pre-install the SSH setup** for the\n"
                "   installed system so we can reconnect after reboot. That means:\n"
                "     - Install openssh-server / openssh in the target rootfs\n"
                "     - Enable sshd for first boot (systemctl / rc-update / sv)\n"
                "     - Create the user account that will own the installation\n"
                "     - Copy this SSH key into that user's authorized_keys with\n"
                "       correct permissions (0700 ~/.ssh, 0600 authorized_keys)\n"
                f"     - The public key to install is already known — it's the one\n"
                f"       authenticating THIS session, so you can pull it from the\n"
                f"       live env's /root/.ssh/authorized_keys or /home/{user}/.ssh/\n"
                f"       authorized_keys file on the current (live) system.\n"
                "4. Install the bootloader (grub / systemd-boot per the primer).\n"
                "5. Issue `reboot`. QuickHatch auto-detects reboot commands and\n"
                "   polls for the installed system's sshd to come back up — you\n"
                "   will transparently reconnect; just continue from the next\n"
                "   command as if nothing happened.\n"
                "6. After reconnect, proceed to post-install config (desktop env,\n"
                "   drivers, theming, apps) as described below.\n\n"
                "IMPORTANT: If the user's preferences or migration plan specify a\n"
                "user account name or password, use those. Otherwise ask the user\n"
                "via the bridge chat once, early; do not invent credentials silently.\n\n"
            )

        system_prompt = (
            "You are a Linux system administrator. You are connected to a remote machine via SSH "
            "and your job is to execute the migration plan (provided below) on the remote machine.\n\n"
            "You have ONE tool: ssh_exec — it runs a shell command on the remote machine and returns the output.\n"
            "To use it, respond with a JSON object:\n"
            '  {"action": "ssh_exec", "command": "your shell command here", "reason": "why you are running this"}\n\n'
            "When you are completely done with the entire setup, respond with:\n"
            '  {"action": "done", "summary": "brief summary of what was accomplished"}\n\n'
            "To post a progress update to the user (shown in the UI), respond with:\n"
            '  {"action": "status", "message": "what you are doing now"}\n\n'
            "CORE PRINCIPLE:\n"
            "Your goal is a WORKING, CONFIGURED system — not just installed packages.\n"
            "Installing a theme is NOT the same as activating it. Installing a package is NOT the\n"
            "same as configuring it. Think about what the user will actually SEE and USE at the end,\n"
            "and work backwards to make that happen.\n\n"
            "RULES:\n"
            "- Run ONE command at a time, then analyze the output before deciding the next step\n"
            "- If a command fails, diagnose WHY from the error output and fix it — don't just retry\n"
            "- Use non-interactive flags always (--noconfirm, -y, DEBIAN_FRONTEND=noninteractive, etc.)\n"
            "- You are logged in as the user shown in the system info — use sudo only if needed\n"
            "- Follow the migration plan above, but ADAPT to actual conditions (e.g. disk space, missing packages)\n"
            "- If disk space is low, address that FIRST before installing packages\n"
            "- After installing major components, VERIFY they work AND are activated before moving on\n"
            "- For desktop themes/icons: ALWAYS activate them in the system config after installing\n"
            "  (e.g. write /etc/xdg/kdeglobals for KDE, /etc/dconf/db/local.d/ for GNOME)\n"
            "- Create a regular user with sudo rights (group `wheel`) — don't leave users on root-only\n"
            "- Enable all required systemd services (display manager, NetworkManager, bluetooth, etc.)\n\n"
            "EXECUTION ORDER (post-install, after any live-env install completes):\n"
            "1. Prepare the system (expand disk/tmpfs if needed, update packages)\n"
            "2. Install and configure GPU drivers (verify with nvidia-smi, glxinfo, etc.)\n"
            "3. Install the desktop environment AND activate theme/icons system-wide\n"
            "4. Install all application equivalents from the plan\n"
            "5. Create a primary user with sudo rights and proper groups (live-env installs do this step earlier, during chroot)\n"
            "6. Apply system configuration (shell, fonts, display manager, services)\n"
            "7. Run final verification: theme is active, services enabled, user can log in\n\n"
            f"== REMOTE SYSTEM INFO ==\n{system_info}\n\n"
            + live_env_block
            + distro_primer_md
            + (
                f"== MIGRATION PLAN (pre-generated) ==\n{migration_plan_md}\n\n"
                if migration_plan_md
                else ""
            )
            + f"== USER MIGRATION PROFILE ==\n{profile_json}"
        )

        # Step 5: Agentic conversation loop
        messages: list[dict[str, str]] = [
            {"role": "user", "content": system_prompt},
        ]

        MAX_ITERATIONS = 100
        results_log: list[str] = []  # track all commands and results for the final summary

        for iteration in range(MAX_ITERATIONS):
            # Honor Stop requests from the UI
            if bridge.stop_requested:
                bridge.add_message("agent", "Stopped.")
                req.post(
                    f"{base}/api/done",
                    json={"summary": "Agent stopped by user."},
                    timeout=5,
                )
                return
            # Cap conversation history to keep prompts under the model's
            # context window. Local models like Gemma-4-26b crash with
            # HTTP 400 once the cumulative history exceeds ~32K tokens.
            # Keep the system prompt (first message) + the last N turns.
            _HISTORY_KEEP = 24  # system + last 23 turns
            if len(messages) > _HISTORY_KEEP:
                system_msg = messages[0]
                tail = messages[-(_HISTORY_KEEP - 1) :]
                summary_msg = {
                    "role": "user",
                    "content": (
                        f"[History pruned — earlier {len(messages) - _HISTORY_KEEP} "
                        f"messages trimmed to stay within context. You have successfully "
                        f"completed several steps already; continue with the next step "
                        f"based on the recent history below and the current system state.]"
                    ),
                }
                messages = [system_msg, summary_msg, *tail]

            # Call the LLM
            if "anthropic" in endpoint:
                response_text = _call_anthropic(
                    api_key, model, system_prompt if iteration == 0 else "continue"
                )
                # For anthropic, we'd need to manage messages differently — simplified here
            else:
                import requests

                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                resp = requests.post(
                    f"{endpoint.rstrip('/')}/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.3,
                        "max_tokens": 1024,
                    },
                    headers=headers,
                    timeout=900,
                )
                resp.raise_for_status()
                response_text = resp.json()["choices"][0]["message"]["content"]

            # Append assistant response to history
            messages.append({"role": "assistant", "content": response_text})

            # Parse actions from the response — there may be multiple JSON objects
            response_text = response_text.strip()
            # Strip markdown fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                response_text = "\n".join(lines)

            actions = _extract_json_objects(response_text)

            if not actions:
                bridge.add_message("agent", f"Model response (non-JSON): {response_text[:200]}")
                messages.append(
                    {
                        "role": "user",
                        "content": 'Please respond with a valid JSON object as specified. Either {"action": "ssh_exec", "command": "..."} or {"action": "done", "summary": "..."}',
                    }
                )
                continue

            # Process each action in order. Feed a consolidated user message
            # back to the model at the end so history stays aligned.
            pending_feedback: list[str] = []
            finished = False

            for action in actions:
                act = action.get("action", "")

                if action.get("message"):
                    bridge.add_message("agent", action["message"])

                if act == "done":
                    summary = action.get("summary", "Setup complete.")
                    bridge.add_message("agent", f"Finished: {summary}")
                    log_md = "### Execution Log\n\n"
                    for entry in results_log:
                        log_md += entry + "\n"
                    req.post(
                        f"{base}/api/suggest",
                        json={
                            "category": "execution_log",
                            "title": "Setup Execution Log",
                            "content": log_md,
                        },
                        timeout=5,
                    )
                    req.post(f"{base}/api/done", json={"summary": summary}, timeout=5)
                    finished = True
                    break

                if act == "ssh_exec":
                    cmd = action.get("command", "")
                    reason = action.get("reason", "")
                    if not cmd:
                        pending_feedback.append(
                            "Error: empty command. Provide a valid shell command."
                        )
                        continue

                    bridge.add_message("agent", f"`{cmd}`" + (f" — {reason}" if reason else ""))

                    # Reboot commands tear down the live-env SSH session.
                    # Fire the command (short timeout — we expect it to
                    # disconnect us), then poll for SSH on the installed
                    # system to come back up. If the user configured a
                    # new login during install, they should have set
                    # linux_user accordingly in the wizard; we reconnect
                    # with whatever `user` is set to now.
                    if _is_reboot_command(cmd):
                        bridge.add_message(
                            "agent",
                            "Reboot issued — waiting for the target to come back online "
                            "(up to 5 min)…",
                        )
                        _ssh_exec(
                            host, user, cmd, key_path, timeout=15, port=ssh_port
                        )  # fire + disconnect
                        reconnected = _wait_for_ssh_reconnect(
                            host,
                            user,
                            key_path,
                            port=ssh_port,
                            timeout=300,
                            interval=5,
                            stop_flag_getter=lambda: bridge.stop_requested,
                        )
                        if not reconnected:
                            bridge.add_message(
                                "agent",
                                "Timed out waiting for reconnect. The install may have "
                                "failed, network may be down, or the installed system's "
                                "sshd was not enabled. Stopping.",
                            )
                            results_log.append(f"**`{cmd}`** — TIMEOUT (no reconnect)")
                            break
                        bridge.add_message(
                            "agent",
                            "Reconnected to the installed system. Continuing with "
                            "post-install configuration.",
                        )
                        # After the first successful reconnect we are no
                        # longer in a live env — flip the flag so downstream
                        # prompt/logic treats the session as installed.
                        is_live = False
                        # Gather fresh system info so the next turn sees
                        # the installed-system reality.
                        fresh_rc, fresh_info, _ = _ssh_exec(
                            host,
                            user,
                            "cat /etc/os-release 2>/dev/null; echo '===USER==='; whoami; id",
                            key_path,
                            port=ssh_port,
                        )
                        if fresh_rc == 0:
                            pending_feedback.append(
                                f"[post-reboot system info]\n{fresh_info.strip()}"
                            )
                        results_log.append(f"**`{cmd}`** — OK (reconnected)")
                        continue

                    rc, out, err = _ssh_exec(host, user, cmd, key_path, timeout=300, port=ssh_port)

                    result_parts = [f"[result for: {cmd}]", f"Exit code: {rc}"]
                    if out.strip():
                        stdout_text = out.strip()
                        if len(stdout_text) > 2000:
                            stdout_text = (
                                stdout_text[:1000] + "\n... (truncated) ...\n" + stdout_text[-500:]
                            )
                        result_parts.append(f"STDOUT:\n{stdout_text}")
                    if err.strip():
                        stderr_text = err.strip()
                        if len(stderr_text) > 1000:
                            stderr_text = (
                                stderr_text[:500] + "\n... (truncated) ...\n" + stderr_text[-300:]
                            )
                        result_parts.append(f"STDERR:\n{stderr_text}")

                    pending_feedback.append("\n".join(result_parts))

                    status = "OK" if rc == 0 else f"FAILED ({rc})"
                    results_log.append(f"**`{cmd}`** — {status}")
                    if rc != 0 and err.strip():
                        results_log.append(f"  Error: {err.strip()[:150]}")

                    if len(results_log) % 5 == 0:
                        req.post(
                            f"{base}/api/suggest",
                            json={
                                "category": "progress",
                                "title": f"Progress ({len(results_log)} commands executed)",
                                "content": "\n".join(results_log[-10:]),
                            },
                            timeout=5,
                        )
                    continue

                if act == "status":
                    # status-only — no feedback needed
                    continue

                pending_feedback.append(f"Unknown action '{act}'. Use ssh_exec, status, or done.")

            if finished:
                return

            # Send consolidated feedback back to the model
            if pending_feedback:
                messages.append({"role": "user", "content": "\n\n".join(pending_feedback)})
            else:
                # Only status messages — prompt the model to continue
                messages.append({"role": "user", "content": "OK, continue with the next step."})

        # Max iterations reached
        bridge.add_message("agent", "Warning: Maximum iterations reached. Stopping.")
        req.post(
            f"{base}/api/done",
            json={
                "summary": f"Setup stopped after {MAX_ITERATIONS} iterations. Review the log for details."
            },
            timeout=5,
        )

    except Exception as e:
        bridge.add_message("agent", f"Error: {e}")


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


def _locate_install_script(gui_state: GUIState) -> Path | None:
    """Find the install.sh to verify — exported bundle first, then suggestion."""
    export_dir = Path.home() / "quickhatch-export"
    if (export_dir / "install.sh").exists():
        return export_dir / "install.sh"
    for s in gui_state.bridge.suggestions:
        if getattr(s, "category", "") == "install_script" and (s.content or "").strip():
            import tempfile as _tmp

            tmp = Path(_tmp.gettempdir()) / "quickhatch-install.sh"
            tmp.write_text(s.content.strip(), encoding="utf-8")
            return tmp
    return None


def _load_public_key(gui_state: GUIState) -> str:
    """Return the user's SSH public key content, or '' if not generated yet."""
    pub_path = gui_state.profile.ssh_public_key_path
    if pub_path and Path(pub_path).exists():
        return Path(pub_path).read_text(encoding="utf-8").strip()
    # Fall back to the default key location
    default = Path.home() / ".ssh" / "id_ed25519.pub"
    if default.exists():
        return default.read_text(encoding="utf-8").strip()
    return ""


def _resolve_private_key_path(gui_state: GUIState) -> str | None:
    """Return the private-key path that matches the public key we seed into the VM.

    QuickHatch generates its key at ~/.ssh/quickhatch_ed25519{.pub} — a
    non-default filename ssh won't discover on its own. We must pass it
    explicitly via `-i` or scp/ssh will try only the standard names
    (id_ed25519, id_rsa, …) and fail publickey auth.
    """
    pub_path = gui_state.profile.ssh_public_key_path
    if pub_path and pub_path.endswith(".pub"):
        priv = pub_path[:-4]
        if Path(priv).exists():
            return priv
    default = Path.home() / ".ssh" / "id_ed25519"
    if default.exists():
        return str(default)
    return None


def _start_vm_assisted(gui_state: GUIState, iso_path: str) -> dict:
    """Kick off phase A of the user-in-loop VM verify flow (synchronous).

    Creates + starts the VM in GUI mode. Returns the paste snippet the
    user needs to run inside the VM's root shell. Keeps the session
    alive on gui_state until /wizard/vm-verify/finish or /cancel.
    """
    from quickhatch.wizard.vm_verify import start_assisted_session

    script = _locate_install_script(gui_state)
    if script is None:
        return {
            "status": "error",
            "message": "No install.sh yet — run the analysis agent first.",
        }
    pubkey = _load_public_key(gui_state)
    if not pubkey:
        return {
            "status": "error",
            "message": "No SSH key yet — generate one on the SSH step first.",
        }

    bridge = gui_state.bridge

    def progress(msg: str) -> None:
        bridge.add_message("agent", f"[vm] {msg}")

    session, err = start_assisted_session(
        script_path=script,
        iso_path=Path(iso_path),
        public_key=pubkey,
        ssh_key=_resolve_private_key_path(gui_state),
        progress=progress,
    )
    if session is None:
        bridge.add_message("agent", f"VM verify prep failed: {err}")
        return {"status": "error", "message": err}

    gui_state.vm_assisted_session = session
    gui_state.vm_assisted_prompt = {
        "public_key": session.public_key,
        "paste_snippet": session.paste_snippet,
        "vm_name": session.vm_name,
    }
    bridge.add_message(
        "agent",
        "VM is booting. Once you see its login prompt, paste the snippet into "
        "the root shell, then click Ready.",
    )
    return {
        "status": "ok",
        "vm_name": session.vm_name,
        "public_key": session.public_key,
        "paste_snippet": session.paste_snippet,
    }


def _finish_vm_assisted(gui_state: GUIState) -> None:
    """Phase B (background): SSH in, run install.sh, tear down."""
    from quickhatch.wizard.vm_verify import finish_assisted_session

    bridge = gui_state.bridge
    gui_state.vm_verify_running = True
    gui_state.vm_verify_result = None
    try:
        session = gui_state.vm_assisted_session
        if session is None:
            gui_state.vm_verify_result = {
                "status": "error",
                "success": False,
                "summary": "No active assisted session",
            }
            return

        def progress(msg: str) -> None:
            bridge.add_message("agent", f"[vm] {msg}")

        result = finish_assisted_session(session, progress=progress)
        gui_state.vm_verify_result = {
            "status": "ok",
            "success": result.success,
            "summary": result.summary(),
            "stages": result.stages,
            "exit_code": result.exit_code,
            "tail": (result.stderr_tail or result.stdout_tail)[-2000:],
            "backend": result.backend,
            "vm_name": result.vm_name,
            "error": result.error,
        }
        bridge.add_message("agent", f"VM verify: {result.summary()}")
    finally:
        gui_state.vm_assisted_session = None
        gui_state.vm_assisted_prompt = None
        gui_state.vm_verify_running = False


def _run_vm_verify(gui_state: GUIState, iso_path: str | None) -> None:
    """Background thread: run the VM-verify pipeline and record results on gui_state.

    Reports progress via bridge.add_message so the UI can tail it in the
    chat feed. On completion, writes a structured dict to
    ``gui_state.vm_verify_result`` and flips ``vm_verify_running`` off.
    """
    from quickhatch.wizard.vm_verify import verify_install_script

    bridge = gui_state.bridge
    gui_state.vm_verify_running = True
    gui_state.vm_verify_result = None
    try:
        # Find the install.sh from the last export (or from suggestions).
        install_script: Path | None = None
        # Prefer a freshly-exported install.sh if one exists under the
        # default export dir.
        export_dir = Path.home() / "quickhatch-export"
        if (export_dir / "install.sh").exists():
            install_script = export_dir / "install.sh"
        if install_script is None:
            # Fallback: write the suggestion body to a temp file.
            for s in bridge.suggestions:
                if getattr(s, "category", "") == "install_script" and (s.content or "").strip():
                    import tempfile

                    tmp = Path(tempfile.gettempdir()) / "quickhatch-install.sh"
                    tmp.write_text(s.content.strip(), encoding="utf-8")
                    install_script = tmp
                    break

        if install_script is None:
            gui_state.vm_verify_result = {
                "status": "error",
                "success": False,
                "summary": "No install.sh available — run the analysis agent first.",
            }
            bridge.add_message("agent", "VM verify: no install.sh found.")
            return

        def progress(msg: str) -> None:
            bridge.add_message("agent", f"[vm] {msg}")

        iso = Path(iso_path) if iso_path else None
        if iso and not iso.exists():
            gui_state.vm_verify_result = {
                "status": "error",
                "success": False,
                "summary": f"ISO not found: {iso}",
            }
            bridge.add_message("agent", f"VM verify: ISO not found at {iso}.")
            return

        result = verify_install_script(
            install_script,
            iso,
            ssh_key=_resolve_private_key_path(gui_state),
            progress=progress,
        )
        gui_state.vm_verify_result = {
            "status": "ok",
            "success": result.success,
            "summary": result.summary(),
            "stages": result.stages,
            "exit_code": result.exit_code,
            "tail": (result.stderr_tail or result.stdout_tail)[-2000:],
            "backend": result.backend,
            "vm_name": result.vm_name,
            "error": result.error,
        }
        bridge.add_message("agent", f"VM verify: {result.summary()}")
    except Exception as exc:
        gui_state.vm_verify_result = {
            "status": "error",
            "success": False,
            "summary": f"VM verify crashed: {exc}",
        }
        bridge.add_message("agent", f"VM verify crashed: {exc}")
    finally:
        gui_state.vm_verify_running = False


def _run_export(gui_state: GUIState) -> dict:
    """Run the full export: instructions, profile, SSH key, quickstart, zip."""
    import zipfile
    from dataclasses import asdict

    import yaml

    from quickhatch.ai.instruction_gen import save_instructions

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

        # Offline install script (present only when the user said they
        # don't have a second machine — analysis phase generated it as
        # the install_script section).
        install_script_content = ""
        for s in gui_state.bridge.suggestions:
            if getattr(s, "category", "") == "install_script" and (s.content or "").strip():
                install_script_content = s.content.strip()
                break
        if install_script_content:
            # Normalize: strip any accidental ``` fences the model added
            if install_script_content.startswith("```"):
                lines = install_script_content.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                install_script_content = "\n".join(lines)
            # Ensure shebang
            if not install_script_content.startswith("#!"):
                install_script_content = "#!/usr/bin/env bash\n" + install_script_content
            install_path = output_dir / "install.sh"
            install_path.write_text(install_script_content, encoding="utf-8")
            try:
                install_path.chmod(0o755)
            except OSError:
                pass  # Windows
            files.append(install_path.name)

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
