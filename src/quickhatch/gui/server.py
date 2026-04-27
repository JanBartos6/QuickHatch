"""Web GUI server — serves the SPA and handles wizard API calls.

Combines the bridge API (for LLM communication) with a wizard API
(for the web frontend to drive the wizard steps).
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from quickhatch.bridge.server import BridgeState
from quickhatch.config import AIConfig, UserProfile
from quickhatch.gallery.library import (
    find_gallery_entries,
    gallery_asset_path,
    load_gallery_entries,
)
from quickhatch.secrets import (
    delete_api_key,
    has_api_key,
    keyring_available,
    load_api_key,
    store_api_key,
)
from quickhatch.setup import (
    SetupFacts,
    SetupOrchestrator,
    SetupRuntime,
    detect_family,
)
from quickhatch.setup import (
    probe_setup_facts as _probe_typed_setup_facts,
)


def _ssh_public_to_private_key_path(public_key_path: str | None) -> Path | None:
    raw = (public_key_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path.with_suffix("") if path.suffix == ".pub" else path


def _resolve_ssh_private_key_path(profile: UserProfile) -> str | None:
    """Resolve the private key QuickHatch should use for SSH.

    Prefer the generated QuickHatch key over unrelated default SSH identities
    when the profile has lost `ssh_public_key_path` due to a refresh/recovery
    bug. This keeps the client aligned with the public key shipped in the
    export bundle and added by `quickstart.sh`.
    """
    candidates: list[Path] = []
    explicit = _ssh_public_to_private_key_path(getattr(profile, "ssh_public_key_path", None))
    if explicit is not None:
        candidates.append(explicit)
    home_ssh = Path.home() / ".ssh"
    candidates.extend(
        [
            home_ssh / "quickhatch_ed25519",
            home_ssh / "id_ed25519",
            home_ssh / "id_rsa",
        ]
    )
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return key
    return None


def _ssh_key_status(profile: UserProfile) -> dict[str, Any]:
    """Return whether the profile's SSH key reference is usable end-to-end."""
    raw = (getattr(profile, "ssh_public_key_path", None) or "").strip()
    if not raw:
        return {
            "path": None,
            "ready": False,
            "public_exists": False,
            "private_exists": False,
            "message": "No SSH public key path is saved.",
        }

    pub_path = Path(raw)
    private_path = _ssh_public_to_private_key_path(raw)
    public_exists = pub_path.exists()
    private_exists = bool(private_path and private_path.exists())
    ready = public_exists and private_exists

    if ready:
        message = "SSH keypair is ready."
    elif not public_exists and not private_exists:
        message = "Saved SSH key path points to files that no longer exist. Generate a new key or choose a valid .pub key."
    elif not public_exists:
        message = "Saved SSH public key file no longer exists. Generate a new key or choose a valid .pub key."
    else:
        message = "Matching SSH private key file is missing. QuickHatch can export the public key, but cannot connect with it."

    return {
        "path": str(pub_path),
        "private_path": str(private_path) if private_path else None,
        "ready": ready,
        "public_exists": public_exists,
        "private_exists": private_exists,
        "message": message,
    }


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
        # The currently-running agent thread (analysis or setup). Used to
        # reject concurrent /wizard/launch-agent calls — two overlapping
        # agents share bridge state and race on suggestions/messages.
        self._agent_thread: threading.Thread | None = None

    def agent_thread_running(self) -> bool:
        """Return whether the managed analysis/setup worker is still alive.

        Also heals stale thread references after the worker exits so the
        rest of the server does not keep treating a dead agent as active.
        """
        with self.lock:
            thread = self._agent_thread
            if thread is None:
                return False
            if thread.is_alive():
                return True
            self._agent_thread = None
        with self.bridge.lock:
            self.bridge.agent_connected = False
            self.bridge.stop_requested = False
            self.bridge.current_phase = ""
            if self.bridge.setup_run.get("status") == "stopping":
                self.bridge.setup_run["status"] = "stopped"
            elif self.bridge.setup_run.get("status") not in {
                "done",
                "blocked",
                "failed",
                "stopped",
                "cancelled",
            }:
                self.bridge.setup_run["status"] = "idle"
        return False

    def mark_agent_running(self, phase: str) -> None:
        with self.bridge.lock:
            self.bridge.agent_connected = True
            if phase == "setup":
                self.bridge.setup_run["status"] = "running"
        self.bridge.set_current_phase(phase)

    def mark_agent_stopped(self, finished_thread: threading.Thread | None = None) -> None:
        with self.lock:
            current = self._agent_thread
            if finished_thread is not None and current is finished_thread:
                self._agent_thread = None
            elif current is not None and not current.is_alive():
                self._agent_thread = None
        with self.bridge.lock:
            self.bridge.agent_connected = False
            self.bridge.stop_requested = False
            self.bridge.current_phase = ""
            if self.bridge.setup_run.get("status") == "stopping":
                self.bridge.setup_run["status"] = "stopped"
            elif self.bridge.setup_run.get("status") not in {
                "done",
                "blocked",
                "failed",
                "stopped",
                "cancelled",
            }:
                self.bridge.setup_run["status"] = "idle"

    # Fields whose declared type is list[...] — null and non-iterable
    # values get coerced to [] instead of crashing every downstream
    # .sort() / len() / iteration.
    _LIST_PREF_FIELDS = frozenset({"gallery_picks", "use_cases"})
    _LIST_TOP_FIELDS = frozenset({"apps_to_install"})
    # Fields whose declared type is str.
    _STR_AI_FIELDS = frozenset({"provider", "model_name", "api_key", "endpoint"})
    _BOOL_AI_FIELDS = frozenset({"native_search"})
    _STR_PREF_FIELDS = frozenset(
        {
            "migration_type",
            "desktop_look",
            "icon_style",
            "wallpaper_style",
            "distro_preference",
            "additional_notes",
            "another_machine",
        }
    )
    _STR_TOP_FIELDS = frozenset(
        {
            "additional_info",
            "data_description",
            "data_transfer_method",
            "linux_ip",
            "linux_user",
            "remote_setup_mode",
            "target_hardware_override",
            "default_account_password",
            "ssh_public_key_path",
        }
    )

    @staticmethod
    def _coerce_str(val: Any) -> str | None:
        """Accept str or None; silently drop anything else."""
        if val is None:
            return None
        return val if isinstance(val, str) else None

    @staticmethod
    def _coerce_str_list(val: Any) -> list[str]:
        """Normalize list-shaped fields: None/not-a-list → [], stringify
        items, drop non-str entries."""
        if not isinstance(val, list):
            return []
        return [x for x in val if isinstance(x, str)]

    @staticmethod
    def _set_if_changed(obj: Any, attr: str, value: Any) -> bool:
        if getattr(obj, attr) == value:
            return False
        setattr(obj, attr, value)
        return True

    def update_profile(self, updates: dict) -> bool:
        """Apply partial updates to the profile from the web UI.

        Type-coerces every field to its declared shape. Unknown keys
        get logged (not silent data loss). No field ever ends up with a
        Python None when the dataclass says list[str]. Returns True when
        at least one persisted field changed.
        """
        import sys as _sys

        dropped: list[str] = []
        changed = False
        with self.lock:
            prefs = updates.get("preferences", {})
            if isinstance(prefs, dict):
                for k, v in prefs.items():
                    if not hasattr(self.profile.preferences, k):
                        dropped.append(f"preferences.{k}")
                        continue
                    if k == "gallery_picks":
                        # Validate against the actual library so a
                        # direct /wizard/update can't smuggle bogus
                        # IDs past the gallery-picks endpoint's guard.
                        picks = self._coerce_str_list(v)
                        try:
                            valid_ids = {e.get("id") for e in load_gallery_entries()}
                            picks = [p for p in picks if p in valid_ids]
                        except Exception:
                            pass
                        changed |= self._set_if_changed(self.profile.preferences, k, picks[:10])
                    elif k in self._LIST_PREF_FIELDS:
                        changed |= self._set_if_changed(
                            self.profile.preferences, k, self._coerce_str_list(v)
                        )
                    elif k in self._STR_PREF_FIELDS:
                        coerced = self._coerce_str(v)
                        if coerced is not None or v is None:
                            changed |= self._set_if_changed(self.profile.preferences, k, coerced)
                    else:
                        # Unknown-but-declared field type — pass through.
                        changed |= self._set_if_changed(self.profile.preferences, k, v)

            ai = updates.get("ai_config", {})
            if isinstance(ai, dict):
                for k, v in ai.items():
                    if not hasattr(self.profile.ai_config, k):
                        dropped.append(f"ai_config.{k}")
                        continue
                    if k in self._STR_AI_FIELDS:
                        changed |= self._set_if_changed(
                            self.profile.ai_config, k, self._coerce_str(v)
                        )
                    elif k in self._BOOL_AI_FIELDS and isinstance(v, bool):
                        changed |= self._set_if_changed(self.profile.ai_config, k, v)
                    else:
                        changed |= self._set_if_changed(self.profile.ai_config, k, v)

            if "additional_info" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "additional_info",
                    self._coerce_str(updates["additional_info"]) or "",
                )
            if "apps_to_install" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "apps_to_install",
                    self._coerce_str_list(updates["apps_to_install"]),
                )
            if "data_description" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "data_description",
                    self._coerce_str(updates["data_description"]) or "",
                )
            if "data_transfer_method" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "data_transfer_method",
                    self._coerce_str(updates["data_transfer_method"]) or "",
                )
            if "linux_ip" in updates:
                # Strip CIDR suffix (`/24`) and surrounding whitespace —
                # users naturally paste from `ip addr show` output which
                # includes the prefix length, but SSH will treat that as
                # part of the hostname and fail to resolve.
                ip = self._coerce_str(updates["linux_ip"]) or ""
                changed |= self._set_if_changed(
                    self.profile,
                    "linux_ip",
                    ip.strip().split("/", 1)[0],
                )
            if "linux_user" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "linux_user",
                    self._coerce_str(updates["linux_user"]) or "",
                )
            if "remote_setup_mode" in updates:
                mode = self._coerce_str(updates["remote_setup_mode"]) or "auto"
                if mode not in {"auto", "wipe_and_reinstall", "configure_only"}:
                    mode = "auto"
                changed |= self._set_if_changed(self.profile, "remote_setup_mode", mode)
            if "target_hardware_override" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "target_hardware_override",
                    self._coerce_str(updates["target_hardware_override"]) or "",
                )
            if "default_account_password" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "default_account_password",
                    self._coerce_str(updates["default_account_password"]) or "password",
                )
            if "ssh_public_key_path" in updates:
                changed |= self._set_if_changed(
                    self.profile,
                    "ssh_public_key_path",
                    self._coerce_str(updates["ssh_public_key_path"]),
                )

            known_top = {
                "preferences",
                "ai_config",
                *self._LIST_TOP_FIELDS,
                *self._STR_TOP_FIELDS,
            }
            dropped.extend(
                sorted(k for k in updates if k not in known_top and not k.startswith("_"))
            )

        if dropped:
            print(
                f"[quickhatch] /wizard/update dropped unknown keys: {dropped}",
                file=_sys.stderr,
            )
        return changed

    def restore_profile(self, restored: UserProfile) -> bool:
        """Replace the current profile contents in place.

        Keep the original UserProfile instance alive so BridgeState and any
        long-lived references continue to observe the updated values.
        """
        with self.lock:
            before = asdict(self.profile)
            existing_api_key = self.profile.ai_config.api_key
            if restored.ai_config.api_key in {"***SET***", "***REDACTED***"}:
                restored.ai_config.api_key = None
            same_ai_target = restored.ai_config.provider == self.profile.ai_config.provider and (
                restored.ai_config.endpoint or ""
            ) == (self.profile.ai_config.endpoint or "")
            if not restored.ai_config.api_key and existing_api_key and same_ai_target:
                restored.ai_config.api_key = existing_api_key
            for field_def in fields(UserProfile):
                setattr(self.profile, field_def.name, getattr(restored, field_def.name))
            self.bridge.profile = self.profile
            changed = asdict(self.profile) != before
        # Re-run persisted-bridge recovery under the NEW profile
        # fingerprint. This is the crash-recovery path: the server may
        # have started under a stale/mismatched checkpoint profile and
        # therefore skipped restoring the real analysis suggestions on
        # startup. Once the user restores the real profile, re-check the
        # persisted bridge file so setup can still recover the generated
        # migration plan after a restart.
        self.bridge.reload_persisted_state()
        return changed


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


def _profile_response_dict(profile: UserProfile) -> dict[str, Any]:
    """Redacted profile payload for SPA responses."""
    profile_dict = asdict(profile)
    if profile_dict.get("ai_config", {}).get("api_key"):
        profile_dict["ai_config"]["api_key"] = "***SET***"
    profile_dict["ssh_key_status"] = _ssh_key_status(profile)
    return profile_dict


def _prepare_ai_secret_update(gui_state: GUIState, updates: dict[str, Any]) -> dict[str, Any]:
    """Normalize API-key updates before GUIState applies them.

    Raw keys may enter through /wizard/update, but they must not be stored in
    checkpoints. If requested, save them to OS credential storage and keep the
    runtime profile hydrated so the current server session can make API calls.
    """
    if not isinstance(updates, dict) or not isinstance(updates.get("ai_config"), dict):
        return {"stored": False, "message": ""}

    ai = dict(updates["ai_config"])
    remember = bool(ai.pop("remember_api_key", False))
    provider = (ai.get("provider") or gui_state.profile.ai_config.provider or "").strip()
    endpoint = (ai.get("endpoint") or gui_state.profile.ai_config.endpoint or "").strip()
    raw_key = ai.get("api_key")
    key = raw_key.strip() if isinstance(raw_key, str) else ""
    stored = False
    message = ""

    if key in {"***SET***", "***REDACTED***"}:
        raw_key = None
        key = ""
        ai["api_key"] = None

    if key:
        if remember:
            result = store_api_key(provider, endpoint, key)
            stored = result.ok
            message = result.message
        ai["api_key"] = key
    elif raw_key is None or (isinstance(raw_key, str) and not raw_key.strip()):
        # Keep an existing in-memory key when the provider/endpoint did not
        # change, otherwise try the OS store for the selected provider. This
        # lets users leave the input blank after opting in to local storage.
        existing = gui_state.profile.ai_config
        same_target = provider == existing.provider and endpoint == (existing.endpoint or "")
        if same_target and existing.api_key:
            ai.pop("api_key", None)
        else:
            loaded = load_api_key(provider, endpoint)
            ai["api_key"] = loaded

    updates["ai_config"] = ai
    return {"stored": stored, "message": message}


def _remote_setup_mode_block(profile: UserProfile) -> str:
    """Explicit user intent for destructive vs non-destructive setup."""
    mode = getattr(profile, "remote_setup_mode", "auto") or "auto"
    if mode == "wipe_and_reinstall":
        return (
            "== REMOTE SETUP MODE ==\n"
            "User selected: WIPE TARGET DISK AND REINSTALL FROM SCRATCH.\n\n"
            "This is an explicit destructive mode. Rules:\n"
            "- If you are in a LIVE environment, you MAY repartition/reformat and wipe the "
            "chosen target disk, but ONLY after showing `lsblk -f` and getting explicit disk "
            "confirmation from the user in bridge chat.\n"
            "- If you are on an INSTALLED system (not a live ISO), DO NOT wipe the running "
            "root filesystem in place. Ask the user to boot back into the live environment "
            "first, then continue the reinstall there.\n"
            "- Do not preserve the existing install unless the user explicitly changes mode.\n\n"
        )
    if mode == "configure_only":
        return (
            "== REMOTE SETUP MODE ==\n"
            "User selected: CONFIGURE EXISTING INSTALL ONLY.\n\n"
            "This mode is non-destructive. Rules:\n"
            "- NEVER repartition, mkfs, wipe disks, or reinstall the distro.\n"
            "- If you detect a LIVE environment, stop and tell the user to either boot into "
            "their installed system or switch the wizard to wipe-and-reinstall mode.\n"
            "- Focus only on drivers, desktop, apps, services, and user configuration.\n\n"
        )
    return (
        "== REMOTE SETUP MODE ==\n"
        "User selected: AUTO.\n\n"
        "Default behavior rules:\n"
        "- In a LIVE environment, you may perform a fresh install if needed, but destructive "
        "disk changes still require explicit disk confirmation from the user after showing "
        "`lsblk -f`.\n"
        "- On an INSTALLED system, do not wipe disks; continue with post-install configuration.\n\n"
    )


