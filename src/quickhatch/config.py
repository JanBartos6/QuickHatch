"""Central data model for the migration wizard."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _checkpoint_path() -> Path:
    """Absolute default checkpoint path under the user's home directory.

    Placed alongside ``~/.quickhatch-suggestions.json`` so both pieces of
    persistent wizard state follow the same convention.
    """
    return Path.home() / ".quickhatch-checkpoint.json"


def _migrate_legacy_checkpoint(new_path: Path) -> None:
    """One-shot migration from the old CWD-relative checkpoint location.

    Previously the checkpoint lived at ``./.quickhatch-checkpoint.json``
    relative to whatever directory QuickHatch was launched from. That
    meant users who relaunched from a different cwd silently lost their
    wizard progress. Detect the legacy file on first load and move it
    into ``~/.quickhatch-checkpoint.json``.

    Only runs when the NEW path doesn't exist — never overwrites a
    newer home-directory checkpoint with an older local one.
    """
    if new_path.exists():
        return
    legacy = Path(".quickhatch-checkpoint.json")
    if not legacy.exists():
        return
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(new_path)
    except OSError:
        pass  # best-effort — caller will treat as "no checkpoint"


@dataclass
class AppInfo:
    """Information about an installed Windows application."""

    name: str
    version: str | None = None
    source: str = "unknown"  # "registry", "winget", "store"
    linux_alternative: str | None = None
    compatibility: str = "unknown"  # "native", "alternative", "wine", "none"
    notes: str = ""


@dataclass
class GPUInfo:
    """GPU hardware information."""

    name: str
    vendor: str  # "nvidia", "amd", "intel"
    vram_mb: int | None = None
    driver_version: str | None = None


@dataclass
class DiskInfo:
    """Disk/partition information."""

    device: str
    mount_point: str
    filesystem: str
    total_gb: float
    used_gb: float
    free_gb: float


@dataclass
class DisplayInfo:
    """Display/monitor preferences captured from the source machine."""

    name: str
    width_px: int = 0
    height_px: int = 0
    refresh_hz: float | None = None
    scale_percent: int | None = None
    primary: bool = False
    source_platform: str = ""


@dataclass
class HardwareInfo:
    """Detected hardware information."""

    cpu: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpus: list[GPUInfo] = field(default_factory=list)
    wifi_adapters: list[str] = field(default_factory=list)
    bluetooth: bool = False
    disks: list[DiskInfo] = field(default_factory=list)
    displays: list[DisplayInfo] = field(default_factory=list)
    efi: bool = False


@dataclass
class Preferences:
    """User preferences for the Linux migration."""

    migration_type: str = ""  # "fresh_install" or "customize_linux"
    desktop_look: str = ""  # "windows_like", "macos_like", "minimal", "tiling", "custom"
    gallery_picks: list[str] = field(default_factory=list)
    icon_style: str = ""  # "flat", "rounded", "classic", "colorful"
    wallpaper_style: str = ""  # "nature", "abstract", "dark", "minimal", "custom"
    use_cases: list[str] = field(default_factory=list)
    distro_preference: str | None = None
    additional_notes: str = ""
    # Does the user have a second machine available during setup (to run
    # the QuickHatch agent while installing Linux on the target)?
    # "yes" / "no" / "" (unanswered). When "no", the analysis phase produces
    # a self-contained bash install script for the user to run offline on
    # the live USB — no agent needed during install, no second-machine
    # orchestration.
    another_machine: str = ""


@dataclass
class AIConfig:
    """AI assistant configuration."""

    provider: str = ""  # "codex", "local_ollama", "local_lmstudio", "api"
    model_name: str | None = None
    api_key: str | None = None
    endpoint: str | None = None
    native_search: bool = True


@dataclass
class UserProfile:
    """Complete user profile accumulated across all wizard steps."""

    preferences: Preferences = field(default_factory=Preferences)
    ai_config: AIConfig = field(default_factory=AIConfig)
    hardware: HardwareInfo = field(default_factory=HardwareInfo)
    apps: list[AppInfo] = field(default_factory=list)
    files_summary: dict = field(default_factory=dict)
    windows_settings: dict = field(default_factory=dict)
    ssh_public_key_path: str | None = None
    additional_info: str = ""
    apps_to_install: list[str] = field(default_factory=list)
    data_description: str = ""
    # How the user plans to physically move their Windows files to Linux.
    # One of: "external_drive", "cloud", "keep_windows", "nothing", "" (not yet answered).
    data_transfer_method: str = ""
    linux_ip: str = ""
    linux_user: str = ""
    # Remote setup intent for the last step of the wizard.
    # "auto" = current default behavior
    # "wipe_and_reinstall" = destructive reinstall is explicitly allowed,
    #   but only from a live environment after disk confirmation
    # "configure_only" = never repartition/reformat; just configure the
    #   current installed system
    remote_setup_mode: str = "auto"
    # Free-text description of the TARGET hardware when it differs from
    # the machine QuickHatch is running on. Empty string means "install
    # for this machine" (hardware scan is authoritative). Non-empty
    # means "install for a different machine; here's what's different"
    # — fed into the analysis + setup agent prompts so driver/CPU/GPU
    # decisions track the target, not the host. Users set this via a
    # button on the Scan step.
    target_hardware_override: str = ""
    # Default password for the root account AND the primary user
    # account created during install. Known constant so the agent can
    # pipe it to `sudo -S` during first post-reboot connect (no tty
    # over SSH — see common-pre-reboot-rules.md). MUST be changed by
    # the user on first login. The setup flow shows a loud "CHANGE
    # YOUR PASSWORD NOW" banner in the summary to drive that home.
    # Stored on the profile (not a constant) so a future UI can let
    # the user pick a different default if they want.
    default_account_password: str = "password"
    completed_steps: list[str] = field(default_factory=list)

    def save_checkpoint(self, path: Path | None = None) -> Path:
        """Save current state to a checkpoint file.

        Default path is ``~/.quickhatch-checkpoint.json`` — anchored to
        $HOME so the checkpoint survives a CWD change between runs
        (previously this was a bare relative path, so launching
        QuickHatch from a different directory silently lost state).
        Writes are atomic (tmp + os.replace) so a crash mid-write can't
        truncate the file.
        """
        if path is None:
            path = _checkpoint_path()
        data = asdict(self)
        # Previous versions redacted api_key to "***REDACTED***" in the
        # checkpoint. Sounded responsible, but it broke every
        # bridge-restart scenario: on reload, the redacted literal got
        # sent to the provider as a bogus API key, which then returned
        # HTTP 400 Missing or invalid Authorization header and the
        # whole analysis failed. Threat model: the checkpoint lives in
        # $HOME alongside ~/.ssh/ private keys — anyone who can read
        # one can read the other. Redacting here is security theatre.
        # Instead: write plaintext and chmod 600 so only the user can
        # read. Matches how ssh-keygen stores ed25519 private keys.
        serialized = json.dumps(data, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.exists() and path.read_text(encoding="utf-8") == serialized:
                    return path
            except OSError:
                pass
            tmp.write_text(serialized, encoding="utf-8")
            import os as _os

            _os.replace(tmp, path)
            # Restrict to owner-only (0600). On Windows chmod is a
            # best-effort no-op; there the NTFS ACLs inherited from
            # the user's home dir already scope visibility.
            try:
                _os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            # Best-effort — don't crash the wizard over a checkpoint write.
            pass
        return path

    @classmethod
    def load_checkpoint(cls, path: Path | None = None) -> UserProfile | None:
        """Load state from a checkpoint file, or return None if not found.

        Transparently migrates from the legacy CWD-relative path
        (``./.quickhatch-checkpoint.json``) to ``~/.quickhatch-checkpoint.json``
        on first read: if the HOME path is missing but a file exists in
        the current directory, move it there.
        """
        if path is None:
            path = _checkpoint_path()
            _migrate_legacy_checkpoint(path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupted JSON — move it aside so the user still has a copy
            # and we can start fresh next time. Surface a clear message.
            import sys as _sys

            backup = path.with_suffix(path.suffix + ".corrupt")
            try:
                path.rename(backup)
            except OSError:
                pass
            print(
                f"[quickhatch] checkpoint at {path} is unreadable ({exc}); "
                f"moved to {backup}. Starting with a fresh profile.",
                file=_sys.stderr,
            )
            return None

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict | None) -> UserProfile | None:
        """Schema-tolerant reconstruction from a JSON-like dict."""
        if not isinstance(data, dict):
            return None

        # Schema-tolerant reconstruction. If a dataclass field was renamed
        # or removed since the checkpoint was saved, skip UNKNOWN keys
        # (`__init__` would TypeError) and warn rather than silently
        # dropping the whole file. Each sub-dataclass restores what it
        # can; missing fields fall back to their default.
        import sys as _sys

        def _safe_init(cls_, kwargs):
            valid = {f.name for f in fields(cls_)}
            clean = {k: v for k, v in (kwargs or {}).items() if k in valid}
            dropped = set(kwargs or {}) - valid
            if dropped:
                print(
                    f"[quickhatch] checkpoint {cls_.__name__} had unknown keys "
                    f"{dropped} — ignoring those.",
                    file=_sys.stderr,
                )
            try:
                return cls_(**clean)
            except TypeError as exc:
                print(
                    f"[quickhatch] could not restore {cls_.__name__}: {exc}. "
                    f"Using defaults for this section.",
                    file=_sys.stderr,
                )
                # Some dataclasses (AppInfo, GPUInfo, DiskInfo) have
                # required positional fields — `cls_()` would re-raise
                # TypeError. Return None in that case; callers filter.
                try:
                    return cls_()
                except TypeError:
                    return None

        # save_checkpoint used to redact api_key to "***REDACTED***" and
        # the GUI response payload still uses "***SET***" as a non-secret
        # presence marker. Neither sentinel should ever become the real
        # runtime API key on restore.
        _ai = data.get("ai_config") or {}
        if isinstance(_ai, dict) and _ai.get("api_key") in {"***REDACTED***", "***SET***"}:
            _ai["api_key"] = None
            data["ai_config"] = _ai

        try:
            hw_raw = data.get("hardware", {}) or {}
            hw = (
                _safe_init(
                    HardwareInfo,
                    {k: v for k, v in hw_raw.items() if k not in ("gpus", "disks", "displays")},
                )
                or HardwareInfo()
            )
            hw.gpus = [g for g in (_safe_init(GPUInfo, g) for g in hw_raw.get("gpus", [])) if g]
            hw.disks = [d for d in (_safe_init(DiskInfo, d) for d in hw_raw.get("disks", [])) if d]
            hw.displays = [
                d for d in (_safe_init(DisplayInfo, d) for d in hw_raw.get("displays", [])) if d
            ]

            return cls(
                preferences=_safe_init(Preferences, data.get("preferences", {})) or Preferences(),
                ai_config=_safe_init(AIConfig, data.get("ai_config", {})) or AIConfig(),
                hardware=hw,
                apps=[a for a in (_safe_init(AppInfo, a) for a in data.get("apps", [])) if a],
                files_summary=data.get("files_summary", {}) or {},
                windows_settings=data.get("windows_settings", {}) or {},
                ssh_public_key_path=data.get("ssh_public_key_path"),
                additional_info=data.get("additional_info", "") or "",
                apps_to_install=list(data.get("apps_to_install", []) or []),
                data_description=data.get("data_description", "") or "",
                data_transfer_method=data.get("data_transfer_method", "") or "",
                linux_ip=data.get("linux_ip", "") or "",
                linux_user=data.get("linux_user", "") or "",
                remote_setup_mode=data.get("remote_setup_mode", "auto") or "auto",
                target_hardware_override=data.get("target_hardware_override", "") or "",
                default_account_password=data.get("default_account_password", "password")
                or "password",
                completed_steps=list(data.get("completed_steps", []) or []),
            )
        except Exception as exc:
            print(
                f"[quickhatch] profile reconstruction failed ({exc}). Starting with a fresh profile.",
                file=_sys.stderr,
            )
            return None
