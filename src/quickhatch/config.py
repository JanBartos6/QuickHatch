"""Central data model for QuickHatch migration wizard."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class AppInfo:
    """Information about an installed application."""
    name: str
    version: str | None = None
    source: str = "unknown"  # "registry", "winget", "store", "wmi"
    linux_alternative: str | None = None
    compatibility: str = "unknown"  # "native", "alternative", "wine", "none"
    compatibility_notes: str = ""


@dataclass
class GPUInfo:
    """GPU hardware information."""
    name: str
    vendor: str = "unknown"  # "nvidia", "amd", "intel"
    vram_mb: int = 0
    driver_version: str = ""


@dataclass
class DiskInfo:
    """Disk information."""
    device: str
    mountpoint: str
    filesystem: str
    total_gb: float
    used_gb: float
    free_gb: float


@dataclass
class HardwareInfo:
    """System hardware information."""
    cpu: str = "unknown"
    cpu_cores: int = 0
    ram_gb: float = 0.0
    gpus: list[GPUInfo] = field(default_factory=list)
    wifi_adapters: list[str] = field(default_factory=list)
    bluetooth: bool = False
    disks: list[DiskInfo] = field(default_factory=list)
    efi: bool = True
    motherboard: str = ""


@dataclass
class Preferences:
    """User preferences for the migration."""
    migration_type: str = "fresh_install"  # "fresh_install" or "switching"
    desktop_look: str = ""  # "windows_like", "macos_like", "minimal", "tiling", "custom"
    icon_style: str = ""  # "modern_flat", "classic", "colorful", "monochrome"
    wallpaper_style: str = ""  # "dark", "light", "nature", "abstract", "minimal"
    use_cases: list[str] = field(default_factory=list)
    distro_preference: str | None = None
    additional_notes: str = ""


@dataclass
class AIConfig:
    """AI assistant configuration."""
    provider: str = ""  # "claude_code", "codex", "local_ollama", "local_lmstudio", "api"
    model_name: str | None = None
    api_key: str | None = None
    endpoint: str | None = None


@dataclass
class UserProfile:
    """Complete user profile aggregating all migration data."""
    preferences: Preferences = field(default_factory=Preferences)
    ai_config: AIConfig = field(default_factory=AIConfig)
    hardware: HardwareInfo = field(default_factory=HardwareInfo)
    apps: list[AppInfo] = field(default_factory=list)
    files_summary: dict = field(default_factory=dict)
    windows_settings: dict = field(default_factory=dict)
    recommended_distro: str | None = None
    recommended_de: str | None = None
    ssh_public_key_path: str | None = None
    usb_device: str | None = None
    completed_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize the profile to a dictionary."""
        return asdict(self)

    def save_checkpoint(self, path: Path | None = None):
        """Save current state as a checkpoint."""
        if path is None:
            path = Path(".quickhatch-checkpoint.json")
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))

    @classmethod
    def load_checkpoint(cls, path: Path | None = None) -> UserProfile | None:
        """Load a checkpoint if one exists."""
        if path is None:
            path = Path(".quickhatch-checkpoint.json")
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            profile = cls()
            profile.preferences = Preferences(**data.get("preferences", {}))
            profile.ai_config = AIConfig(**data.get("ai_config", {}))

            hw = data.get("hardware", {})
            hw["gpus"] = [GPUInfo(**g) for g in hw.get("gpus", [])]
            hw["disks"] = [DiskInfo(**d) for d in hw.get("disks", [])]
            profile.hardware = HardwareInfo(**hw)

            profile.apps = [AppInfo(**a) for a in data.get("apps", [])]
            profile.files_summary = data.get("files_summary", {})
            profile.windows_settings = data.get("windows_settings", {})
            profile.recommended_distro = data.get("recommended_distro")
            profile.recommended_de = data.get("recommended_de")
            profile.ssh_public_key_path = data.get("ssh_public_key_path")
            profile.usb_device = data.get("usb_device")
            profile.completed_steps = data.get("completed_steps", [])
            return profile
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