def _reset_all_data(gui_state: GUIState) -> dict[str, Any]:
    """Delete all QuickHatch-owned local wizard state for this user.

    Clears:
    - the in-memory + persisted profile checkpoint
    - the bridge transcript / suggestions persistence
    - the generated QuickHatch SSH keypair (but never a user-provided key)
    - the exported QuickHatch bundle directory
    """
    import shutil

    from quickhatch.config import _checkpoint_path

    with gui_state.lock:
        if gui_state._agent_thread is not None and gui_state._agent_thread.is_alive():
            return {
                "status": "error",
                "message": "Stop the running agent before wiping QuickHatch data.",
            }

        sess = gui_state.vm_assisted_session
        if sess is not None and getattr(sess, "_vm", None) is not None:
            try:
                sess._vm.teardown()
            except Exception:
                pass

        fresh = UserProfile()
        for field_def in fields(UserProfile):
            setattr(gui_state.profile, field_def.name, getattr(fresh, field_def.name))
        gui_state.bridge.hard_reset(profile=gui_state.profile)
        gui_state.vm_verify_enabled = False
        gui_state.vm_verify_result = None
        gui_state.vm_verify_running = False
        gui_state.vm_assisted_session = None
        gui_state.vm_assisted_prompt = None
        gui_state._agent_thread = None

    removed: list[str] = []

    checkpoint = _checkpoint_path()
    try:
        checkpoint.unlink(missing_ok=True)
        removed.append(str(checkpoint))
    except OSError:
        pass

    # QuickHatch-generated SSH key only. Never touch user-provided keys.
    quickhatch_key = Path.home() / ".ssh" / "quickhatch_ed25519"
    for key_file in (quickhatch_key, quickhatch_key.with_suffix(".pub")):
        try:
            key_file.unlink(missing_ok=True)
            removed.append(str(key_file))
        except OSError:
            pass

    export_dirs = []
    current_export = _export_output_dir()
    export_dirs.append(current_export)
    legacy_export = Path.home() / "QuickHatch-Migration"
    if legacy_export not in export_dirs:
        export_dirs.append(legacy_export)

    for export_dir in export_dirs:
        if not export_dir.exists():
            continue
        try:
            shutil.rmtree(export_dir)
            removed.append(str(export_dir))
        except OSError:
            pass

    return {
        "status": "ok",
        "message": "QuickHatch data cleared.",
        "removed": removed,
        "profile": _profile_response_dict(gui_state.profile),
    }


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
) -> tuple[ThreadingHTTPServer, int]:
    """Create and return the GUI HTTP server."""

    bridge_state = gui_state.bridge

    class GUIHandler(BaseHTTPRequestHandler):
        # HTTP/1.1 keeps keep-alive working — HTTP/1.0 + implicit close
        # races Windows TCP stack with Python's urllib3, which throws
        # ConnectionResetError(10054) on every subsequent request. Every
        # Python client we'd hand the server to (tests, SDK, curl-via-
        # requests) is affected.
        protocol_version = "HTTP/1.1"

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

        # Max JSON body we'll accept. 16 MiB is generous for any legit
        # wizard update (largest field is gallery JSON or install_script
        # content). Larger reads are rejected rather than allowed to pin
        # a worker thread on a slow-loris body stream.
        _MAX_BODY_BYTES = 16 * 1024 * 1024

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            if length < 0 or length > self._MAX_BODY_BYTES:
                # Reject obviously-malformed or oversized bodies. Reading
                # them would hang (negative length → read(-1) reads until
                # EOF) or waste memory (huge length → OOM).
                self._send_json({"status": "error", "message": "invalid Content-Length"}, 413)
                return {}
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
                profile_dict = _profile_response_dict(bridge_state.profile)
                if profile_dict.get("ai_config", {}).get("api_key"):
                    profile_dict["ai_config"]["api_key"] = "***REDACTED***"
                self._send_json({"status": "ok", "profile": profile_dict})

            elif self.path == "/api/status":
                gui_state.agent_thread_running()
                setup_run = bridge_state.snapshot_setup_run()
                self._send_json(
                    {
                        "status": "ok",
                        "agent_connected": bridge_state.agent_connected,
                        "pending_questions": len(bridge_state.get_pending_questions()),
                        "suggestions": len(bridge_state.suggestions),
                        "agent_done": bridge_state.agent_done,
                        "setup_run": setup_run,
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
                            {
                                "id": q.id,
                                "question": q.question,
                                "choices": q.choices,
                                "blocking": q.blocking,
                                "phase": q.phase,
                                "context": q.context,
                            }
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
                q = bridge_state.add_question(
                    question,
                    choices,
                    blocking=bool(data.get("blocking", False)),
                    phase=data.get("phase", "") or "",
                    context=dict(data.get("context", {}) or {}),
                )
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
                q = bridge_state.add_question(
                    question,
                    choices,
                    blocking=bool(data.get("blocking", False)),
                    phase=data.get("phase", "") or "",
                    context=dict(data.get("context", {}) or {}),
                )
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
                self._send_json(
                    {"status": "ok", "profile": _profile_response_dict(gui_state.profile)}
                )

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
                    content = (
                        default.read_text(encoding="utf-8").strip() if default.exists() else ""
                    )
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

            elif route.startswith("/api/"):
                self._handle_bridge_get()

            else:
                self._send_json({"error": "Not found"}, 404)

        def do_POST(self):
            if self.path == "/wizard/update":
                data = self._read_json()
                secret_result = _prepare_ai_secret_update(gui_state, data)
                changed = gui_state.update_profile(data)
                if changed:
                    gui_state.profile.save_checkpoint()
                self._send_json(
                    {
                        "status": "ok",
                        "changed": changed,
                        "api_key_stored": secret_result["stored"],
                        "api_key_storage_message": secret_result["message"],
                        "profile": _profile_response_dict(gui_state.profile),
                    }
                )

            elif self.path == "/wizard/api-key-status":
                data = self._read_json()
                provider = (
                    data.get("provider") or gui_state.profile.ai_config.provider or ""
                ).strip()
                endpoint = (
                    data.get("endpoint") or gui_state.profile.ai_config.endpoint or ""
                ).strip()
                in_session = bool(
                    gui_state.profile.ai_config.api_key
                    and provider == gui_state.profile.ai_config.provider
                    and endpoint == (gui_state.profile.ai_config.endpoint or "")
                )
                self._send_json(
                    {
                        "status": "ok",
                        "available": bool(in_session or has_api_key(provider, endpoint)),
                        "keyring_available": keyring_available(),
                    }
                )

            elif self.path == "/wizard/api-key-delete":
                data = self._read_json()
                provider = (
                    data.get("provider") or gui_state.profile.ai_config.provider or ""
                ).strip()
                endpoint = (
                    data.get("endpoint") or gui_state.profile.ai_config.endpoint or ""
                ).strip()
                result = delete_api_key(provider, endpoint)
                changed = False
                with gui_state.lock:
                    if (
                        provider == gui_state.profile.ai_config.provider
                        and endpoint == (gui_state.profile.ai_config.endpoint or "")
                        and gui_state.profile.ai_config.api_key
                    ):
                        gui_state.profile.ai_config.api_key = None
                        changed = True
                if changed:
                    gui_state.profile.save_checkpoint()
                self._send_json(
                    {
                        "status": "ok" if result.ok else "error",
                        "deleted": result.ok,
                        "message": result.message,
                        "profile": _profile_response_dict(gui_state.profile),
                    },
                    200 if result.ok else 500,
                )

            elif self.path == "/wizard/restore-profile":
                data = self._read_json()
                payload = data.get("profile") if isinstance(data, dict) else None
                restored = UserProfile.from_dict(payload)
                if restored is None:
                    self._send_json(
                        {"status": "error", "message": "Expected a valid profile payload."}, 400
                    )
                    return
                changed = gui_state.restore_profile(restored)
                if changed:
                    gui_state.profile.save_checkpoint()
                self._send_json(
                    {
                        "status": "ok",
                        "changed": changed,
                        "profile": _profile_response_dict(gui_state.profile),
                    }
                )

            elif self.path == "/wizard/reset-all":
                self._read_json()  # drain request body
                result = _reset_all_data(gui_state)
                status = 200 if result.get("status") == "ok" else 409
                self._send_json(result, status)

            elif self.path == "/wizard/open-export-folder":
                # Open the export bundle directory in the host file manager.
                # Per-OS: explorer.exe on Windows, open on macOS, xdg-open
                # on Linux. Fails silently — UI shows the path either way.
                import subprocess as _sp
                import sys as _sys

                target = _export_output_dir()
                try:
                    if not target.exists():
                        self._send_json(
                            {"status": "error", "message": "No bundle exported yet"}, 404
                        )
                    else:
                        if _sys.platform == "win32":
                            _sp.Popen(["explorer", str(target)])
                        elif _sys.platform == "darwin":
                            _sp.Popen(["open", str(target)])
                        else:
                            _sp.Popen(["xdg-open", str(target)])
                        self._send_json({"status": "ok", "path": str(target)})
                except Exception as exc:
                    self._send_json({"status": "error", "message": str(exc)}, 500)

            elif self.path == "/wizard/fetch-models":
                # Provider-agnostic model-list fetch. Body:
                # {"kind": "anthropic"|"openai-compat"|"ollama",
                #  "endpoint": "...", "api_key": "..."}
                from quickhatch.ai.model_list import VALID_KINDS
                from quickhatch.ai.model_list import fetch as _fetch_models

                data = self._read_json()
                kind = (data.get("kind") or "").strip()
                endpoint = (data.get("endpoint") or "").strip()
                api_key = (data.get("api_key") or "").strip()
                provider = (
                    data.get("provider") or gui_state.profile.ai_config.provider or ""
                ).strip()
                if not api_key:
                    same_target = provider == gui_state.profile.ai_config.provider and endpoint == (
                        gui_state.profile.ai_config.endpoint or ""
                    )
                    if same_target and gui_state.profile.ai_config.api_key:
                        api_key = gui_state.profile.ai_config.api_key
                    else:
                        api_key = load_api_key(provider, endpoint) or ""
                if not kind or not endpoint:
                    self._send_json(
                        {"status": "error", "message": "kind and endpoint required"}, 400
                    )
                elif kind not in VALID_KINDS:
                    self._send_json(
                        {"status": "error", "message": f"unknown provider kind: {kind}"}, 400
                    )
                elif not endpoint.startswith(("http://", "https://")):
                    self._send_json(
                        {"status": "error", "message": "endpoint must be http(s)://"}, 400
                    )
                else:
                    try:
                        models = _fetch_models(kind, endpoint, api_key or None)
                    except Exception as exc:
                        self._send_json({"status": "error", "message": str(exc)}, 500)
                        return
                    self._send_json({"status": "ok", "models": models})

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
                changed = gui_state.profile.preferences.gallery_picks != deduped_picks
                gui_state.profile.preferences.gallery_picks = deduped_picks
                if changed:
                    gui_state.profile.save_checkpoint()
                self._send_json({"status": "ok", "picks": deduped_picks, "changed": changed})

            elif self.path == "/wizard/complete_step":
                data = self._read_json()
                step = data.get("step", "")
                if step and step not in gui_state.profile.completed_steps:
                    gui_state.profile.completed_steps.append(step)
                    gui_state.profile.save_checkpoint()
                    changed = True
                else:
                    changed = False
                self._send_json(
                    {
                        "status": "ok",
                        "completed": gui_state.profile.completed_steps,
                        "changed": changed,
                    }
                )

            elif self.path == "/wizard/scan":
                results = _run_scan()
                with gui_state.lock:
                    if results.get("apps"):
                        from quickhatch.analysis.app_catalog import filter_migration_apps
                        from quickhatch.analysis.app_mapper import map_apps
                        from quickhatch.config import AppInfo

                        apps = [AppInfo(**a) for a in results["apps"]]
                        try:
                            apps = map_apps(apps)
                        except Exception:
                            pass
                        apps = filter_migration_apps(apps)
                        gui_state.profile.apps = apps
                        # Reflect the enriched apps in the response
                        results["apps"] = [asdict(a) for a in apps]
                    if results.get("hardware"):
                        from quickhatch.config import DiskInfo, DisplayInfo, GPUInfo, HardwareInfo

                        hw = results["hardware"]
                        gui_state.profile.hardware = HardwareInfo(
                            **{
                                k: v
                                for k, v in hw.items()
                                if k not in ("gpus", "disks", "displays")
                            },
                            gpus=[GPUInfo(**g) for g in hw.get("gpus", [])],
                            disks=[DiskInfo(**d) for d in hw.get("disks", [])],
                            displays=[DisplayInfo(**d) for d in hw.get("displays", [])],
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
                # TOCTOU-safe: flip the flag under the lock BEFORE
                # spawning. Two near-simultaneous POSTs used to both
                # pass the check and both start threads.
                spawn = False
                with gui_state.lock:
                    if gui_state.vm_verify_running:
                        self._send_json({"status": "error", "message": "VM verify already running"})
                    else:
                        gui_state.vm_verify_running = True
                        spawn = True
                if spawn:
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
                else:
                    # TOCTOU: reserve the slot under the lock before
                    # doing any work that ends up assigning back into
                    # vm_assisted_session. Use a sentinel to claim it.
                    claim = False
                    with gui_state.lock:
                        if gui_state.vm_assisted_session is None:
                            gui_state.vm_assisted_session = "__starting__"  # type: ignore[assignment]
                            claim = True
                    if not claim:
                        self._send_json(
                            {
                                "status": "error",
                                "message": "A VM session is already active — cancel it first",
                            }
                        )
                    else:
                        try:
                            result = _start_vm_assisted(gui_state, iso_path)
                        except Exception:
                            with gui_state.lock:
                                if gui_state.vm_assisted_session == "__starting__":
                                    gui_state.vm_assisted_session = None
                            raise
                        # _start_vm_assisted sets the real session on
                        # success; clear sentinel if it didn't.
                        with gui_state.lock:
                            if gui_state.vm_assisted_session == "__starting__":
                                gui_state.vm_assisted_session = None
                        self._send_json(result)

            elif self.path == "/wizard/vm-verify/finish":
                # Phase B of user-in-loop flow: user pasted the key,
                # SSH in, run the script, tear down.
                self._read_json()
                # TOCTOU: atomically check-and-claim running flag.
                spawn = False
                err: str | None = None
                with gui_state.lock:
                    if (
                        gui_state.vm_assisted_session is None
                        or gui_state.vm_assisted_session == "__starting__"
                    ):
                        err = "No active assisted session"
                    elif gui_state.vm_verify_running:
                        err = "Script already running"
                    else:
                        gui_state.vm_verify_running = True
                        spawn = True
                if err:
                    self._send_json({"status": "error", "message": err})
                elif spawn:
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
                if not qid or not answer:
                    self._send_json(
                        {"status": "error", "message": "Missing question_id or answer"}, 400
                    )
                elif not bridge_state.answer_question(qid, answer):
                    # answer_question returns False when the qid is
                    # unknown. Surface that so the UI can show an
                    # error instead of pretending we saved it.
                    self._send_json(
                        {
                            "status": "error",
                            "message": "unknown question_id (already answered or expired)",
                        },
                        404,
                    )
                else:
                    self._send_json({"status": "ok"})

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
                # Prepare launch state only after the new launch has been
                # admitted. Otherwise a concurrent start rejection could
                # clear stop_requested/agent_done under a still-running agent.
                result = _launch_agent(
                    gui_state,
                    server.server_address[1],
                    phase=phase,
                    prelaunch=bridge_state.prepare_for_launch,
                )
                self._send_json(result)

            elif self.path == "/wizard/stop-agent":
                self._read_json()  # drain request body so Windows doesn't RST
                bridge_state.request_stop()
                bridge_state.update_setup_run(status="stopping", waiting_for_input=False)
                if not gui_state.agent_thread_running() and bridge_state.agent_connected:
                    gui_state.mark_agent_stopped()
                bridge_state.add_message(
                    "agent", "Stop requested — finishing current step and exiting."
                )
                self._send_json({"status": "ok"})

            elif self.path == "/wizard/clear-setup-log":
                self._read_json()  # drain request body
                if gui_state.agent_thread_running():
                    self._send_json(
                        {
                            "status": "error",
                            "message": "Stop the running agent before clearing the setup log.",
                        },
                        409,
                    )
                else:
                    bridge_state.clear_phase_log("setup")
                    self._send_json({"status": "ok"})

            elif self.path == "/wizard/reset-remote-setup":
                self._read_json()  # drain request body
                if gui_state.agent_thread_running():
                    self._send_json(
                        {
                            "status": "error",
                            "message": "Stop the running agent before resetting Remote Setup.",
                        },
                        409,
                    )
                    return
                with gui_state.lock:
                    gui_state.profile.linux_ip = ""
                    gui_state.profile.linux_user = ""
                    gui_state.profile.remote_setup_mode = "auto"
                    gui_state.profile.save_checkpoint()
                bridge_state.clear_phase_log("setup")
                self._send_json(
                    {
                        "status": "ok",
                        "profile": _profile_response_dict(gui_state.profile),
                    }
                )

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
                    key_path = _resolve_ssh_private_key_path(gui_state.profile)
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
                # Preflight concurrent-launch check BEFORE mutating any
                # bridge state. Otherwise sendChat during an active run
                # would reset_for_rerun (wiping suggestions + done flag)
                # only to be rejected, leaving the original agent thread
                # alive against an empty state.
                with gui_state.lock:
                    active = (
                        gui_state._agent_thread is not None and gui_state._agent_thread.is_alive()
                    )
                if active:
                    self._send_json(
                        {
                            "status": "error",
                            "message": "An agent is already running. Click Stop first (or wait for it).",
                        }
                    )
                else:
                    result = _launch_agent(
                        gui_state,
                        server.server_address[1],
                        user_feedback=user_msg,
                        prelaunch=lambda: _prepare_full_rerun(bridge_state, user_msg),
                    )
                    self._send_json(result)

            elif self.path == "/wizard/rerun-section":
                # Regenerate a single analysis section without wiping all
                # the others. The section loop's skip-if-already-done logic
                # then resumes from here.
                data = self._read_json()
                section = (data.get("section") or "").strip()
                user_msg = data.get("message", "")
                # Whitelist against the analysis section keys. Unknown
                # strings would silently wipe nothing and then re-run the
                # agent with an invalid target, or worse be echoed back
                # into prompts verbatim.
                _valid_sections = {
                    "distro",
                    "apps",
                    "drivers",
                    "desktop",
                    "plan",
                    "summary",
                    "install_script",
                }
                if not section:
                    self._send_json({"status": "error", "message": "section is required"}, 400)
                elif section not in _valid_sections:
                    self._send_json(
                        {"status": "error", "message": f"unknown section: {section}"}, 400
                    )
                else:
                    # Same preflight: don't wipe a running agent's state.
                    with gui_state.lock:
                        active = (
                            gui_state._agent_thread is not None
                            and gui_state._agent_thread.is_alive()
                        )
                    if active:
                        self._send_json(
                            {
                                "status": "error",
                                "message": "An agent is already running. Click Stop first (or wait for it).",
                            }
                        )
                    else:
                        # Drop the named section's suggestions AND every
                        # downstream section — desktop/plan/summary all
                        # depend on the upstream section's output, so
                        # leaving them in place gives the UI stale info.
                        # Keep the section graph explicit instead of
                        # inferring dependencies from whatever the agent
                        # happened to finish last run. If an upstream
                        # section changes, every later section in the
                        # analysis chain must be recomputed.
                        _DOWNSTREAM = {
                            "distro": {
                                "apps",
                                "drivers",
                                "desktop",
                                "plan",
                                "summary",
                                "install_script",
                            },
                            "apps": {
                                "drivers",
                                "desktop",
                                "plan",
                                "summary",
                                "install_script",
                            },
                            "drivers": {"desktop", "plan", "summary", "install_script"},
                            "desktop": {"plan", "summary", "install_script"},
                            "plan": {"summary", "install_script"},
                            "summary": set(),
                            "install_script": set(),
                        }
                        invalidated = {section} | _DOWNSTREAM.get(section, set())
                        result = _launch_agent(
                            gui_state,
                            server.server_address[1],
                            user_feedback=user_msg,
                            prelaunch=lambda: _prepare_section_rerun(
                                bridge_state, invalidated, user_msg
                            ),
                        )
                        self._send_json(result)

            elif self.path.startswith("/api/"):
                self._handle_bridge_post()

            else:
                self._send_json({"error": "Not found"}, 404)

    server = ThreadingHTTPServer((host, port), GUIHandler)
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


_NATIVE_SEARCH_PROVIDERS = frozenset({"api_openai", "api_anthropic", "api_gemini", "api_grok"})


def _provider_supports_native_search(provider: str | None) -> bool:
    return (provider or "") in _NATIVE_SEARCH_PROVIDERS


def _use_provider_native_search(ai_config: AIConfig) -> bool:
    return bool(getattr(ai_config, "native_search", True)) and _provider_supports_native_search(
        ai_config.provider
    )


def _messages_to_openai_responses_input(
    messages: list[dict[str, str]], system_prompt: str = ""
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if system_prompt.strip():
        items.append({"role": "system", "content": system_prompt})
    for message in messages:
        role = message.get("role") or "user"
        content = message.get("content", "")
        if role not in {"system", "user", "assistant", "developer"}:
            role = "user"
        items.append({"role": role, "content": content})
    return items


def _extract_responses_output_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for block in item.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(p for p in parts if p).strip()


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for cand in payload.get("candidates", []) or []:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content") or {}
        for part in content.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
    return "\n".join(p for p in parts if p).strip()


def _gemini_native_endpoint(endpoint: str | None, model: str) -> str:
    from urllib.parse import quote

    base = (endpoint or "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    return f"{base}/models/{quote(model, safe='')}:generateContent"


def _call_openai_responses_with_native_search(
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    *,
    system_prompt: str = "",
) -> str:
    import requests

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{(endpoint or '').rstrip('/')}/responses"
    body = {
        "model": model,
        "input": _messages_to_openai_responses_input(messages, system_prompt),
        "tools": [{"type": "web_search"}],
    }
    resp = requests.post(url, headers=headers, json=body, timeout=900)
    resp.raise_for_status()
    return _extract_responses_output_text(resp.json())


def _call_anthropic_with_native_search(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    system_prompt: str = "",
) -> str:
    import requests

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=900,
    )
    resp.raise_for_status()
    data = resp.json()
    return "\n".join(
        block.get("text", "")
        for block in (data.get("content") or [])
        if block.get("type") == "text"
    ).strip()


def _call_gemini_with_native_search(
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    *,
    system_prompt: str = "",
) -> str:
    import requests

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key or "",
    }
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = "model" if message.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": message.get("content", "")}]})
    body: dict[str, Any] = {
        "contents": contents,
        "tools": [{"google_search": {}}],
    }
    if system_prompt.strip():
        body["system_instruction"] = {"parts": [{"text": system_prompt}]}
    resp = requests.post(
        _gemini_native_endpoint(endpoint, model),
        headers=headers,
        json=body,
        timeout=900,
    )
    resp.raise_for_status()
    return _extract_gemini_text(resp.json())


def _call_model_with_native_search(
    provider: str | None,
    endpoint: str,
    model: str,
    messages: list[dict[str, str]],
    api_key: str | None,
    *,
    system_prompt: str = "",
) -> str:
    provider = provider or ""
    if provider == "api_anthropic":
        return _call_anthropic_with_native_search(
            api_key or "",
            model,
            messages,
            system_prompt=system_prompt,
        )
    if provider == "api_gemini":
        return _call_gemini_with_native_search(
            endpoint,
            model,
            messages,
            api_key,
            system_prompt=system_prompt,
        )
    if provider in {"api_openai", "api_grok"}:
        return _call_openai_responses_with_native_search(
            endpoint,
            model,
            messages,
            api_key,
            system_prompt=system_prompt,
        )
    raise ValueError(f"Provider {provider!r} does not support native search")


def _call_openai_with_tools(
    endpoint: str,
    model: str,
    messages: list[dict],
    api_key: str | None,
    bridge: BridgeState,
    provider: str | None = None,
    max_rounds: int = 10,
) -> str:
    """Call an OpenAI-compatible API with tool-use loop (LM Studio, OpenAI)."""
    import requests

    from quickhatch.ai.model_list import chat_completions_url

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = chat_completions_url(provider, endpoint)

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
    return _extract_openai_text(resp.json())


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


def _call_model_analysis(
    endpoint: str,
    model: str,
    prompt: str,
    api_key: str | None,
    bridge: BridgeState,
    provider: str | None = None,
    use_native_search: bool = False,
) -> str:
    """Run an analysis-section model call with tool use when supported."""
    from quickhatch.ai.model_list import is_anthropic

    if use_native_search and _provider_supports_native_search(provider):
        return _call_model_with_native_search(
            provider,
            endpoint,
            model,
            [{"role": "user", "content": prompt}],
            api_key,
        )
    if is_anthropic(provider):
        return _call_anthropic_with_tools(
            api_key or "",
            model,
            [{"role": "user", "content": prompt}],
            "",
            bridge,
        )
    return _call_openai_with_tools(
        endpoint,
        model,
        [{"role": "user", "content": prompt}],
        api_key,
        bridge,
        provider=provider,
    )


def _split_setup_system_prompt(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    if messages and messages[0].get("role") == "user":
        return messages[0].get("content", ""), messages[1:]
    return "", messages


def _call_setup_model_text(
    config: AIConfig,
    endpoint: str,
    model: str,
    api_key: str | None,
    messages: list[dict[str, str]],
) -> str:
    from quickhatch.ai.model_list import chat_completions_url, is_anthropic

    system_prompt, conversation = _split_setup_system_prompt(messages)

    if _use_provider_native_search(config):
        if not conversation:
            conversation = [{"role": "user", "content": "Continue from the last action."}]
        return _call_model_with_native_search(
            config.provider,
            endpoint,
            model,
            conversation,
            api_key,
            system_prompt=system_prompt,
        )

    if is_anthropic(config.provider):
        a_msgs: list[dict[str, str]] = []
        for m in conversation:
            a_msgs.append(
                {
                    "role": "assistant" if m.get("role") == "assistant" else "user",
                    "content": m.get("content", ""),
                }
            )
        if not a_msgs:
            a_msgs.append({"role": "user", "content": "Begin the migration."})
        if a_msgs[0]["role"] != "user":
            a_msgs.insert(0, {"role": "user", "content": "Continue from the last action."})
        merged: list[dict[str, str]] = []
        for m in a_msgs:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] = merged[-1]["content"] + "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        return _post_anthropic(
            url=chat_completions_url(config.provider, endpoint),
            api_key=api_key or "",
            body={
                "model": model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": merged,
            },
        )

    body: dict[str, Any] = {"model": model, "messages": messages}
    if _is_reasoning_model(model):
        body["max_completion_tokens"] = 4096
    else:
        body["temperature"] = 0.3
        body["max_tokens"] = 4096
    return _post_openai_compat(
        url=chat_completions_url(config.provider, endpoint),
        api_key=api_key,
        body=body,
    )


def _prefetch_search_context(
    profile: UserProfile, bridge: BridgeState
) -> tuple[str, list[dict[str, str]]]:
    """Legacy seam kept for compatibility; analysis no longer prefetches web data.

    The selected model now decides whether to use web_search/image_search.
    Keeping this helper avoids wider churn in tests and call sites while
    making the analysis path deterministic: no harness-generated hardware
    searches, no raw-GPU loops, no duplicate search spam.
    """
    del profile, bridge
    return "", []


def _trim_text(value: Any, *, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _analysis_context_profile(
    profile: UserProfile,
    section: str,
    *,
    chosen_distro: str | None = None,
) -> dict[str, Any]:
    """Build a section-specific profile payload for the analysis agent.

    The old prompt dumped nearly the full profile into every section. That
    made local models miss high-value user preferences under irrelevant scan
    noise. Keep the context explicit and section-shaped instead.
    """
    prefs = profile.preferences
    base: dict[str, Any] = {
        "mode": prefs.migration_type,
        "preferences": {
            "use_cases": prefs.use_cases,
            "distro_preference": prefs.distro_preference,
            "desktop_look": prefs.desktop_look,
            "gallery_picks": prefs.gallery_picks,
            "icon_style": prefs.icon_style,
            "wallpaper_style": prefs.wallpaper_style,
            "another_machine": prefs.another_machine,
            "additional_notes": prefs.additional_notes,
        },
        "apps_to_install": profile.apps_to_install,
        "data_transfer_method": profile.data_transfer_method,
        "data_description": profile.data_description,
        "additional_info": profile.additional_info,
        "target_hardware_override": profile.target_hardware_override,
        "chosen_distro_so_far": chosen_distro or prefs.distro_preference or "",
        "ai_config": {
            "provider": profile.ai_config.provider,
            "endpoint": profile.ai_config.endpoint,
            "model_name": profile.ai_config.model_name,
            "api_key": "***REDACTED***" if profile.ai_config.api_key else None,
            "native_search": profile.ai_config.native_search,
        },
    }

    app_rows = [
        {
            "name": app.name,
            "version": app.version,
            "source": app.source,
        }
        for app in profile.apps
    ]
    if profile.apps_to_install:
        picks = {name.lower() for name in profile.apps_to_install}
        app_rows = [row for row in app_rows if (row.get("name") or "").lower() in picks]
    from quickhatch.analysis.app_catalog import selected_app_catalog_context

    catalog_names = list(profile.apps_to_install or [])
    if not catalog_names and section in {"apps", "plan"}:
        catalog_names = [str(row.get("name") or "") for row in app_rows[:80]]
    selected_app_catalog = selected_app_catalog_context(
        catalog_names,
        list(profile.apps or []),
        distro_family=chosen_distro or prefs.distro_preference or "",
    )
    if selected_app_catalog:
        base["selected_app_catalog"] = selected_app_catalog

    if section == "distro":
        base["hardware"] = asdict(profile.hardware)
        base["installed_apps"] = []
        base["windows_settings"] = profile.windows_settings
    elif section == "apps":
        base["installed_apps"] = app_rows[:80]
    elif section == "drivers":
        base["hardware"] = asdict(profile.hardware)
        base["installed_apps"] = []
        base["windows_settings"] = profile.windows_settings
    elif section == "desktop":
        base["installed_apps"] = []
        base["windows_settings"] = profile.windows_settings
        base["files_summary"] = {
            "browser_profiles": (profile.files_summary or {}).get("browser_profiles", {}),
            "top_types": (profile.files_summary or {}).get("top_types", {}),
        }
    elif section == "plan":
        base["hardware"] = asdict(profile.hardware)
        base["windows_settings"] = profile.windows_settings
        base["files_summary"] = profile.files_summary
        base["linux_connection"] = {
            "linux_ip": profile.linux_ip,
            "linux_user": profile.linux_user,
        }
        base["ssh_public_key_path"] = profile.ssh_public_key_path
    elif section == "summary":
        base["installed_apps"] = []
        base["linux_connection"] = {
            "linux_ip": profile.linux_ip,
            "linux_user": profile.linux_user,
        }
    elif section == "install_script":
        base["hardware"] = asdict(profile.hardware)
        base["files_summary"] = profile.files_summary
        base["linux_connection"] = {
            "linux_ip": profile.linux_ip,
            "linux_user": profile.linux_user,
        }
        base["ssh_public_key_path"] = profile.ssh_public_key_path

    if base.get("target_hardware_override"):
        base["hardware"] = {
            "_note": (
                "Ignore scanned hardware when target_hardware_override is set. "
                "Use the override text as authoritative."
            )
        }

    profile_dict = json.loads(json.dumps(base, default=str))
    if profile_dict.get("preferences", {}).get("gallery_picks") == []:
        profile_dict["preferences"].pop("gallery_picks", None)
    return profile_dict


def _analysis_previous_outputs_block(previous_outputs: dict[str, dict[str, Any]] | None) -> str:
    if not previous_outputs:
        return ""
    ordered = ("distro", "apps", "drivers", "desktop", "plan", "summary")
    summaries: list[dict[str, Any]] = []
    for key in ordered:
        payload = previous_outputs.get(key)
        if not payload:
            continue
        item: dict[str, Any] = {"section": key, "title": payload.get("title", "")}
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata:
            item["metadata"] = metadata
        markdown = _trim_text(payload.get("markdown") or payload.get("content") or "", limit=400)
        if markdown:
            item["markdown_excerpt"] = markdown
        summaries.append(item)
    if not summaries:
        return ""
    return (
        "== DECISIONS ALREADY MADE (authoritative unless user feedback overrides) ==\n"
        + json.dumps(summaries, indent=2, default=str)
        + "\n\n"
    )


def _analysis_response_contract(section: str, title_hint: str) -> str:
    metadata_hint = ""
    if section == "distro":
        metadata_hint = (
            '    "recommended_distro": "Exact distro name",\n'
            '    "download_url": "https://official.example/iso"\n'
        )
    elif section == "desktop":
        metadata_hint = (
            '    "desktop_environment": "KDE Plasma / GNOME / ...",\n'
            '    "recipe_id": "gallery id if used",\n'
            '    "theme": "theme name",\n'
            '    "icons": "icon theme"\n'
        )
    elif section == "apps":
        metadata_hint = '    "apps_covered": ["Firefox", "Steam"]\n'
    elif section == "plan":
        metadata_hint = '    "requires_reboot": true\n'
    elif section == "summary":
        metadata_hint = '    "next_steps_count": 4\n'
    else:
        metadata_hint = '    "notes": "optional machine-readable facts"\n'

    return (
        "OUTPUT CONTRACT:\n"
        "Respond with exactly ONE JSON object. No prose outside JSON. No Markdown fences.\n"
        "The object shape is:\n"
        "{\n"
        f'  "title": "{title_hint}",\n'
        '  "markdown": "Human-readable Markdown for the wizard UI",\n'
        '  "metadata": {\n'
        f"{metadata_hint}"
        "  }\n"
        "}\n"
        "`markdown` must contain the real user-facing content for this section.\n"
        "Use `metadata` only for key facts the harness should carry into later sections.\n\n"
    )


def _build_section_prompt(
    profile: UserProfile,
    section: str,
    search_context: str,
    user_feedback: str = "",
    chosen_distro: str | None = None,
    vm_verify: bool = False,
    previous_outputs: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Build a prompt for a single section of the migration analysis.

    ``chosen_distro`` lets the caller pass a distro decided earlier in the
    analysis (e.g. extracted from the ``distro`` section's output) so
    downstream sections (``plan``, ``desktop``, ``summary``) can receive the
    matching install primer. If ``chosen_distro`` is None, falls back to
    the user's stated ``preferences.distro_preference``.
    """
    from quickhatch.ai.install_primer import primer_prompt_block, resolve_primer

    profile_dict = _analysis_context_profile(profile, section, chosen_distro=chosen_distro)
    profile_json = json.dumps(profile_dict, indent=2, default=str)
    override_text = (profile.target_hardware_override or "").strip()
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
    # actually matters (plan/install_script = writes install steps;
    # desktop = references package manager + init system; summary may
    # cite the chosen stack).
    # Priority: explicit chosen_distro arg > user's stated preference.
    primer_block = ""
    resolved_distro = chosen_distro or profile.preferences.distro_preference or ""
    if section in ("plan", "desktop", "summary", "install_script") and resolved_distro:
        if resolve_primer(resolved_distro) is not None:
            primer_block = primer_prompt_block(resolved_distro)

    primary_recipe = (
        profile.preferences.gallery_picks[0] if profile.preferences.gallery_picks else ""
    )
    title_hint = {
        "distro": "Recommended Distribution",
        "apps": "Application Mapping",
        "drivers": "Driver Notes",
        "desktop": "Desktop Environment",
        "plan": "Migration Plan",
        "summary": "Your Migration at a Glance",
        "install_script": "Offline Install Script",
    }.get(section, "Analysis")

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
            "Use a Markdown TABLE with columns: | App | Install Command | Source | Est Time | Notes |\n"
            "The install command must be the EXACT command for the chosen distro\n"
            "(from the install primer above). The `Source` column is one of\n"
            "`official-repo` / `aur-binary` / `aur-source` / `flatpak` / `appimage` /\n"
            "`snap` / `npm`. The `Est Time` column is a rough wall-clock estimate\n"
            "per app — use these calibration points:\n"
            "  - official-repo binary, small (<50MB): 5–20s\n"
            "  - official-repo binary, large (Steam, LibreOffice, nvidia stack): 30–90s\n"
            "  - aur-binary (-bin suffix, AppImage wrapper): 30–90s\n"
            "  - aur-source (builds from source, Rust/C++ projects): 5–30 MIN\n"
            "  - flatpak/appimage: 10–60s\n"
            "Notes are for post-install config only — e.g. 'enable Proton in Steam',\n"
            "'run git config --global user.email ...'. Skip apps that need no\n"
            "post-install setup rather than padding with 'works out of the box'.\n\n"
            "RESEARCH-FIRST RULE: If you are not 100% certain of a package's exact\n"
            "name in the chosen distro's ecosystem (e.g. is it `lmstudio-bin` or\n"
            "`lm-studio-bin`? does `claude-desktop-bin` exist on AUR? does the\n"
            "current openai-codex npm package require node-lts or node-current?),\n"
            "USE web search / web-capable tools before writing the row.\n"
            "It is FAR better to output a correct row from a 5-second lookup than\n"
            "an incorrect row the setup agent then spends 10 minutes failing on.\n"
            "Package names change; your training data is stale. Search.\n\n"
            "SELECTED APP CATALOG: The profile may include `selected_app_catalog`.\n"
            "Treat those rows as curated prior knowledge for the user's selected\n"
            "apps: catalog_id, compatibility, source_hint, confidence, category,\n"
            "notes, evidence, and active_install_hint when a distro family is known.\n"
            "Prefer high-confidence catalog hints. Treat active_install_hint as\n"
            "advisory package-resolution data that must still be verified when the\n"
            "source is web/manual_followup/alternative or package names are empty.\n"
            "If confidence is\n"
            "medium/low or the exact distro package is unclear, search before\n"
            "writing the install command. Do not expand beyond the selected apps.\n\n"
            "CALL OUT SLOW INSTALLS: For any row with Est Time ≥ 10 min (big AUR\n"
            "source builds), add a 🐌 emoji to the app name and in Notes write:\n"
            "'Slow install — consider AppImage/flatpak alternative: <link>'. This\n"
            "lets the setup agent proactively ask the user whether to proceed or\n"
            "swap for a faster path.\n\n"
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

    if section == "install_script":
        response_contract = (
            "Respond with ONLY a bash script. No JSON, no prose, no Markdown fences.\n\n"
        )
    else:
        response_contract = _analysis_response_contract(section, title_hint)

    return (
        "You are a Linux migration expert. Write the migration analysis section described below.\n\n"
        "KEY PRINCIPLE: prefer authoritative user input over your own stylistic guesses.\n"
        "Respect explicit user choices. Carry forward decisions already made in earlier sections.\n"
        "Describe the full working END-STATE, not just package installation.\n\n"
        f"== SECTION: {section.upper()} ==\n"
        f"{section_instructions.get(section, '')}\n\n"
        + response_contract
        + (
            "== USER FEEDBACK (authoritative latest input — the user changed their mind, follow this) ==\n"
            f"{user_feedback}\n\n"
            if user_feedback
            else ""
        )
        + _analysis_previous_outputs_block(previous_outputs)
        + f"== RELEVANT USER SYSTEM PROFILE ==\n{profile_json}\n\n"
        + (
            "== TARGET HARDWARE OVERRIDE (AUTHORITATIVE) ==\n"
            "The scanned hardware above is NOT the install target. Use the text below for all "
            "driver / GPU / CPU / WiFi / disk-size decisions. If something is missing, say "
            "'unknown — ask the user' instead of inventing values.\n\n"
            f"{override_text}\n\n"
            if override_text
            else ""
        )
        + primer_block
        + gallery_block
        + library_block
        + (vm_block + "\n" if vm_block else "")
        + (
            f"== LIVE SEARCH RESULTS (supplemental, lower priority than user data) ==\n{search_context}\n\n"
            if search_context
            else ""
        )
    )


def _extract_openai_text(payload: Any) -> str:
    """Pull the visible text out of an OpenAI-compatible chat/completions
    response, defensively. Handles missing fields, empty choices, and
    reasoning-style models that put output in `reasoning_content`.
    Returns '' rather than raising so callers don't tear down the
    agent loop on a malformed 200 response.
    """
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices, list):
        return ""
    msg = (choices[0] or {}).get("message") or {}
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return text if isinstance(text, str) else ""


def _extract_anthropic_text(payload: Any) -> str:
    """Same idea for Anthropic's /v1/messages shape."""
    if not isinstance(payload, dict):
        return ""
    blocks = payload.get("content") or []
    if not isinstance(blocks, list):
        return ""
    out: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                out.append(t)
    return "".join(out)


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


def _strip_markdown_fences(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_first_json_dict(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    objects = _extract_json_objects(text)
    return objects[0] if objects else None


def _render_analysis_section_markdown(section: str, payload: dict[str, Any]) -> str:
    markdown = str(payload.get("markdown") or "").strip()
    if markdown:
        return markdown

    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if section == "distro":
        distro = (
            metadata.get("recommended_distro")
            or payload.get("recommended_distro")
            or payload.get("top_pick")
            or "Unknown"
        )
        reasoning = payload.get("reasoning") or payload.get("content") or ""
        lines = [f"### Recommended Distribution\n\n**{distro}**"]
        if reasoning:
            lines.append(f"\n\n{reasoning}")
        return "".join(lines).strip()

    if section == "apps":
        apps = payload.get("apps")
        if isinstance(apps, list) and apps:
            lines = [
                "| App | Install Command | Notes |",
                "| --- | --- | --- |",
            ]
            for app in apps:
                if not isinstance(app, dict):
                    continue
                lines.append(
                    "| {name} | `{cmd}` | {notes} |".format(
                        name=app.get("name", ""),
                        cmd=app.get("install_command", "") or app.get("command", ""),
                        notes=app.get("notes", ""),
                    )
                )
            return "\n".join(lines)

    if section == "desktop":
        lines = ["### Desktop Environment"]
        de = (
            metadata.get("desktop_environment")
            or payload.get("desktop_environment")
            or payload.get("de")
        )
        if de:
            lines.append(f"\n\n- **Desktop:** {de}")
        for label, key in (
            ("Recipe", "recipe_id"),
            ("Theme", "theme"),
            ("Icons", "icons"),
            ("Wallpaper", "wallpaper"),
        ):
            value = metadata.get(key) or payload.get(key)
            if value:
                lines.append(f"\n- **{label}:** {value}")
        notes = payload.get("notes") or payload.get("content") or ""
        if notes:
            lines.append(f"\n\n{notes}")
        return "".join(lines).strip()

    if section == "plan":
        steps = payload.get("steps")
        if isinstance(steps, list) and steps:
            lines = ["### Migration Plan\n"]
            for idx, step in enumerate(steps, start=1):
                lines.append(f"{idx}. {step}")
            return "\n".join(lines)

    if section == "summary":
        checklist = payload.get("next_steps")
        summary = payload.get("summary") or payload.get("content") or ""
        lines = []
        if summary:
            lines.append(summary)
        if isinstance(checklist, list) and checklist:
            lines.append("\n### What to do next")
            for item in checklist:
                lines.append(f"- {item}")
        return "\n".join(lines).strip()

    fallback = str(payload.get("content") or payload.get("summary") or "").strip()
    return fallback


def _parse_analysis_section_response(section: str, text: str) -> dict[str, Any]:
    cleaned = _strip_markdown_fences(text)
    parsed = _extract_first_json_dict(cleaned)
    if parsed is None:
        return {
            "title": "",
            "markdown": cleaned,
            "metadata": {},
            "structured": False,
        }

    if section in parsed and isinstance(parsed[section], dict):
        parsed = parsed[section]

    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    markdown = _render_analysis_section_markdown(section, parsed).strip()
    title = str(parsed.get("title") or "").strip()
    return {
        "title": title,
        "markdown": markdown or cleaned,
        "metadata": metadata,
        "structured": True,
        "raw": parsed,
    }


_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _is_reasoning_model(model: str | None) -> bool:
    """True for OpenAI's reasoning series (gpt-5, o1, o3, o4-mini, …).

    These models reject the standard `temperature` parameter and require
    `max_completion_tokens` instead of `max_tokens`. Detecting by prefix
    is cheap and matches OpenAI's naming convention; other providers
    may adopt similar schemes later.
    """
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) or m.startswith(f"openai/{p}") for p in _REASONING_MODEL_PREFIXES)


def _call_model_simple(
    endpoint: str,
    model: str,
    prompt: str,
    api_key: str | None,
    provider: str | None = None,
) -> str:
    """Simple single-turn LLM call (no tools). Works for any provider.

    Uses the explicit ``provider`` label to choose Anthropic vs
    OpenAI-compat shape — the legacy substring check on `endpoint`
    misrouted custom endpoints whose hostname contained 'anthropic'.
    """
    from quickhatch.ai.model_list import chat_completions_url, is_anthropic

    if is_anthropic(provider):
        return _post_anthropic(
            url=chat_completions_url(provider, endpoint),
            api_key=api_key or "",
            body={
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    return _post_openai_compat(
        url=chat_completions_url(provider, endpoint),
        api_key=api_key,
        body={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 4096,
        },
    )


def _http_post_with_retry(url: str, headers: dict, body: dict, timeout: int) -> object:
    """POST with 2-attempt retry on 429/5xx, honoring Retry-After.

    Single attempt for non-retryable failures (4xx other than 429).
    Returns the requests.Response object on success; raises
    requests.HTTPError with the response body bytes attached as the
    last argument so the caller can surface a useful message.
    """
    import time as _time

    import requests

    for attempt in range(3):
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code < 400:
            return resp
        # Retry only on 429 / 5xx.
        if resp.status_code != 429 and resp.status_code < 500:
            break
        if attempt == 2:
            break
        ra = resp.headers.get("Retry-After")
        try:
            wait = float(ra) if ra else (2**attempt)
        except ValueError:
            wait = 2**attempt
        _time.sleep(min(wait, 30))
    # Build a useful HTTPError that includes a snippet of the response
    # body — without it the caller sees only "400 Client Error".
    body_snip = ""
    try:
        body_snip = resp.text[:500]
    except Exception:
        pass
    err = requests.HTTPError(f"HTTP {resp.status_code} from {url}: {body_snip}", response=resp)
    raise err


def _post_anthropic(url: str, api_key: str, body: dict) -> str:
    resp = _http_post_with_retry(
        url=url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body=body,
        timeout=900,
    )
    return _extract_anthropic_text(resp.json())


def _post_openai_compat(url: str, api_key: str | None, body: dict) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = _http_post_with_retry(url=url, headers=headers, body=body, timeout=900)
    # _extract_openai_text handles missing choices/message/content and
    # falls back to reasoning_content when content is empty.
    return _extract_openai_text(resp.json())


def _run_agent_background(gui_state: GUIState, port: int, user_feedback: str = "") -> None:
    """Run the AI agent in a background thread, posting results to the bridge.

    Strategy: build one prompt per section and let the selected model decide
    whether to use web-search tools. The harness supplies authoritative facts,
    not precomputed hardware search queries.
    """
    import requests as req

    base = f"http://localhost:{port}"
    bridge = gui_state.bridge
    # Tag every add_message() in this thread as analysis-phase so the
    # UI can filter out setup-phase noise from prior runs.
    bridge.set_current_phase("analysis")
    config = gui_state.profile.ai_config

    try:
        bridge.add_message("agent", "Connected — reading your profile.")

        search_context = ""
        desktop_images: list[dict[str, str]] = []

        endpoint = config.endpoint or ""
        model = config.model_name or ""
        api_key = config.api_key
        native_search_active = _use_provider_native_search(config)

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
        analysis_outputs: dict[str, dict[str, Any]] = {}

        # Build a map of sections that already have a SUCCESSFUL suggestion
        # on disk from a previous run. If the user is re-running without
        # having cleared state (e.g. after a server crash / restart), we
        # skip those sections entirely and only redo the ones that failed
        # or never ran. Placeholder '(failed)' suggestions don't count.
        already_done: set[str] = set()
        for s in bridge.snapshot_suggestions():
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
            # the new attempt can replace it cleanly. Go through the
            # locked replace_suggestions API so a concurrent GET
            # /api/suggestions can't see a half-mutated list.
            kept = [
                s
                for s in bridge.snapshot_suggestions()
                if not (
                    getattr(s, "category", "") == section_key
                    and (getattr(s, "title", "") or "").endswith("(failed)")
                )
            ]
            bridge.replace_suggestions(kept)
            bridge.add_message("agent", f"Generating {title.lower()}…")

            prompt = _build_section_prompt(
                gui_state.profile,
                section_key,
                search_context,
                user_feedback,
                chosen_distro=chosen_distro,
                vm_verify=bool(getattr(gui_state, "vm_verify_enabled", False)),
                previous_outputs=analysis_outputs,
            )
            # Retry once on any network/timeout error — a 15 min timeout plus
            # a second attempt is usually enough for even the biggest local
            # models. If the second try still fails, post an error suggestion
            # for this section and continue with the next rather than
            # crashing the whole analysis run.
            # Retry up to 3 times with exponential backoff (3s, 9s, 27s).
            # Cloud providers under load often 503 or stall for 30+
            # seconds; a single retry at 10s wasn't enough for Gemini's
            # busy moments and the user saw spurious failures.
            import time as _t

            last_exc = None
            content = None
            for attempt in range(1, 4):
                attempt_started = _t.monotonic()
                try:
                    content = _call_model_analysis(
                        endpoint,
                        model,
                        prompt,
                        api_key,
                        bridge,
                        provider=config.provider,
                        use_native_search=native_search_active,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 3:
                        elapsed = max(1, int(round(_t.monotonic() - attempt_started)))
                        wait = 3 * (3 ** (attempt - 1))  # 3, 9, 27
                        bridge.add_message(
                            "agent",
                            f"{title} API call slow ({attempt}/3) — attempt took {elapsed}s, retrying in {wait}s…",
                        )
                        _t.sleep(wait)
            if content is None:
                bridge.add_message(
                    "agent",
                    f"❌  {title} failed after 3 attempts — skipping. "
                    f"Last error: {str(last_exc)[:200]}",
                )
                req.post(
                    f"{base}/api/suggest",
                    json={
                        "category": section_key,
                        "title": f"{title} (failed)",
                        "content": (
                            "This section couldn't be generated after three "
                            f"attempts.\n\n**Last error:** `{str(last_exc)[:200]}`\n\n"
                            "Click 'Rerun analysis' to try again — the model "
                            "was probably just under load."
                        ),
                    },
                    timeout=5,
                )
                continue

            content = content.strip()

            parsed_section: dict[str, Any] | None = None
            if section_key != "install_script":
                parsed_section = _parse_analysis_section_response(section_key, content)
                content = parsed_section["markdown"].strip()
                analysis_outputs[section_key] = {
                    "title": parsed_section.get("title") or title,
                    "markdown": content,
                    "metadata": parsed_section.get("metadata") or {},
                }
                if parsed_section.get("structured"):
                    bridge.add_message(
                        "agent",
                        f"{title} returned structured JSON — rendering for the wizard.",
                    )
            else:
                content = _strip_markdown_fences(content)

            # After the distro section runs, scan its output for a known
            # distro identifier. Whichever our primer library recognizes
            # becomes the ``chosen_distro`` for downstream sections (plan,
            # desktop, summary) so they get the matching install primer.
            # Only overrides when the section actually found a match — if
            # the distro section is vague, fall back to whatever was seeded
            # from user preference.
            if section_key == "distro":
                from quickhatch.ai.install_primer import resolve_primer

                match = None
                if parsed_section is not None:
                    metadata = parsed_section.get("metadata") or {}
                    metadata_distro = metadata.get("recommended_distro")
                    if metadata_distro:
                        match = resolve_primer(
                            str(metadata_distro),
                            preferred=gui_state.profile.preferences.distro_preference or None,
                        )
                if match is None:
                    match = resolve_primer(
                        content,
                        preferred=gui_state.profile.preferences.distro_preference or None,
                    )
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


def _prepare_full_rerun(bridge_state: BridgeState, user_msg: str) -> None:
    """Reset analysis state only after a replacement launch is admitted."""
    bridge_state.reset_for_rerun()
    if user_msg:
        bridge_state.add_message("user", user_msg)


def _prepare_section_rerun(
    bridge_state: BridgeState,
    invalidated_sections: set[str],
    user_msg: str,
) -> None:
    """Invalidate one section and its dependents only after launch admission."""
    kept = [
        s
        for s in bridge_state.snapshot_suggestions()
        if getattr(s, "category", "") not in invalidated_sections
    ]
    bridge_state.replace_suggestions(kept)
    bridge_state.prepare_for_launch()
    if user_msg:
        bridge_state.add_message("user", user_msg)


def _make_managed_agent_target(
    gui_state: GUIState,
    phase: str,
    target: Callable[..., None],
    *args: Any,
    **kwargs: Any,
) -> Callable[[], None]:
    """Wrap an agent worker so runtime state is always cleaned up.

    The old launcher started `_run_agent_background` / `_run_setup_agent`
    directly, which meant:
    - `bridge.agent_connected` stayed sticky after the worker exited
    - `gui_state._agent_thread` could keep pointing at a dead thread
    - `current_phase` remained latched, so later UI actions inherited stale phase
    """

    def runner() -> None:
        gui_state.mark_agent_running(phase)
        current = threading.current_thread()
        try:
            target(*args, **kwargs)
        finally:
            gui_state.mark_agent_stopped(current)

    return runner


def _launch_agent(
    gui_state: GUIState,
    port: int,
    user_feedback: str = "",
    phase: str = "analysis",
    prelaunch: Callable[[], None] | None = None,
) -> dict:
    """Launch the AI agent — calls the configured API in a background thread."""
    config = gui_state.profile.ai_config
    provider = config.provider or "api"

    # Refuse concurrent launches — a second agent thread would race with
    # the first on bridge.suggestions, messages, and agent_done. Any
    # state reset associated with a rerun must happen only AFTER the new
    # slot is admitted.
    with gui_state.lock:
        existing_thread = gui_state._agent_thread
        if existing_thread is not None and not existing_thread.is_alive():
            gui_state._agent_thread = None
            with gui_state.bridge.lock:
                gui_state.bridge.agent_connected = False
                gui_state.bridge.stop_requested = False
                gui_state.bridge.current_phase = ""
                if gui_state.bridge.setup_run.get("status") == "stopping":
                    gui_state.bridge.setup_run["status"] = "stopped"
        if gui_state._agent_thread is not None and gui_state._agent_thread.is_alive():
            return {
                "status": "error",
                "message": "An agent is already running. Click Stop first (or wait for it).",
            }

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
                    sh_prompt_file = "'" + str(prompt_file).replace("'", "'\\''") + "'"
                    shell_cmd = f'codex "$(cat {sh_prompt_file})"'
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
                if prelaunch is not None:
                    prelaunch()
                return {"status": "ok", "message": "Codex launched in a new terminal."}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        # For API-based providers (LM Studio, Anthropic, OpenAI), run in background
        if not config.endpoint:
            return {
                "status": "error",
                "message": "No API endpoint configured. Go back to AI setup.",
            }
        if prelaunch is not None:
            prelaunch()

        if phase == "setup":
            thread = threading.Thread(
                target=_make_managed_agent_target(
                    gui_state,
                    phase,
                    _run_setup_agent,
                    gui_state,
                    port,
                    user_feedback=user_feedback,
                ),
                daemon=True,
            )
        else:
            thread = threading.Thread(
                target=_make_managed_agent_target(
                    gui_state,
                    phase,
                    _run_agent_background,
                    gui_state,
                    port,
                    user_feedback=user_feedback,
                ),
                daemon=True,
            )
        gui_state._agent_thread = thread
        thread.start()

    if phase == "setup":
        return {"status": "ok", "message": "Setup agent started! Connecting via SSH..."}
    return {"status": "ok", "message": "Agent started! Analyzing your system..."}


def _extract_json_objects(text: str) -> list[dict]:
    """Extract one or more top-level JSON objects from text.

    Handles cases where the model emits multiple JSON objects in one response
    (e.g. a status followed by an ssh_exec), possibly with text between them.

    Safe against malformed input:
    - cap the number of objects (prevents runaway memory on weird text)
    - always advance `i` at least once per outer-loop iteration
    - bail out if we've scanned the whole tail without finding a close
    """
    objects: list[dict] = []
    i = 0
    n = len(text)
    MAX_OBJECTS = 50  # any sane agent response < this
    while i < n and len(objects) < MAX_OBJECTS:
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
            if c == '"':
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
            chunk = text[start:end]
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                # Models frequently emit JSON with LITERAL newlines inside
                # string values (e.g. heredoc shell commands). Strict
                # json.loads rejects those. Retry after escaping newlines
                # and carriage returns that sit inside string runs.
                try:
                    import re as _re

                    # Also fix INVALID backslash escapes inside string
                    # values. Models frequently paste shell regex like
                    # `\[multilib\]` into a JSON string — those are not
                    # legal JSON escapes and json.loads rejects them.
                    # Replace any `\X` where X isn't a recognised JSON
                    # escape char with `\\X` so the backslash is
                    # interpreted literally.
                    # Bind via default arg so ruff's B023 doesn't flag
                    # the closure for capturing a "loop variable" — and
                    # also because it's marginally faster than a free
                    # variable lookup.
                    def _fix_body(body: str, _VALID_ESC=frozenset('"\\/bfnrtu')) -> str:
                        out: list[str] = []
                        k = 0
                        while k < len(body):
                            ch = body[k]
                            if ch == "\\" and k + 1 < len(body):
                                nxt = body[k + 1]
                                if nxt in _VALID_ESC:
                                    out.append(ch)
                                    out.append(nxt)
                                    k += 2
                                    continue
                                # Invalid escape — double the backslash.
                                out.append("\\\\")
                                k += 1
                                continue
                            out.append(ch)
                            k += 1
                        return (
                            "".join(out)
                            .replace("\r", "\\r")
                            .replace("\n", "\\n")
                            .replace("\t", "\\t")
                        )

                    repaired = _re.sub(
                        r'"((?:[^"\\]|\\.)*)"',
                        lambda m: '"' + _fix_body(m.group(1)) + '"',
                        chunk,
                    )
                    obj = json.loads(repaired)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except (json.JSONDecodeError, Exception):
                    pass
            # Always advance past the `{` even on unparseable chunk —
            # otherwise an unterminated string inside leaves us stuck.
            i = max(end, start + 1)
        else:
            # No close brace found from this `{` all the way to EOF —
            # nothing further to extract. Bail.
            break
    return objects


def _ssh_exec(
    host: str, user: str, cmd: str, key_path: str | None = None, timeout: int = 120, port: int = 22
) -> tuple[int, str, str]:
    """Execute a command on a remote host via SSH. Returns (returncode, stdout, stderr)."""
    import socket as _socket
    import subprocess

    # Quick TCP reachability probe before spawning ssh. Do not read and
    # validate the SSH banner here: rapid setup command loops can trip
    # sshd MaxStartups/rate-limiting text such as "Not allowed" on this
    # extra probe connection, causing a false harness failure before the
    # real ssh client even runs.
    try:
        with _socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        return -1, "", f"cannot reach {host}:{port} ({exc.__class__.__name__})"

    ssh_args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        # Don't touch the user's known_hosts. Otherwise, rebuilding the
        # target (which produces a new host key) trips 'REMOTE HOST
        # IDENTIFICATION HAS CHANGED' on every subsequent connection and
        # the agent can't SSH back in until the user manually clears
        # ~/.ssh/known_hosts.
        "-o",
        "UserKnownHostsFile=/dev/null",
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
    except OSError as exc:
        # Catches e.g. WinError 193 (corrupt ssh.exe), permission errors,
        # broken pipes mid-handshake. Previously propagated and killed the
        # agent thread with no UI feedback.
        return -1, "", f"ssh invocation failed: {exc}"


_SETUP_CMD_DEFAULT_TIMEOUT = 1800
_SETUP_CMD_LONG_TIMEOUT = 3600
_SETUP_CMD_MIN_TIMEOUT = 30
_SETUP_CMD_MAX_TIMEOUT = 21600
_SETUP_LONG_COMMAND_MARKERS = (
    "pacstrap",
    "debootstrap",
    "dnf --installroot",
    "dnf install",
    "pacman -S",
    "apt install",
    "apt-get install",
    "zypper install",
    "emerge",
    "yay ",
    "yay -S",
    "paru ",
    "paru -S",
    "xbps-install",
    "flatpak install",
    "snap install",
    "makepkg",
    "cmake --build",
    "cargo build",
    "cargo install",
    "npm install",
    "pip install",
    "mkinitcpio",
    "grub-mkconfig",
    "dracut",
)


def _resolve_setup_cmd_timeout(cmd: str, action: dict[str, Any]) -> int:
    """Pick the SSH timeout for one setup-agent command.

    Default every setup command to 30 minutes. Commands that routinely build,
    install, or compile large payloads get 60 minutes automatically.

    The model may request a specific timeout with ``timeout_seconds`` on the
    ``ssh_exec`` action. Clamp it to a sane range so one bad response can't
    wedge the harness for an entire day.
    """
    override = action.get("timeout_seconds")
    if override is not None:
        try:
            return max(_SETUP_CMD_MIN_TIMEOUT, min(int(override), _SETUP_CMD_MAX_TIMEOUT))
        except (TypeError, ValueError):
            pass
    return (
        _SETUP_CMD_LONG_TIMEOUT
        if any(marker in cmd for marker in _SETUP_LONG_COMMAND_MARKERS)
        else _SETUP_CMD_DEFAULT_TIMEOUT
    )


_SETUP_FACT_PREFIX = "QH_"
_ARCH_AUR_ONLY_PACKAGES = frozenset(
    {
        "brave-bin",
        "github-desktop-bin",
        "cursor-bin",
        "lm-studio-bin",
        "mullvad-vpn-bin",
        "claude-desktop-bin",
        "chatgpt-desktop-bin",
        "codex-bin",
        "localsend-bin",
        "stirling-pdf-bin",
        "docker-desktop",
        "catppuccin-kde-theme-git",
        "catppuccin-gtk-theme-mocha",
        "sweet-rainbow-icon-theme-git",
        "bibata-cursor-theme",
    }
)
_AURISH_SUFFIXES = ("-bin", "-git")
_HEAVY_OPTIONAL_DESKTOP_PACKAGES = frozenset(
    {
        "pake-chatgpt",
        "chatgpt-desktop-bin",
        "claude-desktop-bin",
        "openai-codex-desktop",
        "codex-desktop-bin",
    }
)


def _probe_setup_facts(
    host: str,
    user: str,
    key_path: str | None,
    port: int = 22,
    *,
    fallback: dict[str, str] | None = None,
) -> dict[str, str]:
    """Collect compact runtime facts for the setup agent prompt/guards."""
    probe_cmd = r"""
echo 'QH_PM='$(if command -v pacman >/dev/null 2>&1; then echo pacman; elif command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then echo apt; elif command -v dnf >/dev/null 2>&1; then echo dnf; elif command -v zypper >/dev/null 2>&1; then echo zypper; elif command -v apk >/dev/null 2>&1; then echo apk; elif command -v xbps-install >/dev/null 2>&1; then echo xbps; elif command -v emerge >/dev/null 2>&1; then echo emerge; else echo unknown; fi)
echo 'QH_AUR='$(if command -v yay >/dev/null 2>&1; then echo yay; elif command -v paru >/dev/null 2>&1; then echo paru; else echo none; fi)
echo 'QH_USER='$(whoami 2>/dev/null || echo unknown)
echo 'QH_IS_ROOT='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; else echo no; fi)
echo 'QH_SUDO_NOPASSWD='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi)
"""
    rc, out, _ = _ssh_exec(host, user, probe_cmd, key_path, timeout=30, port=port)
    facts = dict(
        fallback
        or {
            "package_manager": "unknown",
            "aur_helper": "none",
            "current_user": user,
            "is_root": "no",
            "sudo_nopasswd": "no",
        }
    )
    facts.setdefault("package_manager", "unknown")
    facts.setdefault("aur_helper", "none")
    facts.setdefault("current_user", user)
    facts.setdefault("is_root", "no")
    facts.setdefault("sudo_nopasswd", "no")
    if rc != 0:
        return facts
    mapping = {
        f"{_SETUP_FACT_PREFIX}PM": "package_manager",
        f"{_SETUP_FACT_PREFIX}AUR": "aur_helper",
        f"{_SETUP_FACT_PREFIX}USER": "current_user",
        f"{_SETUP_FACT_PREFIX}IS_ROOT": "is_root",
        f"{_SETUP_FACT_PREFIX}SUDO_NOPASSWD": "sudo_nopasswd",
    }
    for line in out.splitlines():
        if "=" not in line or not line.startswith(_SETUP_FACT_PREFIX):
            continue
        key, value = line.split("=", 1)
        mapped = mapping.get(key.strip())
        if mapped:
            facts[mapped] = value.strip() or facts[mapped]
    return facts


def _setup_runtime_prompt_block(
    facts: dict[str, str], is_live: bool, *, native_search_active: bool = False
) -> str:
    """Render execution facts and distro-aware package rules for the setup LLM."""
    package_manager = facts.get("package_manager", "unknown")
    aur_helper = facts.get("aur_helper", "none")
    current_user = facts.get("current_user", "unknown")
    is_root = facts.get("is_root", "no") == "yes"
    sudo_nopasswd = facts.get("sudo_nopasswd", "no") == "yes"

    lines = [
        "== SETUP RUNTIME FACTS ==",
        f"- Mode: {'live installer' if is_live else 'installed system (in-place config)'}",
        f"- Current SSH login: {current_user}",
        f"- Current login is root: {'yes' if is_root else 'no'}",
        f"- Passwordless sudo already works: {'yes' if sudo_nopasswd else 'no'}",
        f"- Detected package manager: {package_manager}",
    ]
    if package_manager == "pacman":
        lines.append(f"- Detected AUR helper: {aur_helper}")
    lines.extend(
        [
            "",
            "PACKAGE-MANAGER DISCIPLINE:",
        ]
    )
    if package_manager == "pacman":
        package_lookup_line = (
            "- If `pacman -S` says a target was not found, stop guessing. Search natively or use a harness lookup action, then install the exact discovered package name."
            if native_search_active
            else "- If `pacman -S` says a target was not found, stop guessing. Use the `repo_search`, `aur_search`, or `web_search` action next, then install the exact discovered package name."
        )
        lines.extend(
            [
                "- Official Arch packages, drivers, and core desktop packages must go through `pacman`.",
                "- AUR-only packages must NOT be passed to `pacman`. Use the detected AUR helper or a manual `makepkg -s` + `pacman -U` flow.",
                "- Treat names ending in `-bin` or `-git` as AUR candidates unless you have a concrete search result proving otherwise.",
                package_lookup_line,
                "- Do not fire a chain of alternative package names for the same app without a search result between them.",
            ]
        )
        if aur_helper == "none":
            lines.append(
                "- No AUR helper is currently installed. If you need AUR packages, first install `yay`/`paru` or build manually with `makepkg`."
            )
    elif package_manager == "apt":
        lines.extend(
            [
                "- Use `apt`/`apt-get` for package installation on this machine.",
                "- Do not emit `pacman`, `yay`, or `paru` commands here.",
            ]
        )
    elif package_manager == "dnf":
        lines.extend(
            [
                "- Use `dnf` for package installation on this machine.",
                "- Do not emit `pacman`, `yay`, `paru`, or `apt` commands here.",
            ]
        )
    elif package_manager == "zypper":
        lines.extend(
            [
                "- Use `zypper` for package installation on this machine.",
                "- Do not emit `pacman`, `yay`, `paru`, or `apt` commands here.",
            ]
        )
    if not is_live:
        lines.extend(
            [
                "",
                "INSTALLED-SYSTEM GUARDRAIL:",
                "- This machine is already installed. Work in-place on the current OS and user account.",
                "- Do NOT repartition disks, mkfs, mount `/mnt`, run `pacstrap`, `debootstrap`, `dnf --installroot`, or reinstall the bootloader unless the user explicitly asked for a reinstall.",
                "- If the migration plan contains live-installer steps, ignore those and execute only the post-install configuration parts.",
            ]
        )
    lines.extend(
        [
            "",
            "NETWORK-SECURITY GUARDRAIL:",
            "- Do NOT enable/start VPN or firewall services automatically.",
            "- Installing packages like Mullvad, WireGuard, OpenVPN, firewalld, ufw, or nftables is allowed, but activating them requires an explicit user request in the current run.",
            "- If the user did not ask for network-security changes right now, leave those services stopped and mention any manual follow-up instead of changing routing, DNS, firewall rules, or kill-switch behavior remotely.",
        ]
    )
    if native_search_active:
        lines.extend(
            [
                "",
                "SEARCH MODE:",
                "- Provider-native web search is enabled for this run.",
                "- Use the model provider's own search capability inside the model when package names or distro instructions are uncertain.",
                "- Harness lookup actions remain allowed when you need auditable target-specific results fed back into the next turn.",
                "- In typed staged setup use `repo_search`, `community_search`, or `web_search`; in legacy action mode use `repo_search`, `aur_search`, or `web_search`.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "LOOKUP ACTIONS (use these aggressively — you will be PENALISED for guessing package names):",
                "- `repo_search` asks the target OS package manager for matching packages and returns compact results.",
                "- `aur_search` looks up AUR packages on Arch-family targets and falls back to a web search of aur.archlinux.org when needed.",
                "- `web_search` performs an internet search for current package names, distro-specific instructions, and project pages. USE THIS FIRST whenever a package or command syntax is uncertain — the harness has live internet access and the 10s search beats 5 minutes of broken pacman retries.",
                "- When the exact package name is uncertain, use a lookup action BEFORE `ssh_exec`.",
                "- If any install command fails with 'target not found', 'unable to locate package', or 'no match', your very next action must be a lookup action instead of another guessed install command.",
                "",
                "USER INTERACTION:",
                '- `ask` posts a BLOCKING question to the user and waits up to 10 minutes for their answer. Use it whenever you need a decision that\'s outside the plan (e.g. \'The LUKS step is about to run — keep encryption or skip?\', \'This AUR build will take ~20 minutes — proceed or swap for an AppImage?\'). Schema: `{"action": "ask", "question": "…", "choices": ["yes", "no"], "timeout": 600}`. `choices` is optional; omit for free text. The user\'s answer comes back as the next feedback message.',
                "- `message` is one-way — use it for status updates. If you actually need an answer, use `ask`; posting a message and then running a command that depends on the answer is a bug.",
            ]
        )
    return "\n".join(lines) + "\n\n"


def _collect_remote_system_info(
    host: str, user: str, key_path: str | None, *, port: int = 22
) -> str:
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
    rc, system_info, _ = _ssh_exec(host, user, info_cmd, key_path, port=port)
    return system_info.strip() if rc == 0 else "Could not gather system info"


def _probe_setup_facts_typed(
    host: str,
    user: str,
    key_path: str | None,
    *,
    port: int = 22,
    fallback: SetupFacts | None = None,
    is_live: bool = False,
    live_marker: str = "",
    pending_question_id: str = "",
    pending_question_blocking: bool = False,
) -> SetupFacts:
    facts = _probe_typed_setup_facts(
        lambda ssh_host, ssh_user, cmd, ssh_key, timeout, ssh_port: _ssh_exec(
            ssh_host, ssh_user, cmd, ssh_key, timeout=timeout, port=ssh_port
        ),
        host,
        user,
        key_path,
        port=port,
        fallback=fallback,
        is_live=is_live,
        live_marker=live_marker,
        pending_question_id=pending_question_id,
        pending_question_blocking=pending_question_blocking,
    )
    facts.distro_family = detect_family(facts)
    return facts


def _bootstrap_setup_facts_typed(
    host: str,
    user: str,
    key_path: str | None,
    profile: UserProfile,
    bridge: BridgeState,
    facts: SetupFacts,
    *,
    port: int = 22,
) -> SetupFacts:
    if facts.is_root or facts.sudo_nopasswd:
        return facts
    _maybe_bootstrap_nopasswd_sudo(
        host,
        user,
        key_path,
        profile,
        bridge,
        facts={
            "package_manager": facts.package_manager,
            "aur_helper": facts.community_helper,
            "current_user": facts.current_user,
            "is_root": "yes" if facts.is_root else "no",
            "sudo_nopasswd": "yes" if facts.sudo_nopasswd else "no",
        },
        port=port,
    )
    return _probe_setup_facts_typed(
        host,
        user,
        key_path,
        port=port,
        fallback=facts,
        is_live=facts.is_live,
        live_marker=facts.live_marker,
        pending_question_id=facts.pending_question_id,
        pending_question_blocking=facts.pending_question_blocking,
    )


def _maybe_bootstrap_nopasswd_sudo(
    host: str,
    user: str,
    key_path: str | None,
    profile: UserProfile,
    bridge: BridgeState,
    *,
    facts: dict[str, str],
    port: int = 22,
) -> dict[str, str]:
    """Try to grant passwordless sudo early on installed-system runs.

    This prevents invisible sudo prompts from consuming the whole setup run
    before the model has a chance to make progress.
    """
    if facts.get("is_root") == "yes" or facts.get("sudo_nopasswd") == "yes":
        return facts
    default_pw = profile.default_account_password or "password"
    grant_cmd = (
        f"echo '{default_pw}' | sudo -S -p '' bash -c "
        f"\"echo '{user} ALL=(ALL:ALL) NOPASSWD: ALL' > "
        f"/etc/sudoers.d/99-quickhatch-nopasswd && "
        f'chmod 440 /etc/sudoers.d/99-quickhatch-nopasswd"'
    )
    rc, _, err = _ssh_exec(host, user, grant_cmd, key_path, timeout=30, port=port)
    if rc == 0:
        bridge.add_message(
            "agent",
            "✓ Granted NOPASSWD sudo before setup began, so package installs will not hang on a hidden password prompt.",
        )
        facts = dict(facts)
        facts["sudo_nopasswd"] = "yes"
        return facts
    bridge.add_message(
        "agent",
        "⚠️ Could not grant NOPASSWD sudo at startup. If the current account still requires a password, any plain `sudo` command may time out unless the model uses the known default password explicitly.",
    )
    if err.strip():
        bridge.add_message("agent", f"Startup sudo bootstrap stderr: {err.strip()[:240]}")
    return facts


def _split_command_segments_for_lint(cmd: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd) if part.strip()]


def _normalize_command_tool(token: str) -> str:
    """Collapse absolute paths like /usr/bin/yay down to the executable name."""
    return token.rsplit("/", 1)[-1] if "/" in token else token


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading env/sudo wrappers so command lint sees the real tool."""
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            idx += 1
            continue
        if tok == "env":
            idx += 1
            continue
        if tok == "sudo":
            idx += 1
            while idx < len(tokens):
                opt = tokens[idx]
                if opt in {"-u", "-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                    idx += 2
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        break
    return tokens[idx:]


def _extract_pacman_install_packages(tokens: list[str]) -> list[str]:
    """Extract package names from a pacman/yay/paru install command."""
    if not tokens or _normalize_command_tool(tokens[0]) not in {"pacman", "yay", "paru"}:
        return []
    args = tokens[1:]
    if not any(arg.startswith("-S") and arg not in {"-Ss", "-Si"} for arg in args):
        return []
    return [arg for arg in args if arg and not arg.startswith("-")]


def _uses_fake_plasma_session_wrapper(cmd: str) -> bool:
    """True when a command tries to apply live Plasma state inside dbus-run-session."""
    lower = (cmd or "").lower()
    return "dbus-run-session" in lower and (
        "plasma-apply-lookandfeel" in lower or "plasma-apply-wallpaperimage" in lower
    )


def _is_safe_diagnostic_setup_command(cmd: str) -> bool:
    """True when every command segment is a read-only diagnostic/probe.

    Used only while a sticky pre-reboot block is active: we still want the
    model to inspect `/mnt`, `/boot`, group membership, package metadata,
    and service state, but we do NOT want it to keep mutating unrelated
    parts of the system (desktop installs, app installs, theming, AUR work)
    before the boot blockers are fixed.
    """
    import shlex

    diagnostic_tools = {
        "[",
        "blkid",
        "bootctl",
        "cat",
        "command",
        "df",
        "du",
        "echo",
        "false",
        "find",
        "free",
        "getent",
        "grep",
        "head",
        "id",
        "journalctl",
        "ls",
        "lsblk",
        "lscpu",
        "lsinitrd",
        "lsmod",
        "lspci",
        "mount",
        "nvidia-smi",
        "pacman",
        "pwd",
        "readlink",
        "realpath",
        "stat",
        "systemctl",
        "tail",
        "test",
        "true",
        "uname",
        "whoami",
        "which",
    }
    for segment in _split_command_segments_for_lint(cmd):
        if any(op in segment for op in (">", ">>")):
            return False
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        tokens = _strip_command_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_command_tool(tokens[0])
        args = tokens[1:]
        if tool not in diagnostic_tools:
            return False
        if tool == "systemctl":
            allowed = {
                "cat",
                "is-active",
                "is-enabled",
                "list-unit-files",
                "show",
                "status",
            }
            if not args or args[0] not in allowed:
                return False
        elif tool == "mount":
            # `mount` without arguments is a read-only inventory command.
            # Mounting a device/path mutates the live/install environment.
            if args:
                return False
        elif tool == "pacman":
            allowed_flags = {"-Q", "-Qi", "-Qq", "-Qs", "-Sg", "-Si", "-Ss"}
            if not args or not any(arg in allowed_flags for arg in args):
                return False
        elif tool in {"[", "test"}:
            continue
    return True


_NETWORK_SECURITY_SERVICE_TOKENS = frozenset(
    {
        "mullvad-daemon",
        "mullvad-vpn",
        "openvpn",
        "wg-quick",
        "wg-quick@",
        "wireguard",
        "firewalld",
        "ufw",
        "nftables",
        "iptables",
    }
)


def _user_explicitly_requested_network_security_changes(*texts: str) -> bool:
    """Best-effort signal that the user explicitly asked for VPN/firewall activation."""
    positive = re.compile(
        r"\b(enable|start|activate|configure|set up|setup|turn on|bring up|use)\b",
        re.IGNORECASE,
    )
    sensitive = re.compile(
        r"\b(mullvad|vpn|wireguard|openvpn|firewall|firewalld|ufw|nftables|iptables)\b",
        re.IGNORECASE,
    )
    negative = re.compile(
        r"\b(don't|do not|dont|never|avoid|skip)\b.{0,40}\b"
        r"(mullvad|vpn|wireguard|openvpn|firewall|firewalld|ufw|nftables|iptables)\b",
        re.IGNORECASE | re.DOTALL,
    )
    for text in texts:
        if not text:
            continue
        if negative.search(text):
            continue
        if positive.search(text) and sensitive.search(text):
            return True
    return False


def _activates_network_security_services(cmd: str) -> bool:
    """True when the command enables/starts VPN or firewall services."""
    import shlex

    for segment in _split_command_segments_for_lint(cmd):
        lower_segment = segment.lower()
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = lower_segment.split()
        tokens = _strip_command_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_command_tool(tokens[0])
        args = [t.lower() for t in tokens[1:]]
        if tool == "systemctl" and args:
            verb = args[0]
            if verb in {"enable", "start", "restart", "try-restart"}:
                if any(
                    any(token.startswith(svc) for svc in _NETWORK_SECURITY_SERVICE_TOKENS)
                    for token in args[1:]
                ):
                    return True
        if tool == "ufw" and args and args[0] in {"enable", "reload"}:
            return True
        if tool in {"firewall-cmd", "nft", "iptables", "ip6tables"}:
            if any(word in lower_segment for word in ("enable", "reload", "default", "policy")):
                return True
    return False


def _preflight_setup_command(
    cmd: str,
    facts: dict[str, str],
    *,
    allow_network_security_changes: bool = False,
) -> str | None:
    """Reject obviously impossible package-manager actions before they hit the target."""
    import shlex

    package_manager = facts.get("package_manager", "unknown")
    if not allow_network_security_changes and _activates_network_security_services(cmd):
        return (
            "Do not enable/start VPN or firewall services automatically. "
            "Installing the package is fine, but activating Mullvad, WireGuard, OpenVPN, "
            "firewalld, ufw, nftables, or similar network-security services requires an "
            "explicit user request right now because it can change routing, DNS, firewall "
            "policy, or kill-switch behavior and cut off remote access."
        )
    if _uses_fake_plasma_session_wrapper(cmd):
        return (
            "Do not use `dbus-run-session` to apply KDE/Plasma theming. That creates a throwaway "
            "bus, so `plasma-apply-lookandfeel`/`plasma-apply-wallpaperimage` do not touch the "
            "real desktop the user sees. Use the logged-in user's actual session bus instead "
            "(`XDG_RUNTIME_DIR=/run/user/<uid>` and "
            "`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus`), or write KDE config files "
            "directly and tell the user a logout/login is required."
        )
    aur_helper = facts.get("aur_helper", "none")
    for segment in _split_command_segments_for_lint(cmd):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        tokens = _strip_command_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_command_tool(tokens[0])
        if package_manager == "pacman":
            if tool in {"apt", "apt-get", "dnf", "zypper"}:
                return (
                    f"This target is Arch-family (`pacman`), so `{tool}` is the wrong package manager. "
                    "Use `pacman` for repo packages, and use the AUR helper only for AUR packages."
                )
            if tool in {"yay", "paru"} and aur_helper == "none":
                return (
                    f"The command uses `{tool}` but no AUR helper is installed yet. "
                    "Install `yay`/`paru` first, build the package manually with `makepkg -s` + `pacman -U`, "
                    "or use `aur_search`/`web_search` first if you still need to resolve the exact package name."
                )
            if tool == "pacman":
                packages = _extract_pacman_install_packages(tokens)
                aur_only = [
                    pkg
                    for pkg in packages
                    if pkg in _ARCH_AUR_ONLY_PACKAGES or pkg.endswith(_AURISH_SUFFIXES)
                ]
                if aur_only:
                    helper = aur_helper if aur_helper != "none" else "an AUR helper"
                    joined = ", ".join(aur_only)
                    return (
                        "Do not pass AUR-only packages to `pacman` on Arch. "
                        f"`{joined}` should be installed with {helper} or via manual `makepkg`, "
                        "while repo packages stay on `pacman`. If you are unsure about the exact package name, "
                        "use `aur_search` before issuing another install command."
                    )
            if tool in {"yay", "paru"}:
                packages = _extract_pacman_install_packages(tokens)
                heavy_optional = [
                    pkg for pkg in packages if pkg in _HEAVY_OPTIONAL_DESKTOP_PACKAGES
                ]
                if heavy_optional and len(packages) > len(heavy_optional):
                    joined = ", ".join(heavy_optional)
                    return (
                        "Do not bundle heavyweight optional desktop wrappers into a mixed install batch. "
                        f"`{joined}` should be installed separately from core tools/themes so one slow or "
                        "failed package does not derail the whole transaction. Split this into smaller commands "
                        "and keep optional AI desktop clients independent."
                    )
        elif package_manager == "apt" and tool in {"pacman", "yay", "paru", "dnf", "zypper"}:
            return f"This target uses apt, so `{tool}` is the wrong package manager."
        elif package_manager == "dnf" and tool in {
            "pacman",
            "yay",
            "paru",
            "apt",
            "apt-get",
            "zypper",
        }:
            return f"This target uses dnf, so `{tool}` is the wrong package manager."
        elif package_manager == "zypper" and tool in {
            "pacman",
            "yay",
            "paru",
            "apt",
            "apt-get",
            "dnf",
        }:
            return f"This target uses zypper, so `{tool}` is the wrong package manager."
    return None


def _describe_setup_fact_changes(
    before: dict[str, str], after: dict[str, str], *, is_live: bool
) -> str | None:
    labels = {
        "package_manager": "package manager",
        "aur_helper": "AUR helper",
        "current_user": "current SSH user",
        "is_root": "root login",
        "sudo_nopasswd": "passwordless sudo",
    }
    changed = []
    for key, label in labels.items():
        old = before.get(key, "")
        new = after.get(key, "")
        if old != new:
            changed.append(f"- {label}: {old or '(unknown)'} -> {new or '(unknown)'}")
    if not changed:
        return None
    mode = "live installer" if is_live else "installed system"
    return "[quickhatch runtime facts updated]\n" + f"Mode: {mode}\n" + "\n".join(changed)


def _shell_single_quote(value: str) -> str:
    """Quote an arbitrary string for a POSIX shell single-quoted literal."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _truncate_setup_lookup_text(text: str, limit: int = 2200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = max(200, limit - head - 24)
    return text[:head] + "\n... (truncated) ...\n" + text[-tail:]


def _format_setup_web_lookup_result(kind: str, query: str, raw_json: str) -> tuple[bool, str]:
    header = f"[{kind} results for: {query}]"
    try:
        data = json.loads(raw_json)
    except Exception:
        return (
            False,
            f"{header}\nCould not parse search results.\n{_truncate_setup_lookup_text(raw_json)}",
        )

    error = (data.get("error") or "").strip()
    if error:
        return False, f"{header}\nSearch failed: {error}"

    results = data.get("results") or []
    if not isinstance(results, list) or not results:
        message = (data.get("message") or "No results found.").strip()
        return False, f"{header}\n{message}"

    lines = [header]
    for idx, item in enumerate(results[:5], start=1):
        title = (item.get("title") or "").strip() or "(untitled)"
        url = (item.get("url") or item.get("href") or "").strip()
        snippet = (item.get("snippet") or item.get("body") or "").strip()
        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   Snippet: {snippet[:280]}")
    return True, _truncate_setup_lookup_text("\n".join(lines))


def _build_setup_repo_search_command(package_manager: str, query: str) -> str | None:
    q = _shell_single_quote(" ".join((query or "").split()))
    commands = {
        "pacman": f"pacman -Ss {q} | sed -n '1,40p'",
        "apt": f"apt-cache search --names-only {q} | sed -n '1,40p'",
        "dnf": f"dnf search {q} | sed -n '1,60p'",
        "zypper": f"zypper search --details {q} | sed -n '1,60p'",
        "apk": f"apk search {q} | sed -n '1,40p'",
        "xbps": f"xbps-query -Rs {q} | sed -n '1,40p'",
        "emerge": f"emerge --search {q} | sed -n '1,60p'",
    }
    return commands.get(package_manager)


def _execute_setup_repo_search(
    host: str,
    user: str,
    key_path: str | None,
    facts: dict[str, str],
    query: str,
    *,
    port: int = 22,
) -> tuple[bool, str]:
    query = " ".join((query or "").split())
    if not query:
        return False, "[repo_search error]\nQuery was empty."

    package_manager = facts.get("package_manager", "unknown")
    cmd = _build_setup_repo_search_command(package_manager, query)
    if cmd is None:
        return (
            False,
            f"[repo_search error]\nNo repository search helper is defined for package manager '{package_manager}'. "
            "Use web_search instead.",
        )

    rc, out, err = _ssh_exec(host, user, cmd, key_path, timeout=60, port=port)
    header = f"[repo_search results for: {query}]"
    if out.strip():
        body = _truncate_setup_lookup_text(out)
        if rc == 0:
            return True, f"{header}\n{body}"
        return False, f"{header}\nSearch produced partial output (exit {rc}).\n{body}"
    if err.strip():
        return (
            False,
            f"{header}\nSearch failed (exit {rc}).\n{_truncate_setup_lookup_text(err, 1200)}",
        )
    return False, f"{header}\nNo repo matches found."


def _execute_setup_aur_search(
    host: str,
    user: str,
    key_path: str | None,
    facts: dict[str, str],
    query: str,
    *,
    port: int = 22,
) -> tuple[bool, str]:
    query = " ".join((query or "").split())
    if not query:
        return False, "[aur_search error]\nQuery was empty."
    if facts.get("package_manager") != "pacman":
        return (
            False,
            "[aur_search error]\nAUR lookups are only valid on Arch-family targets. "
            "Use repo_search or web_search instead.",
        )

    aur_helper = facts.get("aur_helper", "none")
    header = f"[aur_search results for: {query}]"
    if aur_helper != "none":
        cmd = f"{aur_helper} -Ss {_shell_single_quote(query)} | sed -n '1,40p'"
        rc, out, err = _ssh_exec(host, user, cmd, key_path, timeout=60, port=port)
        if out.strip():
            body = _truncate_setup_lookup_text(out)
            if rc == 0:
                return True, f"{header}\n{body}"
            return (
                False,
                f"{header}\nLocal {aur_helper} search produced partial output (exit {rc}).\n{body}",
            )
        if err.strip():
            local_note = (
                f"Local {aur_helper} search failed (exit {rc}). Falling back to a web search of aur.archlinux.org.\n"
                f"{_truncate_setup_lookup_text(err, 800)}\n\n"
            )
        else:
            local_note = f"No local {aur_helper} matches found. Falling back to a web search of aur.archlinux.org.\n\n"
    else:
        local_note = "No AUR helper is installed, so using a web search of aur.archlinux.org.\n\n"

    success, formatted = _format_setup_web_lookup_result(
        "aur_search web fallback",
        f"site:aur.archlinux.org {query}",
        _execute_web_search(f"site:aur.archlinux.org {query}", max_results=5),
    )
    return success, f"{header}\n{local_note}{formatted}"


def _execute_setup_web_lookup(query: str, max_results: int = 5) -> tuple[bool, str]:
    query = " ".join((query or "").split())
    if not query:
        return False, "[web_search error]\nQuery was empty."
    try:
        max_results = max(1, min(10, int(max_results)))
    except (TypeError, ValueError):
        max_results = 5
    return _format_setup_web_lookup_result(
        "web_search",
        query,
        _execute_web_search(query, max_results=max_results),
    )


def _needs_kde_final_verification(results_log: list[str], summary: str) -> bool:
    text = "\n".join([*results_log, summary]).lower()
    return any(
        marker in text
        for marker in (
            "plasma-apply-lookandfeel",
            "plasma-apply-wallpaperimage",
            "kwriteconfig6",
            "kde plasma",
            "candy-icons",
            "sweet",
        )
    )


def _verify_setup_completion(
    host: str,
    user: str,
    key_path: str | None,
    summary: str,
    results_log: list[str],
    *,
    port: int = 22,
    apps_to_install: list[str] | None = None,
    setup_facts: dict[str, str] | None = None,
) -> list[str]:
    """Return final-verification failures that should block a premature `done`.

    Checks, in order:

    1. **Apps** — every entry in ``apps_to_install`` resolves to an
       installed package on the target (queried via the detected
       package manager). A requested app that isn't installed is the
       single most common "agent said done, nothing actually landed"
       failure; this catches it deterministically.
    2. **System services** — display manager enabled + no failed
       systemd units. Without a display manager the user boots to a
       tty; "done" there is wrong.
    3. **KDE-specific theming** — unchanged from the original checks.
       Only runs when the results_log mentions KDE commands.
    """
    failures: list[str] = []
    command_log = "\n".join(results_log).lower()

    # --- App installation verification ------------------------------------
    # apps_to_install is the user's CURATED list from the wizard (not the
    # raw scan). Each should resolve to an installed package. Tolerant of
    # package-name drift between distros: we try a couple of common
    # normalisations before declaring a miss (e.g. "GitHub Desktop" →
    # "github-desktop-bin" on Arch).
    if apps_to_install and setup_facts:
        pm = (setup_facts.get("package_manager") or "").lower()
        query_cmds: dict[str, str] = {
            "pacman": "pacman -Qq",
            "apt": "dpkg-query -W -f='${Package}\\n'",
            "dnf": "rpm -qa --queryformat '%{NAME}\\n'",
            "zypper": "rpm -qa --queryformat '%{NAME}\\n'",
        }
        query_cmd = query_cmds.get(pm)
        if query_cmd:
            rc, out, _ = _ssh_exec(host, user, query_cmd, key_path, timeout=30, port=port)
            if rc == 0:
                installed = {
                    line.strip().lower() for line in (out or "").splitlines() if line.strip()
                }
                missing: list[str] = []
                for app in apps_to_install:
                    needle = (app or "").strip().lower()
                    if not needle:
                        continue
                    variants = {
                        needle,
                        needle.replace(" ", "-"),
                        needle.replace(" ", "-") + "-bin",
                        needle.replace(" ", "-") + "-git",
                        needle.replace(" ", ""),
                        needle.split()[0] if needle.split() else needle,
                    }
                    if not any(v in installed for v in variants if v):
                        missing.append(app)
                if missing:
                    failures.append(
                        "apps_to_install contained "
                        f"{len(missing)} package(s) that are NOT installed on the target: "
                        + ", ".join(repr(m) for m in missing[:10])
                        + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
                        + ". Install them (use repo_search / aur_search first if names "
                        "are uncertain) before emitting `done`."
                    )

    # --- System-service verification --------------------------------------
    # systemctl --failed returns non-zero units. If any core service is
    # failing (NetworkManager, sddm, bluetooth, dbus, etc.), the desktop
    # won't boot correctly. Also check that a display manager is enabled
    # — otherwise the user gets a tty prompt at boot.
    svc_cmd = (
        "systemctl --failed --no-legend 2>/dev/null | awk '{print $1}' | head -20; "
        "echo '---DM---'; "
        "for dm in sddm gdm lightdm greetd tuigreet ly; do "
        '  if systemctl is-enabled $dm.service >/dev/null 2>&1; then echo "DM_ENABLED=$dm"; break; fi; '
        "done; "
        "echo '---USER---'; "
        "id -u 2>/dev/null; "
        "getent group wheel sudo 2>/dev/null | head -5"
    )
    rc, svc_out, _ = _ssh_exec(host, user, svc_cmd, key_path, timeout=20, port=port)
    if rc == 0 and svc_out:
        lines = svc_out.splitlines()
        try:
            dm_idx = lines.index("---DM---")
            user_idx = lines.index("---USER---")
        except ValueError:
            dm_idx = user_idx = -1
        if dm_idx >= 0:
            failed_units = [line.strip() for line in lines[:dm_idx] if line.strip()]
            # Whitelist non-critical "failures" that we don't want to hard-block on.
            _ignore_failed = {"systemd-modules-load.service", "systemd-resolved.service"}
            critical_failed = [u for u in failed_units if u not in _ignore_failed]
            if critical_failed:
                failures.append(
                    "systemctl reports failed units: "
                    + ", ".join(critical_failed[:10])
                    + ". Investigate with `systemctl status <unit>` and `journalctl -u <unit> -b` "
                    "before declaring done."
                )
            dm_lines = lines[dm_idx + 1 : user_idx] if user_idx > dm_idx else []
            dm_enabled = any(line.startswith("DM_ENABLED=") for line in dm_lines)
            if not dm_enabled:
                failures.append(
                    "no display manager is enabled (looked for sddm/gdm/lightdm/greetd/tuigreet/ly). "
                    "The user will boot to a tty instead of a graphical login. "
                    "Enable one with `systemctl enable <dm>` before done."
                )

    # --- KDE-specific theming (unchanged) ---------------------------------
    if not _needs_kde_final_verification(results_log, summary):
        return failures

    require_wallpaper = (
        "plasma-apply-wallpaperimage" in command_log or "masterpiece." in command_log
    )
    verify_cmd = r"""
uid=$(id -u 2>/dev/null || echo "")
echo "QH_UID=$uid"
awk '/^\[General\]/{flag=1;next}/^\[/{flag=0}flag && /^ColorScheme=/{print "QH_COLOR=" substr($0,13)}' ~/.config/kdeglobals 2>/dev/null | tail -n1
awk '/^\[Icons\]/{flag=1;next}/^\[/{flag=0}flag && /^Theme=/{print "QH_ICON=" substr($0,7)}' ~/.config/kdeglobals 2>/dev/null | tail -n1
awk '/^\[Plugins\]/{flag=1;next}/^\[/{flag=0}flag && /^wobblywindowsEnabled=/{print "QH_WOBBLY=" substr($0,22)}' ~/.config/kwinrc 2>/dev/null | tail -n1
awk '/^\[Plugins\]/{flag=1;next}/^\[/{flag=0}flag && /^magiclampEnabled=/{print "QH_MAGIC=" substr($0,19)}' ~/.config/kwinrc 2>/dev/null | tail -n1
if [ -n "$uid" ] && [ -S "/run/user/$uid/bus" ]; then echo "QH_BUS=yes"; else echo "QH_BUS=no"; fi
wall_ok=no
for f in "$HOME"/masterpiece.*; do
  if [ -s "$f" ]; then wall_ok=yes; break; fi
done
echo "QH_WALL=$wall_ok"
"""
    rc, out, err = _ssh_exec(host, user, verify_cmd, key_path, timeout=30, port=port)
    if rc != 0:
        failures.append(
            f"final verification probe failed: {(err or out or '').strip()[:200] or 'unknown error'}"
        )
        return failures

    facts: dict[str, str] = {}
    for line in (out or "").splitlines():
        if line.startswith("QH_") and "=" in line:
            key, value = line.split("=", 1)
            facts[key] = value.strip()

    if not facts.get("QH_COLOR"):
        failures.append("KDE color scheme is still unset in ~/.config/kdeglobals")
    if not facts.get("QH_ICON"):
        failures.append("KDE icon theme is still unset in ~/.config/kdeglobals")
    if "wobblywindowsenabled" in command_log and facts.get("QH_WOBBLY", "").lower() != "true":
        failures.append(
            "KWin wobbly windows effect was requested but is not enabled in ~/.config/kwinrc"
        )
    if "magiclampenabled" in command_log and facts.get("QH_MAGIC", "").lower() != "true":
        failures.append(
            "KWin magic lamp effect was requested but is not enabled in ~/.config/kwinrc"
        )
    if require_wallpaper and facts.get("QH_WALL") != "yes":
        failures.append(
            "wallpaper apply was attempted, but no non-empty wallpaper asset was downloaded"
        )
    if facts.get("QH_BUS") != "yes":
        failures.append(
            "no live user session bus was detected under /run/user/<uid>/bus for Plasma apply verification"
        )
    return failures


def _lookup_hint_for_failed_command(cmd: str, output: str, facts: dict[str, str]) -> str | None:
    """Suggest a lookup action when a package install failed due to naming/resolution."""
    text = (output or "").lower()
    package_manager = facts.get("package_manager", "unknown")
    if package_manager == "pacman" and any(
        marker in text
        for marker in (
            "target not found",
            "could not find all required packages",
            "no package matched",
        )
    ):
        return (
            "This looks like a package-resolution failure. Do not guess another install command yet. "
            "Your next action should be `repo_search` for official packages, `aur_search` for AUR packages, "
            "or `web_search` if the result is still ambiguous."
        )
    if package_manager == "apt" and "unable to locate package" in text:
        return (
            "Apt could not resolve that package name. Use `repo_search` or `web_search` next, then install the exact "
            "resolved package instead of guessing alternative names."
        )
    if package_manager == "dnf" and any(
        marker in text for marker in ("no match for argument", "unable to find a match")
    ):
        return (
            "DNF could not resolve that package name. Use `repo_search` or `web_search` next, then retry with the "
            "exact resolved package name."
        )
    if package_manager == "zypper" and any(
        marker in text for marker in ("no provider of", "no matching items found")
    ):
        return (
            "Zypper could not resolve that package name. Use `repo_search` or `web_search` next, then retry with the "
            "exact resolved package name."
        )
    return None


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
    timeout: int | None = None,
    interval: int = 5,
    stop_flag_getter=None,
) -> bool:
    """Poll SSH until the target accepts a connection again.

    By default (``timeout=None``) this polls FOREVER and only abandons
    on Stop. That's intentional: the user may need to walk to the
    target and type a LUKS passphrase — any timeout short enough to
    catch genuine boot failures will also kick users who stepped away
    for coffee. The Stop button is the correct escape hatch.

    When ``timeout`` is an int, retain the old deadline behavior — kept
    for tests that want a bounded wait.

    ``stop_flag_getter`` is a zero-arg callable returning True when the
    user has pressed Stop; polled every ``interval`` seconds.
    """
    import time as _time

    deadline = None if timeout is None else (_time.time() + timeout)
    while True:
        if stop_flag_getter is not None and stop_flag_getter():
            return False
        if deadline is not None and _time.time() >= deadline:
            return False
        rc, out, _ = _ssh_exec(host, user, "echo __reconnected__", key_path, timeout=10, port=port)
        if rc == 0 and "__reconnected__" in out:
            return True
        _time.sleep(interval)


# Commands that trigger the reconnect-after-reboot dance. Matched
# conservatively — the agent's "reboot" is distinct from other uses of
# the word (e.g. config file contents mentioning reboot).
_REBOOT_COMMAND_RE = re.compile(
    r"(^|;|&&|\|\||\s|\bsudo\s+)"
    r"(reboot|shutdown\s+-r|systemctl\s+reboot|init\s+6|telinit\s+6|"
    r"halt\s+--reboot|kexec(\s+-e|\s+--exec)|"
    # writing 'b' (or 's'/'u') to sysrq-trigger forces a reboot
    r"echo\s+['\"]?b['\"]?\s*>\s*/proc/sysrq-trigger)"
    r"(\s|;|&&|$)"
)


# Match `useradd -m NAME` / `useradd ... NAME` to catch the user being
# created inside chroot. We grab the last non-flag, non-option token on
# the line — useradd's final positional arg is always the login name.
# Works on both `useradd quickhatch` and `useradd -m -G wheel ... jan`.
_USERADD_RE = re.compile(
    # User-creation command must be at a genuine command position:
    #   - start of string / line / after a command separator (; && || |)
    #   - after a shell-wrapper quote that opens a command (sh -c "useradd...)
    # An optional `sudo ` / `eval ` / `env VAR=VAL ` prefix is tolerated.
    # NOT matched: `echo useradd bob`, `echo adduser bob`, `cat /etc/useradd.conf`
    # — those have a bare identifier (e.g. `echo`) right before the create-user
    # command, which isn't a command-boundary.
    r"""(?:^|[;&|]\s*|&&\s*|\|\|\s*|["'`])"""
    # optional command prefixes that wrap a real command
    r"""(?:sudo\s+|eval\s+|env\s+(?:\w+=\S+\s+)*)?"""
    r"""(useradd|adduser)\s+([^;&|\n\r]+)""",
    re.IGNORECASE,
)


def _extract_created_user(cmd: str) -> str | None:
    """Return the username created by a create-user command, or None.

    The agent creates the post-install user inside arch-chroot. Its
    login name typically ends up as the last positional arg of
    `useradd`/`adduser`. We
    parse it so `profile.linux_user` can be switched ahead of the
    post-reboot reconnect — otherwise SSH keeps trying to log in as
    the live-ISO's root, which doesn't exist on the installed system.
    """
    # Reject comment lines outright — a `# useradd ...` trail in a
    # shell comment must never flip the real profile username.
    # Heredoc handling is context-dependent:
    #   `cat <<EOF ... useradd bob ... EOF` → heredoc is DATA piped
    #     to cat's stdin; useradd inside is string content, not an
    #     executed command. Skip.
    #   `bash <<EOF ... useradd bob ... EOF` → heredoc is a SHELL
    #     SCRIPT executed by bash. useradd inside IS executed and
    #     creates a real user. Don't skip.
    #   `arch-chroot /mnt <<'EOF' ... useradd bob ... EOF` → same:
    #     commands inside the chroot.
    heredoc_delim: str | None = None
    heredoc_executes = False  # whether this heredoc's body is run as shell
    _heredoc_re = re.compile(
        r"<<-?\s*([\"'])?([A-Za-z_][A-Za-z0-9_]*)\1?",
    )
    # Shell-interpreter commands: when THESE precede `<<EOF`, the
    # heredoc body is executed. Extend if we add more exotic wrappers.
    _shell_exec_prefix_re = re.compile(
        r"(?:^|[;&|]\s*|&&\s*|\|\|\s*)"
        r"(?:sudo\s+|env\s+(?:\w+=\S+\s+)*)?"
        r"(?:bash|sh|zsh|dash|ash|fish|busybox\s+sh|"
        r"arch-chroot(?:\s+\S+){1,3}|chroot(?:\s+\S+){1,4}|"
        r"systemd-nspawn(?:\s+-D\s+\S+)?|unshare(?:\s+-\S+)*|"
        r"ssh\s+\S+|"  # ssh host <<EOF runs remote shell
        r"sudo\s+-[iu]\b|su\s+(?:-\s+)?\S+)"
        r"\s*(?:<<|$)",
        re.IGNORECASE,
    )
    m = None
    for line in cmd.splitlines():
        if heredoc_delim is not None:
            # Inside a heredoc body — closing delimiter is the line
            # alone (or with leading tabs when `<<-` was used).
            if line.strip() == heredoc_delim:
                heredoc_delim = None
                heredoc_executes = False
                continue
            if not heredoc_executes:
                continue  # data-heredoc body → skip entirely
            # Executing heredoc: fall through to the regex match below
            # so useradd inside `bash <<EOF` is still detected.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # this line is a comment, skip it
        # Detect start of a heredoc on THIS line, and decide whether
        # its body is executed or consumed as data.
        hd = _heredoc_re.search(line)
        if hd:
            heredoc_delim = hd.group(2)
            # Slice from line start up to the `<<` to see what command
            # is doing the redirect. Multi-heredoc in one line (rare)
            # just picks the first match, which is good enough.
            before_heredoc = line[: hd.start()]
            heredoc_executes = bool(_shell_exec_prefix_re.search(before_heredoc))
        m = _USERADD_RE.search(line)
        if m:
            break
    if m is None:
        return None
    # Reject `useradd -D` / `--defaults` — that flag SHOWS defaults, it
    # doesn't create anyone. Parsing the positional arg would mis-
    # identify a value like the group name as the "created user".
    command_name = (m.group(1) or "").lower()
    if command_name == "useradd" and re.search(r"(?:^|\s)(-D|--defaults)(?:\s|$)", m.group(0)):
        return None
    tail = m.group(2)
    # Strip trailing closing quotes / backticks that belong to the outer
    # shell wrapper (e.g. `sh -c "useradd -m foo"` → tail is `-m foo"`).
    tail = tail.rstrip("\"'` \t")
    tokens = tail.split()
    flags_with_values = {
        "-c",
        "--comment",
        "-d",
        "--home-dir",
        "-e",
        "--expiredate",
        "-f",
        "--inactive",
        "-g",
        "--gid",
        "--ingroup",
        "-G",
        "--groups",
        "-k",
        "--skel",
        "-p",
        "--password",
        "-s",
        "--shell",
        "-u",
        "--uid",
        "-Z",
        "--selinux-user",
        "-K",
        "--key",
    }
    # Walk right-to-left with real indices (not tokens.index(tok), which
    # returns the FIRST match — if a username happens to equal an
    # earlier flag value, the boundary check misfires).
    for idx in range(len(tokens) - 1, -1, -1):
        tok = tokens[idx]
        if tok.startswith("-"):
            continue
        if idx > 0 and tokens[idx - 1] in flags_with_values:
            continue
        # Strip surrounding single/double quotes/backticks if the arg
        # was quoted in the shell wrapper.
        return tok.strip("\"'`")
    return None


# Hints surfaced to a stuck agent. Ordered: more-specific patterns first.
# Each hint has a "weak" flag: weak hints only fire when the same command
# has repeatedly failed — otherwise generic patterns like "No such file"
# or "Permission denied" trigger on every `ls /does-not-exist` probe the
# agent runs for reconnaissance, drowning real signal in noise.
_FAILURE_HINTS: list[tuple[str, str, bool]] = [
    (
        r"already running|acquired by another|locked by another process",
        "This looks like a mutex/lock failure. The process reports it's "
        "already running. Common fix: find and remove the stale lockfile "
        "(e.g. `sudo find /tmp /var/lock /var/run -maxdepth 3 -name '*.lock' "
        "-o -name '*.lock*' 2>/dev/null`), then retry. For AUR makepkg "
        "failures, the culprit is often a /tmp/*.lock directory left over "
        "from a prior killed run.",
        False,
    ),
    (
        # "is in use" alone over-matches ("device is in use", "this feature
        # is in use"), so keep it near resource/lock/mutex wording.
        r"resource .*(in use|busy)|is in use by another",
        "This looks like a mutex/lock failure. The process reports the "
        "resource is held by another process. Find and remove the stale "
        "lockfile (check /tmp, /var/lock, /var/run), then retry.",
        False,
    ),
    (
        r"database lock|database is locked|could not lock database|"
        r"cannot obtain lock",
        "pacman-style database lock. Run `sudo rm -f /var/lib/pacman/db.lck` "
        "if no other pacman/yay is currently running, then retry.",
        False,
    ),
    (
        r"No space left on device|disk full|out of space",
        "Target disk is full. Check `df -h` and free space before retrying. "
        "For /var/cache/pacman, `sudo pacman -Sc --noconfirm` reclaims "
        "older package cache.",
        False,
    ),
    (
        r"Permission denied|permission denied|not permitted|"
        r"Operation not permitted",
        "Permission issue. You may need `sudo` (check `whoami` and group "
        "membership) or the target file may have read-only perms. For "
        "AUR builds inside fakeroot, sudo inside the script won't work — "
        "check the PKGBUILD for steps that need root outside fakeroot.",
        True,  # over-matches on probes; wait for repetition
    ),
    (
        r"Failed to retrieve|Could not resolve|Connection timed out|"
        r"Network is unreachable|host lookup failed",
        "Network failure. Run `ping -c 1 archlinux.org` (or similar) to "
        "check connectivity. If in chroot, remember to mount /etc/resolv.conf "
        "or use `genfstab -U /mnt` before `arch-chroot`.",
        False,
    ),
    (
        r"No such file or directory",
        "A referenced file doesn't exist. Run `ls` on the expected path to "
        "confirm, or the script may be assuming a dependency/binary is "
        "installed. Verify with `command -v <tool>`.",
        True,  # hugely over-matches reconnaissance probes
    ),
]


def _advise_on_repeated_failure(
    cmd: str,
    combined_output: str,
    recent: list[tuple[str, int]],
) -> str | None:
    """Return an advisory message if the agent seems stuck, else None.

    Triggered when either:
    - The same command has failed back-to-back (2+ times)
    - Combined stdout+stderr matches a known failure pattern
    """
    import re as _re

    # `recent` INCLUDES the just-appended current event, so the count is
    # already accurate — don't add 1.
    same_cmd_fails = sum(1 for c, rc in recent[-3:] if c == cmd and rc != 0)

    # Pattern-match: surface a specific hint as soon as the output shape is
    # recognisable (even on the very first fail). Keeps us from waiting
    # for repetition on known-shape errors like lockfiles / disk full.
    lower = (combined_output or "").lower()
    for pattern, hint, weak in _FAILURE_HINTS:
        if _re.search(pattern, lower, _re.IGNORECASE):
            # Weak patterns over-match on recon probes (`ls /nope`,
            # `test -e /path`). Require at least one prior same-cmd
            # failure before firing these.
            if weak and same_cmd_fails < 2:
                continue
            return hint

    # No pattern match → only surface the generic "you're looping" hint
    # when the same command has failed at least TWICE. Avoids spamming
    # the chat on one-off probe failures the agent already expects
    # (e.g. `which yay` returning rc=1 when yay isn't installed yet).
    if same_cmd_fails >= 2:
        return (
            f"This exact command has failed {same_cmd_fails} times in a "
            f"row with no change. Consider: (a) reading the FULL stderr "
            f"(some tools redirect it to a log file — check /tmp, /var/log, "
            f"or the program's own lock/cache dir), (b) cleaning up stale "
            f"state (lockfiles, partial downloads, half-built packages), "
            f"(c) trying a different approach entirely if the error isn't "
            f"actionable."
        )
    return None


def _command_intent_signatures(cmd: str) -> list[str]:
    """Normalize all install/remove command segments to stable intent keys."""
    import re as _re

    if not cmd:
        return []
    # Strip leading `echo ... |` / `yes |` / `printf ... |` — these are
    # pseudo-confirmation tricks, not the real intent.
    stripped = _re.sub(
        r"^\s*(?:echo\s+['\"]?y['\"]?|yes|printf\s+['\"][^'\"]*['\"])\s*\|\s*",
        "",
        cmd,
    )
    segments = _re.split(r"\s*(?:&&|\|\||;)\s*", stripped)
    segments = [s.strip() for s in segments if s.strip()]
    if not segments:
        return []

    def _sig_for_segment(seg: str) -> str | None:
        # Drop leading env vars + sudo (in any order / interleaving).
        # Env vars look like UPPER_CASE=value; they can appear before
        # or after `sudo`, and more than once.
        env_or_sudo = r"(?:sudo\s+|[A-Z_][A-Z0-9_]*=\S+\s+)+"
        seg = _re.sub(r"^\s*" + env_or_sudo, "", seg)
        tokens = seg.split()
        if not tokens:
            return None
        tool = tokens[0]
        if tool not in {"pacman", "yay", "paru", "apt", "apt-get", "dnf", "zypper"}:
            return None
        # Collapse short + long operation flags to a canonical verb.
        op: str | None = None
        targets: list[str] = []
        install_ops = {
            "-S",
            "-Sy",
            "-Syu",
            "-Syyu",
            "install",
            "reinstall",
            "localinstall",
            "in",
        }
        remove_ops = {
            "-R",
            "-Rs",
            "-Rn",
            "-Rns",
            "-Rnsu",
            "-Rdd",
            "-U",
            "remove",
            "purge",
            "autoremove",
        }
        # All flags we explicitly strip — escalation flags, noninteractive
        # flags, confirmation-override flags. If any of these drift between
        # turns the intent is still identical.
        DROP_FLAGS = {
            "--noconfirm",
            "--needed",
            "--nodeps",
            "--force",
            "-f",
            "-y",
            "--yes",
            "-q",
            "--quiet",
            "--overwrite",
            "--assume-yes",
            "--allow-downgrades",
            "--allow-unauthenticated",
            "-Dd",
            "-dd",
        }
        for tok in tokens[1:]:
            if tok in install_ops:
                op = op or "install"
                continue
            if tok in remove_ops:
                op = op or "remove"
                continue
            if tok.startswith("-") and tok in DROP_FLAGS:
                continue
            if tok.startswith("--") and tok in DROP_FLAGS:
                continue
            # Unrecognised short flag stack (e.g. -Syyu) — if it's all
            # letters, treat it as operation modifier and drop.
            if _re.fullmatch(r"-[A-Za-z]{1,6}", tok):
                # Heuristic: if it contains S/R/U, map to install/remove/upgrade.
                if op is None:
                    if "S" in tok:
                        op = "install"
                    elif "R" in tok:
                        op = "remove"
                    elif "U" in tok:
                        op = "install"
                continue
            # Treat remaining tokens as package names (deduped, sorted).
            targets.append(tok)
        if op is None or not targets:
            return None
        return f"{tool}:{op}:" + " ".join(sorted(set(targets)))

    sigs: list[str] = []
    for seg in segments:
        sig = _sig_for_segment(seg)
        if sig:
            sigs.append(sig)
    return sigs


def _command_intent_signature(cmd: str) -> str | None:
    """Normalize an install/remove command to its INTENT so sibling variants
    collapse to the same key.

    Motivated by the live run where the agent spent 8 turns in a pacman
    conflict loop, each command a different flag variation of the same
    intent:

        pacman -Rns nodejs && pacman -S nodejs-lts-iron openai-codex
        pacman -S nodejs-lts-iron openai-codex               # tried plain
        echo 'y' | pacman -S nodejs-lts-iron openai-codex    # piped y
        pacman -S --nodeps nodejs-lts-iron openai-codex      # added --nodeps
        pacman -Rdd nodejs && pacman -S nodejs-lts-iron ...  # force-remove

    The exact-command detector didn't fire (each literal string differs).
    Stripping flags, env prefixes, pipes, and `sudo` reveals a single
    intent ("install nodejs-lts-iron openai-codex") repeated five times.

    Returns a stable signature like ``pacman:install:nodejs-lts-iron openai-codex``
    for install/remove-shaped commands, or None for commands we don't
    try to normalize (the same-exact-command detector still covers those).
    """
    # Split on `&&` / `;` / `||` and pick the LAST install-shaped segment.
    # Conflict-escalation loops typically bury the real install at the end
    # behind a preparatory remove (`-Rdd nodejs && -S nodejs-lts-iron`).
    sigs = _command_intent_signatures(cmd)
    return sigs[-1] if sigs else None


def _detect_flag_escalation(recent_commands: list[str]) -> str | None:
    """If the agent has issued 3+ variants of the same intent with
    PROGRESSIVELY MORE DESTRUCTIVE flags, return a warning string.

    Catches the escalation pattern where each attempt differs from the
    last only by adding a more forceful flag (``--noconfirm`` →
    ``--nodeps`` → ``-Rdd`` → ``echo y |``) to work around the same
    underlying problem. The intent signature normalises all those
    variants to the same key — we just need N repeats of the key.
    """
    if len(recent_commands) < 3:
        return None
    sigs: list[str] = []
    for command in recent_commands[-5:]:
        sigs.extend(_command_intent_signatures(command))
    if len(sigs) < 3:
        return None
    from collections import Counter

    counts = Counter(sigs)
    candidates = [(sig, n) for sig, n in counts.items() if n >= 3]
    if not candidates:
        return None

    def _target_count(sig: str) -> int:
        return len(sig.rsplit(":", 1)[-1].split())

    # In chained commands, an unrelated helper install can repeat more often
    # than the actual conflict. Prefer the richer repeated package intent.
    top_sig, top_n = max(candidates, key=lambda item: (_target_count(item[0]), item[1]))
    if top_n < 3:
        return None
    return top_sig


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


_STALL_TOPIC_WORDS = re.compile(
    # Nouns + tools that typically headline a topic. Pulled from the
    # failure modes observed in live runs — extend as we see more.
    r"\b("
    r"pacman\.conf|pacman|mirrorlist|sig(?:level|nature)|keyring|archlinux-keyring|"
    r"multilib|reflector|architecture|gpg|clock|ntp|timedatectl|date|"
    r"xorg|x11|wayland|sddm|kwin|modeset|drm|vboxvideo|"
    r"useradd|chpasswd|wheel|sudoers|"
    r"brave|steam|github-desktop|yay|aur|makepkg|base-devel|"
    r"disk|partition|mount|fstab|grub|systemd-boot|efi|luks|"
    r"nvidia|mesa|driver|kernel-fsync|"
    r"network|resolv|dhcp|nmcli|systemd-networkd|"
    # Generic package-manager concepts — these caught the nodejs /
    # openai-codex conflict loop after the fact. If the agent mentions
    # "conflict" or "dependency" for 4+ turns it's almost always stuck
    # on a dep-resolution rabbit hole.
    r"conflict|dependency|deps|dependencies|unresolvable|"
    r"nodejs|node-lts|npm|"
    # Build-from-source common grief spots.
    r"cmake|meson|ninja|configure|autoreconf|linker|ld\.so|pkgconfig"
    r")\b",
    re.IGNORECASE,
)


def _detect_stall_topic(recent_reasons: list[str]) -> str | None:
    """If the agent's recent reasons repeatedly mention the same topic
    keyword, return that keyword. Otherwise None.

    A stall is defined as: 4+ of the last 5 reasons referencing the same
    topic word (case-insensitive). The topic word is defined narrowly in
    _STALL_TOPIC_WORDS so unrelated filler words don't trigger false
    positives. Example: 10 turns about 'pacman.conf' / 'mirrorlist' /
    'SigLevel' should fire once those words accumulate.

    We check the RAW keyword rather than a topic-cluster so the nudge
    message can cite the specific thing the model keeps circling.
    """
    if len(recent_reasons) < 4:
        return None
    from collections import Counter

    counts: Counter[str] = Counter()
    for reason in recent_reasons:
        found = {m.group(1).lower() for m in _STALL_TOPIC_WORDS.finditer(reason or "")}
        for topic in found:
            counts[topic] += 1
    # At least 4 occurrences of the same topic in 5 reasons.
    for topic, n in counts.most_common():
        if n >= 4:
            return topic
    return None


# Bash that runs an exhaustive "is this freshly-installed system
# actually bootable?" check inside /mnt before the agent reboots.
# Cross-distro: detects mkinitcpio (Arch family), initramfs-tools
# (Debian/Ubuntu family), or dracut (Fedora/SUSE/Void) and adapts.
# Output: `STATUS|<level>|<key>|<message>` lines. Levels: OK / WARN /
# FAIL / SKIP. Harness blocks reboot on any FAIL.
_PRE_REBOOT_CHECK_SCRIPT = r"""set +e
_root=/mnt
[ -d "$_root/etc" ] || _root=
if [ -z "$_root" ]; then
  echo "STATUS|SKIP|chroot|/mnt has no /etc — likely already on the installed system; skipping checks"
  exit 0
fi

emit(){ echo "STATUS|$1|$2|$3"; }

# Detect distro family from /etc/os-release on the target.
DFAMILY=unknown
if [ -f "$_root/etc/os-release" ]; then
  . "$_root/etc/os-release" 2>/dev/null
  case "$ID $ID_LIKE" in
    *arch*|*manjaro*|*endeavouros*|*cachyos*|*garuda*|*artix*) DFAMILY=arch ;;
    *debian*|*ubuntu*|*pop*|*mint*|*kali*|*parrot*|*zorin*|*elementary*) DFAMILY=debian ;;
    *fedora*|*rhel*|*centos*|*rocky*|*alma*|*nobara*|*bazzite*) DFAMILY=fedora ;;
    *suse*|*opensuse*) DFAMILY=suse ;;
    *void*) DFAMILY=void ;;
    *alpine*) DFAMILY=alpine ;;
    *nixos*) DFAMILY=nixos ;;
  esac
  emit OK distro "Detected distro family: $DFAMILY ($PRETTY_NAME)"
fi

# 1. fstab present + non-empty
if [ -s "$_root/etc/fstab" ]; then
  emit OK fstab "/etc/fstab present ($(wc -l < "$_root/etc/fstab") lines)"
else
  emit FAIL fstab "/etc/fstab missing or empty — initramfs will fail to mount root"
fi

# 2. Kernel + initramfs in /boot. Naming differs per family:
#    Arch/Fedora/SUSE/Void: initramfs-*.img
#    Debian/Ubuntu:         initrd.img-*
KERN_CT=$(ls "$_root"/boot/vmlinuz-* 2>/dev/null | wc -l)
INIT_CT=$(ls "$_root"/boot/initramfs-*.img "$_root"/boot/initrd.img-* 2>/dev/null | wc -l)
if [ "$KERN_CT" -gt 0 ] && [ "$INIT_CT" -gt 0 ]; then
  emit OK kernel "/boot has $KERN_CT kernel(s) and $INIT_CT initramfs image(s)"
else
  emit FAIL kernel "/boot missing kernel or initramfs (vmlinuz / initramfs / initrd count: $KERN_CT / $INIT_CT). Run the distro's initramfs generator inside chroot — Arch: \`mkinitcpio -P\`. Debian/Ubuntu: \`update-initramfs -u -k all\`. Fedora/SUSE/Void: \`dracut --regenerate-all -f\`."
fi

# 3. Initramfs config-vs-image mtime: regen must have run AFTER last
#    config edit. Detect which generator's config is in use.
INIT_NEWEST=$(ls -t "$_root"/boot/initramfs-*.img "$_root"/boot/initrd.img-* 2>/dev/null | head -n1)
INIT_T=""
[ -n "$INIT_NEWEST" ] && INIT_T=$(stat -c %Y "$INIT_NEWEST" 2>/dev/null)
CONF_PATH=""
CONF_GEN=""
if [ -f "$_root/etc/mkinitcpio.conf" ]; then
  CONF_PATH="$_root/etc/mkinitcpio.conf"; CONF_GEN="mkinitcpio -P"
elif [ -d "$_root/etc/initramfs-tools" ]; then
  CONF_PATH=$(find "$_root/etc/initramfs-tools" -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | awk '{print $2}')
  CONF_GEN="update-initramfs -u -k all"
elif [ -f "$_root/etc/dracut.conf" ] || [ -d "$_root/etc/dracut.conf.d" ]; then
  CONF_PATH=$(find "$_root/etc/dracut.conf" "$_root/etc/dracut.conf.d" -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | awk '{print $2}')
  CONF_GEN="dracut --regenerate-all -f"
fi
if [ -n "$CONF_PATH" ] && [ -n "$INIT_T" ]; then
  CONF_T=$(stat -c %Y "$CONF_PATH" 2>/dev/null)
  if [ -n "$CONF_T" ]; then
    if [ "$INIT_T" -ge "$CONF_T" ]; then
      emit OK initramfs "initramfs newer than $(basename "$CONF_PATH") — regen ran after last edit"
    else
      emit FAIL initramfs "initramfs config ($(basename "$CONF_PATH")) was edited AFTER initramfs was built — run \`$CONF_GEN\` inside chroot"
    fi
  fi
fi

# 4. LUKS plumbing. If any block device is LUKS, the initramfs must
#    have the unlock module AND the kernel cmdline must reference it.
LUKS_DEVS=$(lsblk -o NAME,FSTYPE -nr 2>/dev/null | awk '$2=="crypto_LUKS"{print $1}')
if [ -n "$LUKS_DEVS" ]; then
  emit OK luks-present "LUKS containers detected: $(echo $LUKS_DEVS | tr '\n' ' ')"
  # 4a. Initramfs must include LUKS unlock plumbing.
  case "$DFAMILY" in
    arch|alpine)
      HOOKS=$(grep -E '^HOOKS=' "$_root/etc/mkinitcpio.conf" 2>/dev/null | head -n1)
      if echo "$HOOKS" | grep -qE '\b(encrypt|sd-encrypt)\b'; then
        emit OK luks-init "HOOKS includes encrypt: $HOOKS"
      else
        emit FAIL luks-init "LUKS in use but mkinitcpio HOOKS lacks 'encrypt' — initramfs cannot unlock root. Add 'encrypt' between 'block' and 'filesystems' in /etc/mkinitcpio.conf, then re-run mkinitcpio -P. Current: $HOOKS"
      fi
      ;;
    debian)
      if chroot "$_root" dpkg -s cryptsetup-initramfs >/dev/null 2>&1; then
        emit OK luks-init "cryptsetup-initramfs installed"
      else
        emit FAIL luks-init "LUKS in use but 'cryptsetup-initramfs' package is not installed — initramfs cannot unlock root. Run \`apt install -y cryptsetup-initramfs\` then \`update-initramfs -u -k all\` inside chroot."
      fi
      if [ -s "$_root/etc/crypttab" ]; then
        emit OK luks-crypttab "/etc/crypttab populated ($(wc -l < "$_root/etc/crypttab") lines)"
      else
        emit FAIL luks-crypttab "LUKS in use but /etc/crypttab is empty — Debian's initramfs needs a crypttab entry like 'cryptroot UUID=<luks-uuid> none luks'"
      fi
      ;;
    fedora|suse|void)
      if [ -n "$INIT_NEWEST" ] && chroot "$_root" lsinitrd "$INIT_NEWEST" 2>/dev/null | grep -qi crypt; then
        emit OK luks-init "dracut initramfs includes crypt module"
      elif [ -s "$_root/etc/crypttab" ]; then
        emit OK luks-init "/etc/crypttab populated (dracut should pick up crypt module automatically on regen)"
      else
        emit FAIL luks-init "LUKS in use but no crypt module evidence in initramfs and /etc/crypttab is empty. Add a crypttab entry then run \`dracut --regenerate-all -f\`."
      fi
      ;;
    *)
      emit WARN luks-init "LUKS in use but distro family '$DFAMILY' unknown — cannot auto-verify initramfs unlock plumbing. Manually verify the initramfs has cryptsetup support."
      ;;
  esac
  # 4b. Kernel cmdline must reference the LUKS container. Check GRUB,
  # systemd-boot loader entries, and a kernel/cmdline file (UKI setups).
  CMDLINE=""
  [ -f "$_root/etc/default/grub" ] && CMDLINE="$CMDLINE $(grep -E '^GRUB_CMDLINE_LINUX(_DEFAULT)?=' "$_root/etc/default/grub" 2>/dev/null | tr '\n' ' ')"
  [ -f "$_root/etc/kernel/cmdline" ] && CMDLINE="$CMDLINE $(cat "$_root/etc/kernel/cmdline" 2>/dev/null)"
  for f in "$_root"/boot/loader/entries/*.conf; do
    [ -f "$f" ] && CMDLINE="$CMDLINE $(grep -E '^options' "$f" 2>/dev/null)"
  done
  if echo "$CMDLINE" | grep -qE '\b(cryptdevice|rd\.luks\.uuid|rd\.luks\.name)='; then
    emit OK luks-cmdline "Kernel cmdline references LUKS"
  else
    emit FAIL luks-cmdline "LUKS in use but kernel cmdline has no cryptdevice= / rd.luks.uuid= — initramfs has nothing to unlock. Edit /etc/default/grub or /boot/loader/entries/*.conf and regenerate the bootloader config."
  fi
fi

# 5. Bootloader installed on disk
GRUB_CT=0
SDB_CT=0
[ -d "$_root/boot/grub" ] && GRUB_CT=$(ls "$_root/boot/grub" 2>/dev/null | wc -l)
[ -d "$_root/boot/grub2" ] && GRUB_CT=$(ls "$_root/boot/grub2" 2>/dev/null | wc -l)
[ -d "$_root/boot/EFI/systemd" ] && SDB_CT=1
[ -d "$_root/boot/loader" ] && SDB_CT=1
if [ "$GRUB_CT" -gt 1 ] || [ "$SDB_CT" -ge 1 ]; then
  emit OK bootloader "Bootloader files found (grub=$GRUB_CT, sd-boot=$SDB_CT)"
else
  emit FAIL bootloader "No bootloader installed in /boot — system will not boot. Run grub-install / grub2-install / bootctl install inside chroot."
fi

# 6. grub.cfg mtime vs /etc/default/grub
GRUB_CFG=""
[ -f "$_root/boot/grub/grub.cfg" ] && GRUB_CFG="$_root/boot/grub/grub.cfg"
[ -f "$_root/boot/grub2/grub.cfg" ] && GRUB_CFG="$_root/boot/grub2/grub.cfg"
if [ -n "$GRUB_CFG" ] && [ -f "$_root/etc/default/grub" ]; then
  CFG_T=$(stat -c %Y "$GRUB_CFG" 2>/dev/null)
  DEF_T=$(stat -c %Y "$_root/etc/default/grub" 2>/dev/null)
  if [ "$CFG_T" -ge "$DEF_T" ]; then
    emit OK grub-cfg "grub.cfg newer than /etc/default/grub"
  else
    emit FAIL grub-cfg "/etc/default/grub edited AFTER grub.cfg — run \`grub-mkconfig -o $GRUB_CFG\` (Arch/Debian) or \`grub2-mkconfig -o $GRUB_CFG\` (Fedora/SUSE) inside chroot"
  fi
fi

# 7. Root recovery: at least one of these must be true
ROOT_HAS_PW=0
SUDOER_OK=0
if [ -f "$_root/etc/shadow" ]; then
  RHASH=$(awk -F: '$1=="root"{print $2}' "$_root/etc/shadow" 2>/dev/null)
  case "$RHASH" in
    ''|'!'|'!!'|'!*'|'*'|'x') ROOT_HAS_PW=0 ;;
    *) ROOT_HAS_PW=1 ;;
  esac
fi
# Privileged group: wheel (Arch/Fedora/SUSE) or sudo (Debian/Ubuntu).
if [ -f "$_root/etc/group" ]; then
  ADMIN_USERS=$(awk -F: '$1=="wheel" || $1=="sudo"{print $4}' "$_root/etc/group" 2>/dev/null | tr -d ' ')
  if [ -n "$ADMIN_USERS" ]; then
    if grep -qE '^\s*%(wheel|sudo)\s+ALL=\(ALL(:ALL)?\)\s+(NOPASSWD:)?ALL' "$_root/etc/sudoers" 2>/dev/null \
       || (ls "$_root/etc/sudoers.d/" 2>/dev/null | xargs -I{} grep -qlE '^\s*%(wheel|sudo)\s+ALL' "$_root/etc/sudoers.d/{}" 2>/dev/null); then
      SUDOER_OK=1
    fi
  fi
fi
if [ "$ROOT_HAS_PW" = "1" ] || [ "$SUDOER_OK" = "1" ]; then
  emit OK recovery "Root recovery available (root_pw=$ROOT_HAS_PW, admin-grp+sudoers=$SUDOER_OK)"
else
  emit FAIL recovery "Root account is locked AND no sudo-capable user in 'wheel' or 'sudo' group — emergency mode will be unrecoverable (sulogin refuses). Either run \`passwd\` to set root, OR create a user + add to wheel/sudo + ensure /etc/sudoers grants the group. THIS IS THE SINGLE MOST COMMON UNRECOVERABLE FAILURE."
fi
"""


def _smoke_test_preboot_script(
    host: str, user: str, key_path: str | None, port: int = 22
) -> str | None:
    """Run the check script against a target with no /mnt to verify the
    bash+script syntax parses on this target's shell. If the script
    crashes on first SSH (shell compatibility bug, missing `bash`,
    etc.), return an error string; otherwise None.

    Fast + side-effect-free: the script's first branch emits a single
    SKIP line when /mnt has no /etc, so a successful self-test costs
    us one brief SSH round-trip.
    """
    try:
        wrapped = "bash -s <<'QH_PREBOOT_EOF'\n" + _PRE_REBOOT_CHECK_SCRIPT + "\nQH_PREBOOT_EOF\n"
        rc, out, err = _ssh_exec(host, user, wrapped, key_path, port=port, timeout=30)
    except Exception as exc:
        return f"SSH error during self-test: {exc}"
    if "STATUS|" not in (out or ""):
        # Script emitted nothing recognisable — likely a parse error or
        # `bash` missing on the target.
        return f"self-test output has no STATUS lines (rc={rc}). stderr: {(err or '')[:300]}"
    return None


def _run_pre_reboot_checks(
    host: str, user: str, key_path: str | None, port: int = 22
) -> tuple[list[str], list[str]]:
    """SSH into the live ISO and run pre-reboot safety checks against
    /mnt. Returns (failures, ok_lines) — failures should BLOCK the
    reboot until addressed; ok_lines are informational.
    """
    # Force execution through bash — archiso's default user often has
    # zsh as the login shell (Arch installer + pacstrap-base convention),
    # and the script uses bash-isms the POSIX shell syntax of zsh parses
    # differently. We wrap the script in a bash heredoc so zsh just
    # launches bash and pipes the script body as stdin; bash then parses
    # it with its own semantics regardless of the login shell.
    wrapped = "bash -s <<'QH_PREBOOT_EOF'\n" + _PRE_REBOOT_CHECK_SCRIPT + "\nQH_PREBOOT_EOF\n"
    rc, out, err = _ssh_exec(host, user, wrapped, key_path, port=port, timeout=60)
    failures: list[str] = []
    oks: list[str] = []
    if rc != 0 and not out:
        # Couldn't run the checks at all — don't block, but log it.
        return ([f"pre-reboot check script failed to run (rc={rc}): {err[:200]}"], [])
    for line in (out or "").splitlines():
        if not line.startswith("STATUS|"):
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        _, level, key, msg = parts
        entry = f"[{key}] {msg}"
        if level == "FAIL":
            failures.append(entry)
        elif level == "OK":
            oks.append(entry)
    return failures, oks


# Topic keywords we look for inside pre-reboot-guard failure lines.
# When the guard blocks a reboot, we extract these out of the failure
# text and then use them to detect whether subsequent agent commands
# are ACTUALLY addressing the failures or pivoting to unrelated work.
# Keep aligned with the keys emitted by the check script.
_BLOCK_TOPIC_VOCAB = frozenset(
    {
        # initramfs generators + config paths
        "mkinitcpio",
        "initramfs",
        "initramfs-tools",
        "dracut",
        "hooks",
        "encrypt",
        "sd-encrypt",
        "crypttab",
        "cryptsetup",
        "cryptsetup-initramfs",
        "luks",
        # bootloader
        "grub",
        "grub-install",
        "grub-mkconfig",
        "grub2-mkconfig",
        "bootctl",
        "systemd-boot",
        "cmdline",
        "cryptdevice",
        "rd.luks.uuid",
        "rd.luks.name",
        # filesystem + mounts
        "fstab",
        "genfstab",
        # recovery
        "passwd",
        "sudoers",
        "wheel",
        "sudo",
        "root",
        "shadow",
        "useradd",
        # kernel images
        "vmlinuz",
        "initrd",
        "linux",
    }
)


def _extract_topic_words(failures: list[str]) -> set[str]:
    """Return the set of block-topic keywords that appear in any
    failure line. Used by the pivot detector to score subsequent
    commands as 'addressing a block' vs 'pivoting away'.
    """
    import re as _re

    joined = " ".join(failures).lower()
    return {w for w in _BLOCK_TOPIC_VOCAB if _re.search(rf"\b{_re.escape(w)}\b", joined)}


def _command_addresses_topics(cmd: str, topics: set[str]) -> bool:
    """True iff ``cmd`` mentions at least one of the block's topic
    keywords — meaning the agent is at least TRYING to address the
    failures instead of running off on a tangent."""
    if not topics:
        return True  # no active block → every command is fine
    import re as _re
    import shlex as _shlex

    normalized_segments: list[str] = []
    for segment in _split_command_segments_for_lint(cmd or ""):
        try:
            tokens = _shlex.split(segment, posix=True)
        except ValueError:
            continue
        normalized = _strip_command_wrappers(tokens)
        if normalized:
            normalized_segments.append(" ".join(normalized).lower())
    haystack = "\n".join(normalized_segments)
    return any(_re.search(rf"\b{_re.escape(t)}\b", haystack) for t in topics)


def _recipe_apply_for_setup(profile: UserProfile) -> str:
    """Return the gallery recipe apply block for injection into the
    setup agent's system prompt. Empty string when no recipe pinned or
    the pin isn't in the gallery. Prefixed with a blank line + newline
    so it slots cleanly between adjacent prompt sections.
    """
    try:
        from quickhatch.ai.recipe_apply import recipe_apply_block

        picks = getattr(profile.preferences, "gallery_picks", []) or []
        block = recipe_apply_block(picks)
        if not block:
            return ""
        return block + "\n\n"
    except Exception:
        # Never let a recipe-metadata hiccup kill the setup agent.
        return ""


def _build_setup_runtime(gui_state: GUIState, port: int, user_feedback: str = "") -> SetupRuntime:
    bridge = gui_state.bridge
    bridge.set_current_phase("setup")
    profile = gui_state.profile
    config = profile.ai_config
    host = getattr(profile, "linux_ip", "") or ""
    user = getattr(profile, "linux_user", "") or ""
    ssh_port = 22
    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        try:
            ssh_port = int(port_str)
        except ValueError:
            pass
    key_path = _resolve_ssh_private_key_path(profile)
    native_search_active = _use_provider_native_search(config)

    migration_plan_md = ""
    if bridge.suggestions:
        plan_parts = [
            "The migration plan has already been generated during the analysis phase.\n"
            "Use this as your blueprint — do not re-plan from scratch.\n"
        ]
        for suggestion in bridge.snapshot_suggestions():
            plan_parts.append(f"\n### {suggestion.title}\n{suggestion.content}")
        migration_plan_md = "\n".join(plan_parts)

    from quickhatch.ai.install_primer import primer_prompt_block, resolve_primer

    distro_hint = (profile.preferences.distro_preference or "").strip()
    if not distro_hint and bridge.suggestions:
        for suggestion in bridge.snapshot_suggestions():
            if (
                getattr(suggestion, "category", "") == "distro"
                or "distribut" in (suggestion.title or "").lower()
            ):
                match = resolve_primer(suggestion.content or "")
                if match is not None:
                    distro_hint = match["canonical"]
                    break
    distro_primer_md = primer_prompt_block(distro_hint)
    config_for_runtime = deepcopy(config)
    session_user = {"value": user}

    def _repo_search(query: str) -> str:
        facts = _probe_setup_facts(host, session_user["value"], key_path, port=ssh_port)
        return _execute_setup_repo_search(
            host, session_user["value"], key_path, facts, query, port=ssh_port
        )[1]

    def _community_search(query: str) -> str:
        facts = _probe_setup_facts(host, session_user["value"], key_path, port=ssh_port)
        return _execute_setup_aur_search(
            host, session_user["value"], key_path, facts, query, port=ssh_port
        )[1]

    def _wait_for_reconnect(reconnect_user: str) -> tuple[bool, str, str]:
        ok = _wait_for_ssh_reconnect(
            host,
            reconnect_user,
            key_path,
            port=ssh_port,
            timeout=None,
            stop_flag_getter=lambda: bridge.stop_requested,
        )
        if ok:
            session_user["value"] = reconnect_user
        fresh_info = (
            _collect_remote_system_info(host, session_user["value"], key_path, port=ssh_port)
            if ok
            else ""
        )
        return ok, fresh_info, reconnect_user

    def _pre_reboot_result() -> tuple[bool, list[str], str]:
        failures, oks = _run_pre_reboot_checks(host, session_user["value"], key_path, port=ssh_port)
        summary_lines = [f"✓ {line}" for line in oks] + [f"✗ {line}" for line in failures]
        return (not failures, failures, "\n".join(summary_lines))

    return SetupRuntime(
        profile=profile,
        bridge=bridge,
        config=config_for_runtime,
        host=host,
        user=user,
        ssh_port=ssh_port,
        key_path=key_path,
        system_info_fn=lambda: _collect_remote_system_info(
            host, session_user["value"], key_path, port=ssh_port
        ),
        call_model_text=lambda model_config, messages: _call_setup_model_text(
            model_config,
            model_config.endpoint or "",
            model_config.model_name or "",
            model_config.api_key,
            messages,
        ),
        probe_facts=lambda **kwargs: _probe_setup_facts_typed(
            host,
            session_user["value"],
            key_path,
            port=ssh_port,
            **kwargs,
        ),
        detect_live_env=lambda: _detect_live_env(
            host, session_user["value"], key_path, port=ssh_port
        ),
        ssh_exec=lambda command, timeout: _ssh_exec(
            host, session_user["value"], command, key_path, timeout=timeout, port=ssh_port
        ),
        repo_search=_repo_search,
        community_search=_community_search,
        web_search=lambda query, max_results: _execute_web_search(query, max_results),
        wait_for_reconnect=_wait_for_reconnect,
        run_pre_reboot_checks=_pre_reboot_result,
        smoke_test_preboot_script=lambda: _smoke_test_preboot_script(
            host, session_user["value"], key_path, port=ssh_port
        ),
        bootstrap_nopasswd_sudo=lambda facts: _bootstrap_setup_facts_typed(
            host, session_user["value"], key_path, profile, bridge, facts, port=ssh_port
        ),
        runtime_prompt_block=lambda facts, is_live: _setup_runtime_prompt_block(
            {
                "package_manager": facts.package_manager,
                "aur_helper": facts.community_helper,
                "current_user": facts.current_user,
                "is_root": "yes" if facts.is_root else "no",
                "sudo_nopasswd": "yes" if facts.sudo_nopasswd else "no",
            },
            is_live,
            native_search_active=native_search_active,
        ),
        migration_plan_md=migration_plan_md
        + (f"\n\n== USER FEEDBACK ==\n{user_feedback.strip()}" if user_feedback.strip() else ""),
        distro_primer_md=distro_primer_md,
        target_distro_family=distro_hint,
        native_search_active=native_search_active,
    )


def _run_setup_agent(gui_state: GUIState, port: int, user_feedback: str = "") -> None:
    runtime = _build_setup_runtime(gui_state, port, user_feedback)
    if not runtime.host or not runtime.user:
        runtime.bridge.add_message("agent", "Error: No Linux IP/username configured.")
        return
    try:
        summary = SetupOrchestrator(runtime).run()
    except Exception as exc:
        summary = f"Agent stopped: {exc}"
        runtime.bridge.add_message("agent", f"⚠️ {summary}")
    import requests as req

    req.post(
        f"http://localhost:{port}/api/done",
        json={"summary": summary},
        timeout=5,
    )


def _run_setup_agent_legacy(gui_state: GUIState, port: int, user_feedback: str = "") -> None:
    """Run the setup phase as an agentic loop: SSH into the Linux machine,
    let the LLM decide what to run, execute it, feed results back, repeat.

    The model maintains full conversation history and adapts to errors,
    unexpected state, and changing conditions on the remote machine.
    """
    import requests as req

    base = f"http://localhost:{port}"
    bridge = gui_state.bridge
    # Tag every add_message() in this thread as setup-phase so the UI
    # filter doesn't show these shell commands on the Analysis step.
    bridge.set_current_phase("setup")
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

    key_path = _resolve_ssh_private_key_path(profile)
    native_search_active = _use_provider_native_search(config)

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

        # Step 1a: Self-test the pre-reboot check script against this
        # target's shell environment. If it parses and produces STATUS
        # output, great — the guard will work when needed. If it
        # doesn't (e.g. bash missing, heredoc wrapper misread),
        # surface a loud warning now rather than at reboot time.
        _preboot_err = _smoke_test_preboot_script(host, user, key_path, port=ssh_port)
        if _preboot_err:
            bridge.add_message(
                "agent",
                "⚠️ Pre-reboot guard self-test failed: "
                + _preboot_err
                + ". The guard will still run at reboot time but its output may be "
                + "unreliable — watch for 'pre-reboot check script failed to run' "
                + "and diagnose manually if it fires.",
            )
        else:
            bridge.add_message(
                "agent",
                "✓ Pre-reboot guard self-test passed — checks will run correctly at reboot time.",
            )

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
        setup_facts = _probe_setup_facts(host, user, key_path, port=ssh_port)
        if not is_live:
            setup_facts = _maybe_bootstrap_nopasswd_sudo(
                host,
                user,
                key_path,
                profile,
                bridge,
                facts=setup_facts,
                port=ssh_port,
            )
        runtime_block = _setup_runtime_prompt_block(
            setup_facts, is_live, native_search_active=native_search_active
        )

        # Step 2b: Resolve the target distro before building any app-catalog
        # context. Runtime facts can describe the live ISO, not the distro we
        # are about to install.
        from quickhatch.ai.install_primer import primer_prompt_block, resolve_primer

        distro_hint = (profile.preferences.distro_preference or "").strip()
        if not distro_hint and bridge.suggestions:
            for s in bridge.snapshot_suggestions():
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

        # Step 3: Build profile context
        profile_dict = asdict(profile)
        if profile_dict.get("ai_config", {}).get("api_key"):
            profile_dict["ai_config"]["api_key"] = "***REDACTED***"
        # Trim the scanned apps list the same way analysis-phase prompts do:
        # if the user picked a specific apps_to_install subset, include only
        # those; a 317-app dump × every setup iteration would blow past the
        # context window of any local model on long runs.
        picks = gui_state.profile.apps_to_install or []
        if picks:
            pick_names = {p.lower() for p in picks}
            profile_dict["apps"] = [
                a
                for a in profile_dict.get("apps", [])
                if (a.get("name") or "").lower() in pick_names
            ]
        from quickhatch.analysis.app_catalog import selected_app_catalog_context

        selected_app_catalog = selected_app_catalog_context(
            list(gui_state.profile.apps_to_install or []),
            list(gui_state.profile.apps or []),
            distro_family=distro_hint or getattr(setup_facts, "distro_family", ""),
        )
        if selected_app_catalog:
            profile_dict["selected_app_catalog"] = selected_app_catalog
        profile_json = json.dumps(profile_dict, indent=2, default=str)

        endpoint = config.endpoint or ""
        model = config.model_name or ""
        api_key = config.api_key

        # Cross-distro pre-reboot rules. Always inject this — applies to
        # every install regardless of distro family. Documents the seven
        # invariants the harness's pre-reboot guard enforces, so the
        # agent knows what it MUST satisfy before issuing reboot.
        try:
            from importlib import resources as _res

            common_rules = (
                _res.files("quickhatch.data.install_primers")
                .joinpath("common-pre-reboot-rules.md")
                .read_text(encoding="utf-8")
            )
            common_rules_block = (
                "== PRE-REBOOT RULES (cross-distro, mandatory) ==\n\n" + common_rules + "\n\n"
            )
        except Exception:
            common_rules_block = ""

        # Step 4b: Gather any existing migration plan from the analysis phase
        migration_plan_md = ""
        if bridge.suggestions:
            plan_parts = [
                "The migration plan has already been generated during the analysis phase.\n"
                "Use this as your blueprint — do not re-plan from scratch.\n"
            ]
            for s in bridge.snapshot_suggestions():
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

        # === TEMPORAL GROUNDING ===
        # Pinned at the very top of the system prompt so the model reads it
        # before any other context. Motivated by a live run where Gemma-4
        # insisted the VM clock showing 2026 was wrong (its training data
        # said <=2024) and rolled the clock back mid-install, invalidating
        # every TLS cert on the Arch CDN.
        import datetime as _dt

        _today = _dt.datetime.now().strftime("%Y-%m-%d (%A)")
        _temporal_block = (
            "== TEMPORAL GROUNDING (READ THIS FIRST) ==\n"
            f"Today's real-world date is: **{_today}**.\n"
            "This fact is verified by the orchestration harness, not by your\n"
            "training data. If the remote system's clock agrees with this\n"
            "date, the clock is CORRECT — do not 'fix' it by setting it\n"
            "backwards, even if pacman/apt/curl reports signature or TLS\n"
            "errors that look like future-date problems. Those errors have\n"
            "other causes (stale keyrings, deprecated DB signing, broken\n"
            "mirrors). Rolling the clock back will only add broken TLS on\n"
            "top of the original issue.\n\n"
            "If you are EVER tempted to run `date -s`, `hwclock --set`, or\n"
            "to disable NTP to 'correct' a date that already matches the\n"
            "one above — STOP and diagnose the actual root cause instead.\n\n"
        )

        if native_search_active:
            search_mode_block = (
                "You have a SMALL, EXPLICIT ACTION SET. Every response must be one or more JSON objects.\n"
                "Use only these actions:\n"
                '  {"action": "ssh_exec", "command": "your shell command here", "reason": "why you are running this"}\n'
                '    Optional: add "timeout_seconds": 7200 for commands you KNOW may run longer than 30 minutes (big package installs, AUR builds, compiles).\n'
                '  {"action": "status", "message": "what you are doing now"}\n'
                '  {"action": "done", "summary": "brief summary of what was accomplished"}\n\n'
                "SEARCH DISCIPLINE:\n"
                "- Provider-native web search is ENABLED for this run.\n"
                "- Use the model provider's own search capability whenever you need current package names, distro-specific instructions, or up-to-date compatibility data.\n"
                "- Do NOT emit QuickHatch harness search actions (`repo_search`, `aur_search`, `web_search`) in this mode.\n"
                "- If an install command fails because a package was not found, search natively first and then issue a corrected command.\n\n"
            )
        else:
            search_mode_block = (
                "You have a SMALL, EXPLICIT ACTION SET. Every response must be one or more JSON objects.\n"
                "Use only these actions:\n"
                '  {"action": "ssh_exec", "command": "your shell command here", "reason": "why you are running this"}\n'
                '    Optional: add "timeout_seconds": 7200 for commands you KNOW may run longer than 30 minutes (big package installs, AUR builds, compiles).\n'
                '  {"action": "repo_search", "query": "package or app name", "reason": "why you are searching"}\n'
                '  {"action": "aur_search", "query": "package or app name", "reason": "why you are searching"}\n'
                '  {"action": "web_search", "query": "up-to-date package or distro question", "reason": "why you are searching", "max_results": 5}\n'
                '  {"action": "status", "message": "what you are doing now"}\n'
                '  {"action": "done", "summary": "brief summary of what was accomplished"}\n\n'
                "ACTION DISCIPLINE:\n"
                "- `ssh_exec` is for commands on the remote machine.\n"
                "- `repo_search` is for official package-repository lookups on the target distro.\n"
                "- `aur_search` is for Arch AUR lookups when an app/theme is likely AUR-only.\n"
                "- `web_search` is for current public information when package names or distro instructions are uncertain.\n"
                "- If an install command fails because a package was not found, your NEXT action must be one of the search actions, not another guessed install command.\n"
                "- Before installing a package whose exact name is uncertain, look it up first. Linux package ecosystems are fragile; certainty beats guessing.\n\n"
            )

        system_prompt = (
            _temporal_block
            + "You are a Linux system administrator. You are connected to a remote machine via SSH "
            "and your job is to execute the migration plan (provided below) on the remote machine.\n\n"
            + search_mode_block
            + "When you are completely done with the entire setup, respond with:\n"
            '  {"action": "done", "summary": "brief summary of what was accomplished"}\n\n'
            "To post a progress update to the user (shown in the UI), respond with:\n"
            '  {"action": "status", "message": "what you are doing now"}\n\n'
            "CORE PRINCIPLE:\n"
            "Your goal is a WORKING, CONFIGURED system — not just installed packages.\n"
            "Installing a theme is NOT the same as activating it. Installing a package is NOT the\n"
            "same as configuring it. Think about what the user will actually SEE and USE at the end,\n"
            "and work backwards to make that happen.\n\n"
            "SEARCH-FIRST DISCIPLINE (READ THIS TWICE):\n"
            "You have live internet access via `web_search`, `aur_search`, and `repo_search`. USE THEM.\n"
            "A 5-second search is ALWAYS cheaper than a 5-minute loop of failed pacman commands.\n"
            "Before issuing an install command, if the exact package name is not 100% certain\n"
            "(e.g. 'is it `lmstudio-bin` or `lm-studio-bin`? `claude-desktop` or `claude-desktop-bin`?'),\n"
            "SEARCH FIRST. Do not guess. Do not try variations of the same wrong name.\n\n"
            "IF A PACKAGE INSTALL FAILS with 'target not found', 'unable to locate', 'no match',\n"
            "or any 'conflicting dependencies' error, your NEXT action is NOT another variation of\n"
            "the same install with more flags. Your next action is ONE OF:\n"
            "  (a) a `repo_search` / `aur_search` / `web_search` to find the real package name, or\n"
            "  (b) an `ask` action to the user if the package you want doesn't seem to exist.\n"
            "Escalating flags (`--noconfirm` → `--nodeps` → `-Rdd`) to work around a conflict is\n"
            "ALMOST ALWAYS a sign you picked the wrong package variant. Re-read the conflict\n"
            "message: it usually tells you exactly which dep is incompatible. If `openai-codex`\n"
            "wants `nodejs` and you already have `nodejs`, DROP `nodejs-lts-iron` from the command\n"
            "— you never needed it. The harness WILL detect escalation loops and block you.\n\n"
            "WHEN TO STOP AND ASK:\n"
            "If a single app in the user's list would take >10 minutes to install (big AUR source\n"
            "builds, multi-gigabyte downloads on slow links, long-running npm postinstalls), or\n"
            "if the package you need doesn't exist under any obvious name, emit an `ask` action:\n"
            '  {"action": "ask", "question": "<X> is a 20-minute AUR build and not critical for '
            'first-login. Install now, skip, or replace with the official AppImage from <Y>?", '
            '"choices": ["install", "skip", "appimage"]}\n'
            "The user's answer comes back in the next turn's feedback and you adapt. Do NOT "
            "silently skip important apps, and do NOT silently commit to long installs without\n"
            "warning — both surprise the user in bad ways.\n\n"
            "DESKTOP-APPLY DISCIPLINE:\n"
            "- KDE/Plasma live apply must target the REAL logged-in user's session bus. Do NOT use\n"
            "  `dbus-run-session` for `plasma-apply-lookandfeel` or `plasma-apply-wallpaperimage`.\n"
            "- If a Plasma GUI session is active, use `XDG_RUNTIME_DIR=/run/user/<uid>` and\n"
            "  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus` when talking to Plasma.\n"
            "- If no GUI session is active, write KDE config files directly and tell the user a\n"
            "  logout/login is required before visuals update.\n"
            "- For GTK apps on KDE, `gsettings` is optional polish only. It does NOT prove the KDE shell\n"
            "  theme was applied.\n\n"
            "INSTALL-BATCH DISCIPLINE:\n"
            "- Keep optional desktop clients and heavyweight wrappers in separate commands from core tools.\n"
            "- If the user asks to skip one package from a batch, immediately reissue a reduced command;\n"
            "  do not keep the skipped package in the transaction.\n\n"
            "DEFAULT ACCOUNT PASSWORD:\n"
            f"Set BOTH the root account AND the primary user account's password to the literal\n"
            f"string `{profile.default_account_password}` during install. This is a KNOWN CONSTANT —\n"
            "it lets the harness pipe it to `sudo -S` during first post-reboot connect to grant\n"
            "NOPASSWD sudo, so subsequent commands don't hang on an invisible sudo prompt (SSH\n"
            "has no pty). Use one of these patterns inside chroot:\n"
            f"  echo 'root:{profile.default_account_password}' | chpasswd\n"
            f"  echo '<username>:{profile.default_account_password}' | chpasswd\n"
            "After the first successful post-reboot connect, the harness auto-grants NOPASSWD\n"
            "sudo via a sudoers.d drop-in. Your migration summary at the END must include a\n"
            "VERY PROMINENT 'CHANGE YOUR PASSWORD IMMEDIATELY' block telling the user to run\n"
            "`passwd` and `sudo passwd` on first login — the default MUST NOT persist.\n\n"
            "WARNINGS vs ERRORS:\n"
            "Non-fatal warnings are NOT your problem unless they block a later command.\n"
            "Pacman 'directive X not recognized', GTK accessibility warnings, deprecation\n"
            "notices, 'already enabled', 'nothing to do' — skip them. You are on a goal,\n"
            "not a cleanup crew. Only investigate a message when a SUBSEQUENT command\n"
            "depends on that message being resolved. A command that prints warnings but\n"
            "exits 0 IS A SUCCESS. Move on.\n\n"
            "RULES:\n"
            "- Run ONE command at a time, then analyze the output before deciding the next step\n"
            "- If a command fails, diagnose WHY from the error output and fix it — don't just retry\n"
            "- Before running a command, ask yourself: 'Have I already run something equivalent?'\n"
            "  If yes, READ THE PREVIOUS OUTPUT instead of re-probing. Re-probing the same\n"
            "  state (`grep X file`, `pacman -Q Y`, `systemctl is-enabled Z`) wastes context.\n"
            "- Use non-interactive flags always (--noconfirm, -y, DEBIAN_FRONTEND=noninteractive, etc.)\n"
            "- NEVER run a command that prompts for input (sudo password, `read`, `makepkg -i`,\n"
            '  `ssh-keygen` without -N "", `passwd` without piped input). SSH has no pty — the\n'
            "  prompt is invisible and the command hangs silently. See PRE-REBOOT RULES for the\n"
            "  AUR/makepkg/sudo pattern: split `makepkg -si` into `makepkg -s` + `pacman -U`\n"
            "  and grant NOPASSWD sudo for the install window via /etc/sudoers.d/.\n"
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
            + runtime_block
            + _remote_setup_mode_block(profile)
            + live_env_block
            + common_rules_block
            + (
                "== TARGET HARDWARE OVERRIDE ==\n"
                "The user indicated their install TARGET differs from the machine that "
                "ran QuickHatch's scan. Treat the description below as ground truth for "
                "driver / GPU / CPU / WiFi / disk decisions. The REMOTE SYSTEM INFO above "
                "reflects the live-ISO kernel's view of the TARGET hardware (what's "
                "actually there); the description below clarifies anything the scan "
                "might have misread or that's relevant beyond what `uname` / `lspci` "
                "show. If the live-ISO view contradicts the description, prefer the "
                "live-ISO view (it's running ON the target) but flag the mismatch.\n\n"
                + (profile.target_hardware_override or "").strip()
                + "\n\n"
                if (profile.target_hardware_override or "").strip()
                else ""
            )
            + distro_primer_md
            + (
                f"== MIGRATION PLAN (pre-generated) ==\n{migration_plan_md}\n\n"
                if migration_plan_md
                else ""
            )
            + _recipe_apply_for_setup(profile)
            + (
                f"== USER FEEDBACK (the user interjected — follow this!) ==\n{user_feedback}\n\n"
                if user_feedback
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
        # Cap size — a long install can otherwise produce an ever-growing list
        # that gets re-persisted to disk on every suggestion add.
        _RESULTS_LOG_MAX = 100
        # Track the post-install user the agent creates in chroot. On the
        # pre-reboot side we're logged in as root (live ISO); after reboot
        # the target has no root SSH by default, and login must switch to
        # the newly-created user. If we never see a useradd we keep the
        # current linux_user unchanged.
        created_user: str | None = None
        # Short history of (command, exit_code) so we can detect when the
        # agent gets stuck hammering the same failing command. When we
        # see the same command fail N times in a row we inject a
        # recovery hint into the chat feed (lock files, stale state,
        # permissions) — covers the whole 'WhiteSur /tmp/*.lock' class
        # where the real error is hidden and the model loops.
        recent_exits: list[tuple[str, int]] = []
        # Count of consecutive blank model responses — three in a row
        # ends the run instead of looping forever.
        blank_turns = 0
        # Remember why the latest blank turn happened. Real provider
        # transport failures should stop with the underlying error,
        # not collapse into a misleading "blank model output".
        last_blank_cause: str | None = None
        # Same budget for consecutive non-JSON (unparseable) responses.
        # Without this cap, a model stuck emitting malformed JSON (e.g.
        # invalid `\[...\]` escapes) loops until MAX_ITERATIONS.
        nonjson_turns = 0
        # Sliding window of recent command "reason" fields, used by the
        # stall detector to notice when the model has burned N turns on
        # the same topic (e.g. 10 turns investigating pacman.conf
        # warnings that don't actually block anything).
        recent_reasons: list[str] = []
        # Sliding window of recent literal commands, used by the
        # flag-escalation detector. Separate from recent_reasons because
        # the reasons are free text (good for topic detection) while the
        # commands are what the intent-signature normaliser needs.
        recent_commands: list[str] = []
        # Track iterations since last plan re-anchor so we can re-inject
        # the remaining-goal checklist periodically. Without this, on
        # long runs the early plan falls out of the attention window
        # and the model drifts into tangents.
        last_anchor_iter = 0
        # Track when we last re-probed the installed system for a
        # rolling state-refresh (installed packages, failed units,
        # free disk space). Refreshed setup_facts already cover
        # package manager + live/installed flag, but the model also
        # needs to know "what's already installed" so it doesn't
        # re-fetch or re-configure steps already done earlier.
        last_state_refresh_iter = 0
        # Sticky pre-reboot-guard state. When the guard blocks a reboot,
        # we latch the failures here and keep re-injecting them on every
        # turn until the next reboot attempt's check passes (or the
        # agent explicitly clears it via an action we recognise). Keeps
        # the agent focused on the specific failures instead of
        # drifting to unrelated work as soon as the first block scrolls
        # out of context.
        sticky_block_failures: list[str] = []
        # Topic keywords pulled from the failure text; used to detect
        # when the agent issues an ssh_exec whose command is clearly
        # NOT addressing any of the blocked invariants ("pivot").
        sticky_block_topics: set[str] = set()
        # Consecutive pivot count since the last block — if >= 3, we
        # stop and /api/ask the user so they can steer.
        pivot_counter = 0
        # Track how many bridge.messages we've already forwarded into the
        # model conversation. The UI's chat input posts to /wizard/chat,
        # which calls bridge.add_message("user", msg). Previously the
        # setup loop never consulted bridge.messages, so anything the
        # user typed mid-install was INVISIBLE to the agent (e.g. "don't
        # encrypt my drive" arrived after the LUKS step had already
        # run and was silently ignored). Every iteration we check for
        # new user messages since _last_bridge_msg_idx and prepend them
        # to pending_feedback with a loud marker so they actually land.
        with bridge.lock:
            _last_bridge_msg_idx = len(bridge.messages)

        for _iteration in range(MAX_ITERATIONS):
            # Plan re-anchor: on long runs the original MIGRATION PLAN
            # falls out of the attention window. Every ~15 turns we
            # reinject a compact "remaining goal" nudge so the model
            # doesn't drift off into tangents. Only active when we
            # actually have a migration_plan_md to anchor against.
            if _iteration > 0 and _iteration - last_anchor_iter >= 15 and migration_plan_md:
                last_anchor_iter = _iteration
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "ANCHOR CHECK: review the MIGRATION PLAN in your "
                            "system prompt. What concrete items from it are "
                            "NOT yet done on the remote system? Your next "
                            "command should advance one of those items. If "
                            "you've been investigating something tangential "
                            "(stale warnings, non-blocking probes), drop it "
                            "and return to the plan."
                        ),
                    }
                )
            # Rolling state-refresh — once out of the live ISO, every 8
            # turns (or after the first time we're past the reboot) we
            # snapshot a few cheap probes and feed them back as context.
            # Without this the model is planning against its original
            # assumption of the system state and can re-run commands that
            # already succeeded earlier (e.g. re-install a package that's
            # already there, re-configure a service that's already active).
            # Only runs on the installed system — during live ISO the
            # useful state is all behind chroot and not worth probing.
            if not is_live and _iteration > 0 and _iteration - last_state_refresh_iter >= 8:
                last_state_refresh_iter = _iteration
                pm = (setup_facts or {}).get("package_manager", "").lower()
                pkg_count_cmd = {
                    "pacman": "pacman -Q 2>/dev/null | wc -l",
                    "apt": "dpkg-query -W 2>/dev/null | wc -l",
                    "dnf": "rpm -qa 2>/dev/null | wc -l",
                    "zypper": "rpm -qa 2>/dev/null | wc -l",
                }.get(pm, "echo 0")
                refresh_probe = (
                    f"echo '===PKG==='; {pkg_count_cmd}; "
                    "echo '===FAILED==='; "
                    "systemctl --failed --no-legend 2>/dev/null | awk '{print $1}'; "
                    "echo '===DISK==='; "
                    "df -h / /home 2>/dev/null | awk 'NR>1 {print $1, $4, $5}'; "
                    "echo '===SERVICES==='; "
                    "for s in NetworkManager sshd sddm gdm lightdm docker; do "
                    "  if systemctl is-active $s.service >/dev/null 2>&1; "
                    'then echo "$s=active"; fi; done; '
                    "echo '===UPTIME==='; uptime -p"
                )
                _rc, _rout, _ = _ssh_exec(
                    host, user, refresh_probe, key_path, timeout=15, port=ssh_port
                )
                if _rc == 0 and _rout:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[harness state refresh — snapshot of the remote system "
                                "RIGHT NOW. Use this to avoid re-running steps already done, "
                                "and to notice things that regressed.]\n"
                                f"{_rout.strip()[:2000]}"
                            ),
                        }
                    )
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

            try:
                response_text = _call_setup_model_text(
                    config,
                    endpoint,
                    model,
                    api_key,
                    messages,
                )
            except Exception as call_exc:
                # Surface the actual error to the user instead of letting
                # it kill the thread silently. _http_post_with_retry
                # already includes the response body snippet.
                last_blank_cause = str(call_exc)[:500]
                bridge.add_message(
                    "agent",
                    f"⚠️  LLM call failed: {last_blank_cause}. Retrying next iteration.",
                )
                response_text = ""

            # If the model produced nothing (empty content AND empty
            # reasoning), don't append a blank assistant turn — some
            # providers reject the next request's empty-content turn,
            # and _extract_json_objects on '' sends us into the
            # "non-JSON" branch forever. Retry up to 3 times, then bail.
            if not response_text or not response_text.strip():
                blank_turns += 1
                if blank_turns >= 3:
                    if last_blank_cause:
                        bridge.add_message(
                            "agent",
                            "Model backend failed 3 times in a row — stopping. "
                            f"Last error: {last_blank_cause}",
                        )
                        summary = (
                            "Agent stopped: model backend unavailable. "
                            f"Last error: {last_blank_cause[:200]}"
                        )
                    else:
                        bridge.add_message(
                            "agent",
                            "Model returned blank responses 3 times in a row — "
                            "stopping. Try a different model or temperature.",
                        )
                        summary = "Agent stopped: blank model output."
                    req.post(
                        f"{base}/api/done",
                        json={"summary": summary},
                        timeout=5,
                    )
                    return
                if last_blank_cause:
                    bridge.add_message(
                        "agent",
                        f"Model backend unavailable ({blank_turns}/3) — retrying.",
                    )
                else:
                    bridge.add_message(
                        "agent",
                        f"Model returned empty content ({blank_turns}/3) — retrying.",
                    )
                    # Add a nudge so the retry isn't an identical request —
                    # identical input → identical blank output → we burn all
                    # 3 strikes with nothing to show for it. Vary the
                    # context by asking the model to acknowledge + produce
                    # one small action.
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was empty. Please respond with a "
                                "single JSON object now, e.g. "
                                '{"action":"ssh_exec","command":"echo alive"} to confirm '
                                "you are still running, or "
                                '{"action":"done","summary":"..."} if you are stuck.'
                            ),
                        }
                    )
                continue
            blank_turns = 0  # reset on any real response
            last_blank_cause = None

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
                nonjson_turns += 1
                bridge.add_message(
                    "agent",
                    f"Model response (non-JSON {nonjson_turns}/5): {response_text[:200]}",
                )
                if nonjson_turns >= 5:
                    bridge.add_message(
                        "agent",
                        "Model produced 5 unparseable responses in a row — stopping. "
                        "Common cause: shell regex escapes (\\[, \\], \\$) inside a JSON "
                        "string value. Try a different model or restart the bridge.",
                    )
                    req.post(
                        f"{base}/api/done",
                        json={"summary": "Agent stopped: repeated non-JSON output."},
                        timeout=5,
                    )
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Please respond with a valid JSON object as specified. Use one of: "
                            '{"action":"ssh_exec","command":"...","reason":"..."}, '
                            '{"action":"repo_search","query":"...","reason":"..."}, '
                            '{"action":"aur_search","query":"...","reason":"..."}, '
                            '{"action":"web_search","query":"...","reason":"..."}, '
                            '{"action":"status","message":"..."}, or '
                            '{"action":"done","summary":"..."}.'
                        ),
                    }
                )
                continue

            # Successful parse — reset the non-JSON strike counter.
            nonjson_turns = 0

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
                    final_failures = _verify_setup_completion(
                        host,
                        user,
                        key_path,
                        summary,
                        results_log,
                        port=ssh_port,
                        apps_to_install=list(profile.apps_to_install or []),
                        setup_facts=setup_facts,
                    )
                    if final_failures:
                        verification_msg = (
                            "[quickhatch final verification]\n"
                            "Do NOT claim completion yet. The following end-state checks still failed:\n"
                            + "\n".join(f"- {item}" for item in final_failures)
                            + "\nFix the missing apply/verification steps first, then verify again and only "
                            "after that emit another `done` action."
                        )
                        bridge.add_message(
                            "agent",
                            "🛑 Final verification failed — continuing setup instead of accepting `done`.",
                        )
                        for item in final_failures:
                            bridge.add_message("agent", f"  ✗ {item}")
                        pending_feedback.append(verification_msg)
                        results_log.append(
                            f"**[done blocked]** — {len(final_failures)} final verification failure(s)"
                        )
                        continue
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

                if act == "repo_search":
                    query = (action.get("query") or "").strip()
                    if not query:
                        pending_feedback.append(
                            "Error: empty query. Provide a repo_search query string."
                        )
                        continue
                    success, lookup_text = _execute_setup_repo_search(
                        host,
                        user,
                        key_path,
                        setup_facts,
                        query,
                        port=ssh_port,
                    )
                    pending_feedback.append(lookup_text)
                    results_log.append(
                        f"**[repo_search] {query}** — {'OK' if success else 'NO MATCH / ERROR'}"
                    )
                    continue

                if act == "aur_search":
                    query = (action.get("query") or "").strip()
                    if not query:
                        pending_feedback.append(
                            "Error: empty query. Provide an aur_search query string."
                        )
                        continue
                    success, lookup_text = _execute_setup_aur_search(
                        host,
                        user,
                        key_path,
                        setup_facts,
                        query,
                        port=ssh_port,
                    )
                    pending_feedback.append(lookup_text)
                    results_log.append(
                        f"**[aur_search] {query}** — {'OK' if success else 'NO MATCH / ERROR'}"
                    )
                    continue

                if act == "web_search":
                    query = (action.get("query") or "").strip()
                    if not query:
                        pending_feedback.append(
                            "Error: empty query. Provide a web_search query string."
                        )
                        continue
                    success, lookup_text = _execute_setup_web_lookup(
                        query,
                        action.get("max_results", 5),
                    )
                    pending_feedback.append(lookup_text)
                    results_log.append(
                        f"**[web_search] {query}** — {'OK' if success else 'NO MATCH / ERROR'}"
                    )
                    continue

                if act == "ask":
                    # Blocking question to the user. Previously the agent
                    # could only `message` the user (one-way) and then
                    # immediately moved on to the next command — if it
                    # "wanted an answer", there was no way to pause.
                    # `ask` mirrors /api/ask semantics: add the question
                    # to the bridge, wait up to 10 min for an answer,
                    # feed it back into the next turn as user feedback.
                    question = (action.get("question") or "").strip()
                    if not question:
                        pending_feedback.append(
                            "Error: empty question. Provide 'question' (and optionally "
                            "'choices' list, 'timeout' seconds) when using action=ask."
                        )
                        continue
                    choices = action.get("choices")
                    if choices is not None and not isinstance(choices, list):
                        choices = None
                    try:
                        ask_timeout = int(action.get("timeout", 600))
                    except (TypeError, ValueError):
                        ask_timeout = 600
                    # Clamp so a pathological model can't park the loop
                    # for hours with timeout=999999.
                    ask_timeout = max(30, min(ask_timeout, 1800))
                    q = bridge.add_question(question, choices)
                    bridge.add_message(
                        "agent",
                        f"❓ Waiting for your answer: {question}",
                    )
                    results_log.append(f"**[ask]** {question}")
                    # Block the setup loop until the user answers, they
                    # hit Stop, or the timeout expires. Check stop_requested
                    # on a short poll so Stop stays responsive.
                    answered = False
                    waited = 0
                    poll_interval = 2
                    while waited < ask_timeout:
                        if q.answered.wait(timeout=poll_interval):
                            answered = True
                            break
                        if bridge.stop_requested:
                            break
                        waited += poll_interval
                    if bridge.stop_requested:
                        # Stop was hit while waiting — let the main loop's
                        # stop handler catch it on the next iteration.
                        pending_feedback.append(
                            "User pressed Stop while you were awaiting an answer. "
                            "Exit cleanly on the next turn with action=done."
                        )
                        continue
                    if answered:
                        pending_feedback.append(
                            f"[USER ANSWER TO `{question}`]\n{q.answer or '(empty)'}"
                        )
                    else:
                        pending_feedback.append(
                            f"[ask TIMED OUT after {ask_timeout}s]\n"
                            "The user did not respond. If the question was optional, "
                            "pick a sensible default and proceed. If the question was "
                            "blocking (e.g. a passphrase), post a `message` explaining "
                            "you need a reply and ask again once they're active."
                        )
                    continue

                if act == "ssh_exec":
                    cmd = action.get("command", "")
                    reason = action.get("reason", "")
                    if not cmd:
                        pending_feedback.append(
                            "Error: empty command. Provide a valid shell command."
                        )
                        continue

                    # Stall detector: if 4 out of the last 5 commands
                    # share a topic keyword, we inject a pause-and-
                    # reconsider nudge. Motivated by the live run where
                    # the model spent 10 turns rebuilding pacman.conf
                    # around warnings that weren't blocking anything.
                    recent_reasons.append(reason or cmd)
                    if len(recent_reasons) > 5:
                        recent_reasons.pop(0)
                    recent_commands.append(cmd)
                    if len(recent_commands) > 6:
                        recent_commands.pop(0)

                    # Flag-escalation detector: catches the pattern where
                    # the agent issues 3+ variants of the same install/
                    # remove intent with progressively more destructive
                    # flags (--noconfirm → --nodeps → -Rdd → piped `y`).
                    # Normalise to an intent signature so sibling variants
                    # collapse to the same key, then check for 3+ repeats.
                    _esc_sig = _detect_flag_escalation(recent_commands)
                    if _esc_sig is not None:
                        escalation_msg = (
                            f"[harness] FLAG-ESCALATION LOOP DETECTED — your last "
                            f"3+ commands all share the intent `{_esc_sig}`, each "
                            f"time with more destructive flags. This pattern is a "
                            f"signature of fighting the symptom instead of the "
                            f"cause. STOP and reconsider: (a) do you actually need "
                            f"this exact package set? Can you drop one and unblock "
                            f"yourself? (b) is the conflict message pointing at a "
                            f"version of a dep you already have (e.g. nodejs) that "
                            f"would satisfy everything if you just used the DEFAULT "
                            f"package instead of a variant? (c) if you truly need "
                            f"user input, emit an `ask` action — don't keep "
                            f"piling force flags. Next command MUST be different "
                            f"in SUBSTANCE (different packages or lookup action), "
                            f"not just flags."
                        )
                        bridge.add_message("agent", escalation_msg)
                        pending_feedback.append(escalation_msg)
                        # Reset so we don't fire on every subsequent turn
                        # — the model gets ONE loud warning then normal
                        # operation resumes.
                        recent_commands.clear()
                    _topic = _detect_stall_topic(recent_reasons)
                    if _topic is not None:
                        nudge = (
                            f"STALL DETECTED — you've spent the last 4+ turns "
                            f"investigating '{_topic}'. Stop and check: (a) is this "
                            f"actually blocking the migration goal, or is it a "
                            f"non-fatal warning you can ignore? (b) if it IS "
                            f"blocking, what is the CONCRETE fix, not another "
                            f"probe? If you cannot name the fix in one command, "
                            f"skip this topic and continue with the original "
                            f"plan. Re-read the MIGRATION PLAN section above."
                        )
                        bridge.add_message("agent", f"[harness] {nudge}")
                        pending_feedback.append(nudge)
                        # Reset the window so the nudge isn't re-fired on
                        # every subsequent turn of the same topic.
                        recent_reasons.clear()

                    # === Sticky-block + pivot detector ===
                    # When a pre-reboot block is active, the agent MUST
                    # address the specific failures. Score each command
                    # against the failure topic words; commands that
                    # mention none of them count as "pivots". Three
                    # pivots in a row and we escalate to /api/ask so the
                    # user can steer (e.g. "skip encryption", "set root
                    # password manually, here's what it is").
                    if sticky_block_failures and not _is_reboot_command(cmd):
                        if _command_addresses_topics(cmd, sticky_block_topics):
                            pivot_counter = 0  # agent is on topic — reset
                        elif _is_safe_diagnostic_setup_command(cmd):
                            pivot_counter += 1
                            bridge.add_message(
                                "agent",
                                f"[harness] ⚠️ Pivot detected ({pivot_counter}/3). "
                                f"Current reboot block is still active on: "
                                f"{', '.join(sorted(sticky_block_topics)) or '(unknown)'}. "
                                "This diagnostic command is being allowed because it is read-only, "
                                "but the agent should stay focused on clearing the reboot blockers.",
                            )
                            pending_feedback.append(
                                "REMINDER: a pre-reboot block is still in effect. Your last "
                                "command was allowed because it is read-only, but it still did not "
                                "touch the blocked topics "
                                f"({', '.join(sorted(sticky_block_topics)) or '?'}). "
                                "Use the results only to fix the reboot blockers — do NOT pivot to "
                                "unrelated installs or theming."
                            )
                            if pivot_counter >= 3:
                                import requests as _requests

                                try:
                                    _requests.post(
                                        f"{base}/api/ask",
                                        json={
                                            "question": (
                                                "The pre-reboot safety guard is still blocking on: "
                                                + f"{', '.join(sorted(sticky_block_topics))}. "
                                                + "The agent keeps spending turns on read-only diagnostics "
                                                + "instead of fixing the blockers. How should it proceed?"
                                            ),
                                            "timeout": 600,
                                        },
                                        timeout=5,
                                    )
                                except Exception:
                                    pass
                                pivot_counter = 0
                        else:
                            pivot_counter += 1
                            block_topics = ", ".join(sorted(sticky_block_topics)) or "(unknown)"
                            block_hint = (
                                f"A pre-reboot block is still active on: {block_topics}. "
                                "This command does not address any blocked topic. Fix the reboot "
                                "blockers first, then continue with drivers, desktop, apps, or theming."
                            )
                            bridge.add_message(
                                "agent",
                                f"[harness] 🛑 Command blocked: `{cmd}`\n{block_hint}",
                            )
                            bridge.add_message(
                                "agent",
                                f"[harness] ⚠️ Pivot detected ({pivot_counter}/3). "
                                f"Current reboot block is still active on: {block_topics}. "
                                "Addressing one of those is higher priority than this command.",
                            )
                            pending_feedback.append(
                                "REMINDER: a pre-reboot block is still in effect. Your last "
                                "command did not touch any of the blocked topics "
                                f"({block_topics}). Keep drilling "
                                "into the failures listed above — do NOT pivot to unrelated "
                                "work (apps, AUR, theming, etc.) until the reboot is unblocked."
                            )
                            results_log.append(f"**`{cmd}`** — BLOCKED (sticky pre-reboot block)")
                            if pivot_counter >= 3:
                                # Hand control to the user.
                                import requests as _requests

                                try:
                                    _requests.post(
                                        f"{base}/api/ask",
                                        json={
                                            "question": (
                                                "The pre-reboot safety guard has blocked the reboot "
                                                + f"on: {', '.join(sorted(sticky_block_topics))}. "
                                                + "The agent has tried 3+ commands that don't address "
                                                + "the blockers. How should it proceed? "
                                                + "(e.g. 'set root password NOPASSWD', "
                                                + "'skip encryption check', 'I'll fix it manually, continue')"
                                            ),
                                            "timeout": 600,
                                        },
                                        timeout=5,
                                    )
                                except Exception:
                                    pass
                                pivot_counter = 0  # reset so we don't re-ask every turn
                            continue

                    lint_error = _preflight_setup_command(cmd, setup_facts)
                    if lint_error:
                        bridge.add_message(
                            "agent",
                            f"[harness] 🛑 Command blocked: `{cmd}`\n{lint_error}",
                        )
                        pending_feedback.append(
                            "[quickhatch preflight]\n"
                            + lint_error
                            + "\nSearch for the exact package name first, then issue a corrected command."
                        )
                        results_log.append(f"**`{cmd}`** — BLOCKED (preflight)")
                        continue

                    bridge.add_message("agent", f"`{cmd}`" + (f" — {reason}" if reason else ""))

                    # Reboot commands tear down the live-env SSH session.
                    # Fire the command (short timeout — we expect it to
                    # disconnect us), then poll for SSH on the installed
                    # system to come back up. If the agent created a user
                    # during chroot (common: the live ISO has no root SSH
                    # after the first real boot), we swap to that user
                    # before reconnect — otherwise SSH keeps trying the
                    # live-ISO's root, which doesn't exist on-disk.
                    if _is_reboot_command(cmd):
                        reconnect_user = created_user or user
                        # GUARD: before we let the agent reboot, run a
                        # battery of "is the freshly-installed system
                        # actually bootable?" checks against /mnt. If
                        # any FAIL, refuse to reboot and feed the
                        # specific failures back so the agent fixes
                        # them on the next iteration. This catches the
                        # whole class of "agent thinks done, system
                        # boots into emergency mode" failures (missing
                        # encrypt hook, locked root, stale grub.cfg,
                        # etc.).
                        if is_live:
                            bridge.add_message(
                                "agent",
                                "Pre-reboot safety checks running…",
                            )
                            failures, oks = _run_pre_reboot_checks(
                                host, user, key_path, port=ssh_port
                            )
                            for line in oks:
                                bridge.add_message("agent", f"  ✓ {line}")
                            if failures:
                                # Latch onto sticky state so we re-inject
                                # the failure list on EVERY subsequent
                                # turn until cleared. Without this, the
                                # failures scroll out of the model's
                                # attention and it pivots to unrelated
                                # work (apps, AUR, etc.) instead of
                                # drilling in on the actual blockers.
                                sticky_block_failures = list(failures)
                                sticky_block_topics = _extract_topic_words(failures)
                                pivot_counter = 0
                                msg = (
                                    "REBOOT BLOCKED — pre-reboot checks failed. Fix these "
                                    "BEFORE attempting another reboot:\n\n"
                                    + "\n".join(f"  ✗ {f}" for f in failures)
                                    + "\n\nDo NOT issue another reboot command until every check above passes. "
                                    "These checks specifically prevent the 'system boots into emergency mode "
                                    "with locked root account' class of failure. If you cannot make progress "
                                    "on a specific failure after 2 tries, use /api/ask to ask the user — "
                                    "DO NOT pivot to unrelated work (apps, AUR, theming) while these remain."
                                )
                                for f in failures:
                                    bridge.add_message("agent", f"  ✗ {f}")
                                bridge.add_message(
                                    "agent",
                                    "🛑 Reboot blocked — agent must fix the failures above before retrying.",
                                )
                                pending_feedback.append(msg)
                                results_log.append(
                                    f"**`{cmd}`** — BLOCKED ({len(failures)} pre-reboot fail)"
                                )
                                continue  # skip the reboot, agent gets the feedback
                            bridge.add_message(
                                "agent",
                                "✓ All pre-reboot checks passed — proceeding with reboot.",
                            )
                            # Clear sticky state on success.
                            sticky_block_failures = []
                            sticky_block_topics = set()
                            pivot_counter = 0
                            # === LUKS-at-reboot user prompt ===
                            # If LUKS is in play, the initramfs will prompt
                            # for the passphrase on the TARGET machine's
                            # local console. The agent cannot answer over
                            # SSH (no network in initramfs). Tell the
                            # user loudly — the frontend looks for the
                            # [ATTENTION] marker and fires sound + flash
                            # + native notification so the user gets
                            # pulled out of whatever they're doing.
                            luks_detected = any("luks-present" in line for line in oks)
                            if luks_detected:
                                bridge.add_message(
                                    "agent",
                                    "[ATTENTION] LUKS passphrase needed — go to the target "
                                    "machine's screen NOW and type the passphrase you set "
                                    "during install. The initramfs prompt is on the local "
                                    "console (no network yet, agent can't answer it). "
                                    "QuickHatch will auto-reconnect as soon as the "
                                    "installed system boots.",
                                )
                        # IMPORTANT: fire the reboot command itself as the
                        # CURRENT live-ISO user — the new user only exists
                        # inside the installed chroot and has no shell on
                        # the running live session. Switching before the
                        # reboot would hang the install. We persist the
                        # user switch only AFTER the reboot is fired.
                        bridge.add_message(
                            "agent",
                            "Reboot issued — polling for the target to come back "
                            "(no timeout, press Stop to abort)…",
                        )
                        _ssh_exec(
                            host, user, cmd, key_path, timeout=15, port=ssh_port
                        )  # fire + disconnect (as live-ISO user)
                        if reconnect_user != user:
                            bridge.add_message(
                                "agent",
                                f"Switching SSH user to '{reconnect_user}' for post-reboot "
                                f"reconnect (agent created this account during chroot; root "
                                f"has no SSH access on the installed system).",
                            )
                            # Persist to profile so /wizard/test-ssh + any
                            # subsequent agent launch also uses the new user.
                            profile.linux_user = reconnect_user
                            try:
                                profile.save_checkpoint()
                            except Exception:
                                pass
                            user = reconnect_user
                        # Poll forever until SSH comes back OR the user
                        # presses Stop. Earlier 5-minute deadline was
                        # shorter than the time needed to walk to the
                        # target, type a LUKS passphrase, and walk back.
                        # The UI now flashes/beeps when input is needed;
                        # the Stop button is the explicit escape hatch.
                        reconnected = _wait_for_ssh_reconnect(
                            host,
                            user,
                            key_path,
                            port=ssh_port,
                            timeout=None,
                            interval=5,
                            stop_flag_getter=lambda: bridge.stop_requested,
                        )
                        if not reconnected:
                            # Be specific about the most likely causes so
                            # the user knows where to look on their console.
                            bridge.add_message(
                                "agent",
                                "❌ Timed out waiting for the installed system's SSH to come back.\n\n"
                                "Most likely causes (check the target machine's screen):\n"
                                "  1. **Initramfs dropped to emergency mode** — usually means the "
                                "encrypt hook is missing from /etc/mkinitcpio.conf, or the kernel "
                                "cmdline doesn't reference the LUKS container. The pre-reboot "
                                "guard should have caught this — please report if it didn't.\n"
                                "  2. **No sshd on first boot** — `systemctl enable sshd` was "
                                "never run inside chroot, or NetworkManager isn't enabled so "
                                "the system boots without an IP.\n"
                                "  3. **Bootloader didn't install** — system stops at the BIOS/"
                                "UEFI 'no bootable device' screen.\n"
                                "  4. **Wrong reconnect user** — the agent created a user but "
                                "we still SSH as the live-ISO root user.\n\n"
                                "To recover: boot from the Arch USB again, mount the install, "
                                "chroot, and fix whatever the screen shows. The QuickHatch "
                                "primer has the recovery procedure under 'Post-install gotchas'.",
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
                        # Auto-grant NOPASSWD sudo to the reconnect user
                        # using the known default password. Without
                        # this, every subsequent `sudo` command hangs on
                        # an invisible password prompt (SSH has no pty).
                        # The agent is instructed in the system prompt
                        # to set both root and user passwords to the
                        # literal profile.default_account_password
                        # during install, so this pipe-and-tee is safe.
                        default_pw = profile.default_account_password or "password"
                        nopasswd_cmd = (
                            f"echo '{default_pw}' | sudo -S -p '' bash -c "
                            f"\"echo '{user} ALL=(ALL:ALL) NOPASSWD: ALL' > "
                            f"/etc/sudoers.d/99-quickhatch-nopasswd && "
                            f'chmod 440 /etc/sudoers.d/99-quickhatch-nopasswd"'
                        )
                        _nrc, _nout, _nerr = _ssh_exec(
                            host, user, nopasswd_cmd, key_path, port=ssh_port, timeout=30
                        )
                        if _nrc == 0:
                            bridge.add_message(
                                "agent",
                                "✓ Granted NOPASSWD sudo to "
                                f"'{user}' via /etc/sudoers.d/99-quickhatch-nopasswd "
                                "using the default account password. Subsequent sudo "
                                "commands will not prompt.",
                            )
                        else:
                            bridge.add_message(
                                "agent",
                                "⚠️ Could not auto-grant NOPASSWD sudo (default password "
                                "may not have been set during install). `sudo` commands "
                                "will hang on invisible prompts until the agent grants "
                                "passwordless sudo manually. Remember to pipe the correct "
                                "password if prompting.",
                            )
                        # Gather fresh system info so the next turn sees
                        # the installed-system reality.
                        refreshed_setup_facts = _probe_setup_facts(
                            host,
                            user,
                            key_path,
                            port=ssh_port,
                            fallback=setup_facts,
                        )
                        fact_update = _describe_setup_fact_changes(
                            setup_facts, refreshed_setup_facts, is_live=is_live
                        )
                        setup_facts = refreshed_setup_facts
                        fresh_rc, fresh_info, _ = _ssh_exec(
                            host,
                            user,
                            "cat /etc/os-release 2>/dev/null; echo '===USER==='; whoami; id",
                            key_path,
                            port=ssh_port,
                        )
                        if fresh_rc == 0:
                            refresh_bits = [f"[post-reboot system info]\n{fresh_info.strip()}"]
                            if fact_update:
                                refresh_bits.append(fact_update)
                            refresh_bits.append(
                                f"[harness] Passwordless sudo has been granted for '{user}'. "
                                f"Run any sudo command freely — no prompt will appear. "
                                f"REMINDER: include a 'CHANGE YOUR PASSWORD IMMEDIATELY' "
                                f"block in your final summary so the user rotates the "
                                f"default password ('{default_pw}') on first login."
                            )
                            pending_feedback.append("\n\n".join(refresh_bits))
                        results_log.append(f"**`{cmd}`** — OK (reconnected)")
                        continue

                    cmd_timeout = _resolve_setup_cmd_timeout(cmd, action)
                    rc, out, err = _ssh_exec(
                        host, user, cmd, key_path, timeout=cmd_timeout, port=ssh_port
                    )

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

                    # Track the exit history so we can catch the agent
                    # looping on a failing command and inject a hint.
                    recent_exits.append((cmd, rc))
                    if len(recent_exits) > 10:
                        recent_exits[:] = recent_exits[-10:]
                    if rc != 0:
                        lookup_hint = _lookup_hint_for_failed_command(
                            cmd, (out or "") + "\n" + (err or ""), setup_facts
                        )
                        if lookup_hint:
                            pending_feedback.append(f"[quickhatch lookup hint]\n{lookup_hint}")
                            bridge.add_message("agent", f"💡 Lookup hint: {lookup_hint[:240]}")
                        hint = _advise_on_repeated_failure(
                            cmd, (out or "") + "\n" + (err or ""), recent_exits
                        )
                        if hint:
                            # Append to pending_feedback so it goes to the
                            # model in the next turn — not just UI chat.
                            pending_feedback.append(
                                f"[quickhatch hint — the framework noticed this "
                                f"looks familiar]\n{hint}"
                            )
                            bridge.add_message("agent", f"💡 Hint: {hint[:240]}")

                    # Snoop for `useradd ... NAME` so we know who to log
                    # in as after the post-install reboot. See HIGH
                    # finding: "setup agent creates user but profile
                    # stays on root, reconnect fails."
                    if rc == 0:
                        maybe_user = _extract_created_user(cmd)
                        if maybe_user:
                            created_user = maybe_user
                            bridge.add_message(
                                "agent",
                                f"Noted: user '{maybe_user}' created. Will switch SSH to "
                                f"this account after reboot.",
                            )
                        refreshed_setup_facts = _probe_setup_facts(
                            host,
                            user,
                            key_path,
                            port=ssh_port,
                            fallback=setup_facts,
                        )
                        fact_update = _describe_setup_fact_changes(
                            setup_facts, refreshed_setup_facts, is_live=is_live
                        )
                        setup_facts = refreshed_setup_facts
                        if fact_update:
                            pending_feedback.append(fact_update)
                    # Trim to avoid unbounded growth across a long install
                    if len(results_log) > _RESULTS_LOG_MAX:
                        results_log[:] = results_log[-_RESULTS_LOG_MAX:]

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

                pending_feedback.append(
                    f"Unknown action '{act}'. Use ssh_exec, repo_search, aur_search, web_search, ask, status, or done."
                )

            if finished:
                return

            # Pull any user chat messages typed since the last iteration.
            # These come from the UI's chat box (/wizard/chat → bridge.add_message).
            # Prepend them to the feedback with a loud marker so the agent
            # actually notices, reconsiders the plan, and replies in-chat.
            user_interjections: list[str] = []
            with bridge.lock:
                new_tail = bridge.messages[_last_bridge_msg_idx:]
                _last_bridge_msg_idx = len(bridge.messages)
            for m in new_tail:
                if m.get("role") == "user":
                    txt = (m.get("content") or "").strip()
                    if txt:
                        user_interjections.append(txt)

            # Send consolidated feedback back to the model. If a
            # pre-reboot block is still active, ALWAYS prepend the
            # sticky failure list — this keeps the blockers in the
            # model's immediate attention instead of letting them
            # scroll out of the context window behind command output.
            feedback_parts: list[str] = []
            if user_interjections:
                feedback_parts.append(
                    "[USER INTERJECTION — HIGHEST PRIORITY]\n"
                    "The user just typed the following in the chat. Treat this "
                    "as a direct instruction from the user and re-evaluate your "
                    "current plan before running another command. Acknowledge "
                    "what they said by posting a short reply via the "
                    "'message' action, then adapt. If what they ask is "
                    "impossible at this stage (e.g. 'undo encryption' after "
                    "LUKS is already formatted), say so plainly and explain "
                    "what their options are.\n\n"
                    + "\n\n".join(f"> {t}" for t in user_interjections)
                )
            if sticky_block_failures:
                feedback_parts.append(
                    "[STICKY — PRE-REBOOT BLOCK ACTIVE]\n"
                    "These invariants are still failing and must be fixed before reboot:\n"
                    + "\n".join(f"  ✗ {f}" for f in sticky_block_failures)
                    + "\n"
                    "Focus ONLY on resolving these. Any command that doesn't touch one of "
                    "the blocked topics is a pivot and will be flagged."
                )
            feedback_parts.extend(pending_feedback)
            if feedback_parts:
                messages.append({"role": "user", "content": "\n\n".join(feedback_parts)})
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

    from quickhatch.wizard.ssh import _find_ssh_keygen

    key_path = Path.home() / ".ssh" / "quickhatch_ed25519"
    pub_path = key_path.with_suffix(".pub")

    if pub_path.exists() and not key_path.exists():
        try:
            pub_path.unlink()
        except OSError:
            return {
                "status": "error",
                "message": (
                    f"Saved public key exists but the private key is missing: {key_path}. "
                    "Delete the public key or choose a valid keypair."
                ),
            }

    if pub_path.exists():
        # MED-8: if the private key exists, verify it's 0600 — a world-
        # readable key means ssh will refuse to use it and the whole
        # remote-setup flow silently fails with "bad permissions".
        try:
            if key_path.exists():
                import stat as _stat

                mode = key_path.stat().st_mode & 0o777
                if mode & 0o077:
                    try:
                        key_path.chmod(0o600)
                    except OSError:
                        pass
                del _stat
        except OSError:
            pass
        gui_state.profile.ssh_public_key_path = str(pub_path)
        gui_state.profile.save_checkpoint()
        return {
            "status": "ok",
            "message": "SSH key already exists",
            # Explicit utf-8 — Windows default cp1252 chokes on any
            # non-ASCII content that ended up in the comment field.
            "public_key": pub_path.read_text(encoding="utf-8").strip(),
            "path": str(pub_path),
        }

    try:
        ssh_keygen = _find_ssh_keygen()
        if not ssh_keygen:
            raise FileNotFoundError("ssh-keygen not found")
        key_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ssh_keygen, "-t", "ed25519", "-C", "quickhatch", "-f", str(key_path), "-N", ""],
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
            "public_key": pub_path.read_text(encoding="utf-8").strip(),
            "path": str(pub_path),
        }
    except FileNotFoundError:
        return {"status": "error", "message": "ssh-keygen not found. Install OpenSSH first."}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"ssh-keygen failed: {e.stderr or e.stdout}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _locate_install_script(gui_state: GUIState) -> Path | None:
    """Find the install.sh to verify — exported bundle first, then suggestion.

    Checks the canonical export dir (`_export_output_dir()`) — same
    location the export step actually writes to. Previously this looked
    in `~/quickhatch-export/`, which the export code never wrote to —
    so the VM-verify path always fell through to the temp-file
    fallback even when a real bundle existed.
    """
    export_dir = _export_output_dir()
    if (export_dir / "install.sh").exists():
        return export_dir / "install.sh"
    # Legacy location, kept as a fallback for users who exported with
    # an older QuickHatch and still have the bundle in the old path.
    legacy = Path.home() / "quickhatch-export"
    if (legacy / "install.sh").exists():
        return legacy / "install.sh"
    for s in gui_state.bridge.snapshot_suggestions():
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
        # canonical export dir (same path _run_export writes to).
        export_dir = _export_output_dir()
        if (export_dir / "install.sh").exists():
            install_script = export_dir / "install.sh"
        else:
            legacy = Path.home() / "quickhatch-export"
            if (legacy / "install.sh").exists():
                install_script = legacy / "install.sh"
        if install_script is None:
            # Fallback: write the suggestion body to a temp file.
            for s in bridge.snapshot_suggestions():
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


