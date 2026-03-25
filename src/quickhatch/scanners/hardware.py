"""Scan hardware information."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import psutil

from quickhatch.config import DiskInfo, GPUInfo, HardwareInfo
from quickhatch.console import print_info, print_warning


def _get_cpu() -> tuple[str, int]:
    """Get CPU name and core count."""
    cpu_name = platform.processor() or "Unknown CPU"

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "cpu", "get", "Name", "/value"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in (result.stdout or "").strip().split("\n"):
                if line.startswith("Name="):
                    cpu_name = line.split("=", 1)[1].strip()
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "linux":
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
            for line in cpuinfo.split("\n"):
                if line.startswith("model name"):
                    cpu_name = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass

    return cpu_name, psutil.cpu_count(logical=False) or psutil.cpu_count() or 1


def _get_ram() -> float:
    """Get total RAM in GB."""
    return round(psutil.virtual_memory().total / (1024**3), 1)


def _vendor_from_name(name: str) -> str:
    """Determine GPU vendor from device name."""
    lower = name.lower()
    if "nvidia" in lower:
        return "nvidia"
    if "amd" in lower or "radeon" in lower:
        return "amd"
    if "intel" in lower:
        return "intel"
    return "unknown"


def _get_gpus() -> list[GPUInfo]:
    """Detect GPUs."""
    gpus = []

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "wmic",
                    "path",
                    "win32_VideoController",
                    "get",
                    "Name,AdapterRAM,DriverVersion",
                    "/format:list",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            current: dict[str, str] = {}
            for line in (result.stdout or "").strip().split("\n"):
                line = line.strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    current[key.strip()] = val.strip()
                elif not line and current:
                    name = current.get("Name", "")
                    if name:
                        vram = None
                        try:
                            vram = int(current.get("AdapterRAM", "0")) // (1024 * 1024)
                        except (ValueError, TypeError):
                            pass
                        gpus.append(
                            GPUInfo(
                                name=name,
                                vendor=_vendor_from_name(name),
                                vram_mb=vram if vram else None,
                                driver_version=current.get("DriverVersion"),
                            )
                        )
                    current = {}
            # Handle last entry
            if current and current.get("Name"):
                name = current["Name"]
                gpus.append(GPUInfo(name=name, vendor=_vendor_from_name(name)))

        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    elif sys.platform == "linux":
        # Try lspci for GPU detection
        try:
            result = subprocess.run(
                ["lspci", "-mm"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (result.stdout or "").split("\n"):
                lower = line.lower()
                if "vga" in lower or "3d" in lower or "display" in lower:
                    # lspci -mm format: Slot "Class" "Vendor" "Device" ...
                    parts = line.split('"')
                    if len(parts) >= 8:
                        name = f"{parts[5]} {parts[7]}"  # Vendor + Device
                    else:
                        name = line.split(None, 1)[1] if " " in line else line
                    name = name.strip()
                    if name:
                        vram = _get_linux_gpu_vram(name)
                        driver = _get_linux_gpu_driver(name)
                        gpus.append(
                            GPUInfo(
                                name=name,
                                vendor=_vendor_from_name(name),
                                vram_mb=vram,
                                driver_version=driver,
                            )
                        )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not gpus:
        gpus.append(GPUInfo(name="Unknown GPU", vendor="unknown"))

    return gpus


def _get_linux_gpu_vram(gpu_name: str) -> int | None:
    """Try to get VRAM on Linux via nvidia-smi or DRM."""
    if "nvidia" in gpu_name.lower():
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            val = (result.stdout or "").strip().split("\n")[0].strip()
            if val:
                return int(val)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            pass
    # Try DRM subsystem
    try:
        for card_dir in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            mem_file = card_dir / "device" / "mem_info_vram_total"
            if mem_file.exists():
                vram_bytes = int(mem_file.read_text().strip())
                return vram_bytes // (1024 * 1024)
    except (OSError, ValueError):
        pass
    return None


def _get_linux_gpu_driver(gpu_name: str) -> str | None:
    """Try to get GPU driver version on Linux."""
    if "nvidia" in gpu_name.lower():
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            val = (result.stdout or "").strip().split("\n")[0].strip()
            if val:
                return val
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # Try kernel driver via /sys
    try:
        for card_dir in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            driver_link = card_dir / "device" / "driver"
            if driver_link.is_symlink():
                return Path(driver_link.resolve()).name
    except OSError:
        pass
    return None


def _get_wifi() -> list[str]:
    """Detect WiFi adapters."""
    adapters = []
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "drivers"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in (result.stdout or "").split("\n"):
                if ":" in line and any(
                    kw in line.lower() for kw in ["vendor", "provider", "description"]
                ):
                    adapters.append(line.split(":", 1)[1].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "linux":
        # Try iw dev first
        try:
            result = subprocess.run(
                ["iw", "dev"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (result.stdout or "").split("\n"):
                line = line.strip()
                if line.startswith("Interface "):
                    iface = line.split()[1]
                    # Get device info via /sys
                    phy_path = Path(f"/sys/class/net/{iface}/device")
                    if phy_path.exists():
                        try:
                            result2 = subprocess.run(
                                ["lspci", "-s", (phy_path / "uevent").read_text().split("PCI_SLOT_NAME=")[1].split("\n")[0], "-mm"],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            parts = result2.stdout.split('"')
                            if len(parts) >= 8:
                                adapters.append(f"{parts[5]} {parts[7]}")
                                continue
                        except (FileNotFoundError, subprocess.TimeoutExpired, IndexError, OSError):
                            pass
                    adapters.append(iface)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback: check /sys/class/net for wireless interfaces
        if not adapters:
            try:
                for net_dir in Path("/sys/class/net").iterdir():
                    if (net_dir / "wireless").exists():
                        adapters.append(net_dir.name)
            except OSError:
                pass
    return adapters


def _get_bluetooth() -> bool:
    """Check if Bluetooth is present."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Select-Object -First 1",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return bool((result.stdout or "").strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "linux":
        # Check /sys/class/bluetooth for any adapters
        try:
            bt_dir = Path("/sys/class/bluetooth")
            if bt_dir.exists() and any(bt_dir.iterdir()):
                return True
        except OSError:
            pass
        # Fallback: try hciconfig
        try:
            result = subprocess.run(
                ["hciconfig"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "hci" in (result.stdout or "").lower()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return False


def _get_disks() -> list[DiskInfo]:
    """Get disk partition information."""
    disks = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append(
                DiskInfo(
                    device=part.device,
                    mount_point=part.mountpoint,
                    filesystem=part.fstype,
                    total_gb=round(usage.total / (1024**3), 1),
                    used_gb=round(usage.used / (1024**3), 1),
                    free_gb=round(usage.free / (1024**3), 1),
                )
            )
        except (PermissionError, OSError):
            continue
    return disks


def _is_efi() -> bool:
    """Check if system is EFI-booted."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["bcdedit", "/enum", "firmware"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    elif sys.platform == "linux":
        return Path("/sys/firmware/efi").exists()
    return False


def scan() -> HardwareInfo:
    """Scan all hardware information."""
    print_info("Scanning hardware...")

    cpu_name, cpu_cores = _get_cpu()
    ram = _get_ram()
    gpus = _get_gpus()
    wifi = _get_wifi()
    bluetooth = _get_bluetooth()
    disks = _get_disks()
    efi = _is_efi()

    info = HardwareInfo(
        cpu=cpu_name,
        cpu_cores=cpu_cores,
        ram_gb=ram,
        gpus=gpus,
        wifi_adapters=wifi,
        bluetooth=bluetooth,
        disks=disks,
        efi=efi,
    )

    print_info(f"CPU: {cpu_name} ({cpu_cores} cores)")
    print_info(f"RAM: {ram} GB")
    for gpu in gpus:
        vram_str = f" ({gpu.vram_mb} MB)" if gpu.vram_mb else ""
        print_info(f"GPU: {gpu.name}{vram_str} [{gpu.vendor}]")
    print_info(f"Disks: {len(disks)} partitions found")
    print_info(f"WiFi adapters: {len(wifi)}")
    print_info(f"Bluetooth: {'Yes' if bluetooth else 'No'}")
    print_info(f"EFI boot: {'Yes' if efi else 'No'}")

    return info
