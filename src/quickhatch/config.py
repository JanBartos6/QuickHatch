"""Central data model for the migration wizard."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
            return cls(
                preferences=Preferences(**data.get("preferences", {})),
                ai_config=AIConfig(**data.get("ai_config", {})),
                hardware=HardwareInfo(
                    **{
                        k: v
                        for k, v in data.get("hardware", {}).items()
                        if k not in ("gpus", "disks")
                    },
                    gpus=[GPUInfo(**g) for g in data.get("hardware", {}).get("gpus", [])],
                    disks=[DiskInfo(**d) for d in data.get("hardware", {}).get("disks", [])],
                ),
                apps=[AppInfo(**a) for a in data.get("apps", [])],
                files_summary=data.get("files_summary", {}),
                windows_settings=data.get("windows_settings", {}),
                ssh_public_key_path=data.get("ssh_public_key_path"),
                additional_info=data.get("additional_info", ""),
                apps_to_install=data.get("apps_to_install", []),
                data_description=data.get("data_description", ""),
                data_transfer_method=data.get("data_transfer_method", ""),
                linux_ip=data.get("linux_ip", ""),
                linux_user=data.get("linux_user", ""),
                completed_steps=data.get("completed_steps", []),
            )
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