def _write_unix_text(path: Path, text: str) -> None:
    """Write a text file with LF-only newlines + UTF-8.

    Use for any shell script we ship in the bundle: Path.write_text on
    Windows defaults to platform line endings (CRLF), which makes the
    Linux kernel parse the shebang as `#!/usr/bin/env bash\\r` and
    fail with `bash\\r: no such file or directory`.
    """
    path.write_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def _export_output_dir() -> Path:
    """Where to write the migration bundle.

    Prefer ~/Downloads/QuickHatch-Migration so the bundle lands where
    users already look for downloaded files (and where most file
    explorers default when copying to a USB stick). Fall back to
    ~/QuickHatch-Migration if Downloads doesn't exist (uncommon Linux
    setups, customized $HOME).
    """
    downloads = Path.home() / "Downloads"
    if downloads.exists() and downloads.is_dir():
        return downloads / "QuickHatch-Migration"
    return Path.home() / "QuickHatch-Migration"


def _run_export(gui_state: GUIState) -> dict:
    """Run the full export: instructions, profile, SSH key, quickstart, zip."""
    import zipfile
    from dataclasses import asdict

    import yaml

    from quickhatch.ai.instruction_gen import save_instructions
    from quickhatch.wizard.export import (
        GENERATED_EXPORT_FILES,
        _generate_quickstart,
        generate_start_here,
    )

    output_dir = _export_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []

    try:
        key_status = _ssh_key_status(gui_state.profile)
        if gui_state.profile.ssh_public_key_path and not key_status["ready"]:
            return {
                "status": "error",
                "message": (
                    f"{key_status['message']} The bundle was not exported because "
                    "quickstart.sh would not be able to install a key that this wizard can use. "
                    "Generate a new SSH key or choose a valid public key with its matching private key."
                ),
                "ssh_key_status": key_status,
            }

        for name in GENERATED_EXPORT_FILES:
            generated = output_dir / name
            if generated.exists():
                generated.unlink()
        for stale_pub in output_dir.glob("*.pub"):
            if stale_pub.is_file():
                stale_pub.unlink()

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

        # Offline install script (present only when the user said they
        # don't have a second machine — analysis phase generated it as
        # the install_script section).
        install_script_content = ""
        for s in gui_state.bridge.snapshot_suggestions():
            if getattr(s, "category", "") == "install_script" and (s.content or "").strip():
                install_script_content = s.content.strip()
                break

        start_here_path = output_dir / "START_HERE.md"
        start_here_path.write_text(
            generate_start_here(
                gui_state.profile,
                include_install_script=bool(install_script_content),
            ),
            encoding="utf-8",
        )
        files.append(start_here_path.name)

        # SSH public key
        if gui_state.profile.ssh_public_key_path:
            ssh_pub = Path(gui_state.profile.ssh_public_key_path)
            dest = output_dir / ssh_pub.name
            dest.write_text(ssh_pub.read_text(encoding="utf-8"), encoding="utf-8")
            files.append(dest.name)

        # Quickstart script — write with explicit LF line endings so
        # the Linux kernel can parse the shebang on Windows-exported
        # bundles. Default Path.write_text() on Windows writes \r\n,
        # which makes /usr/bin/env look for a binary named "bash\r".
        qs_path = output_dir / "quickstart.sh"
        _write_unix_text(qs_path, _generate_quickstart(gui_state.profile))
        files.append(qs_path.name)
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
            # Same LF rule as quickstart.sh — the live ISO's kernel
            # rejects shebangs with CR.
            _write_unix_text(install_path, install_script_content)
            try:
                install_path.chmod(0o755)
            except OSError:
                pass  # Windows
            files.append(install_path.name)

        # ZIP bundle — skip dotfiles (notably .api_key, which is saved
        # to the export dir for local use but must not ship in the zip).
        zip_path = output_dir / "quickhatch-migration-bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in output_dir.iterdir():
                if file.name != zip_path.name and not file.name.startswith(".") and file.is_file():
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
