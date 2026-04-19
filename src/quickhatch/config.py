"""Central data model for the migration wizard."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


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
class HardwareInfo:
    """Detected hardware information."""

    cpu: str = ""
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpus: list[GPUInfo] = field(default_factory=list)
    wifi_adapters: list[str] = field(default_factory=list)
    bluetooth: bool = False
    disks: list[DiskInfo] = field(default_factory=list)
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
    completed_steps: list[str] = field(default_factory=list)

    def save_checkpoint(self, path: Path | None = None) -> Path:
        """Save current state to a checkpoint file."""
        if path is None:
            path = Path(".quickhatch-checkpoint.json")
        data = asdict(self)
        # Redact sensitive data from checkpoint
        if data.get("ai_config", {}).get("api_key"):
            data["ai_config"]["api_key"] = "***REDACTED***"
        path.write_text(json.dumps(data, indent=2))
        return path

    @classmethod
    def load_checkpoint(cls, path: Path | None = None) -> UserProfile | None:
        """Load state from a checkpoint file, or return None if not found."""
        if path is None:
            path = Path(".quickhatch-checkpoint.json")
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
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

        try:
            hw_raw = data.get("hardware", {}) or {}
            hw = _safe_init(
                HardwareInfo,
                {k: v for k, v in hw_raw.items() if k not in ("gpus", "disks")},
            )
            hw.gpus = [g for g in (_safe_init(GPUInfo, g) for g in hw_raw.get("gpus", [])) if g]
            hw.disks = [d for d in (_safe_init(DiskInfo, d) for d in hw_raw.get("disks", [])) if d]

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
                completed_steps=list(data.get("completed_steps", []) or []),
            )
        except Exception as exc:
            print(
                f"[quickhatch] checkpoint load failed ({exc}). Starting with a fresh profile.",
                file=_sys.stderr,
            )
            return None
