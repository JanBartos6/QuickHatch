"""Scan hardware information."""

from __future__ import annotations

import json
import platform
import subprocess
import sys

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

    return cpu_name, psutil.cpu_count(logical=False) or psutil.cpu_count() or 1


def _get_ram() -> float:
    """Get total RAM in GB."""
    return round(psutil.virtual_memory().total / (1024**3), 1)


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
                        vendor = "unknown"
                        name_lower = name.lower()
                        if "nvidia" in name_lower:
                            vendor = "nvidia"
                        elif "amd" in name_lower or "radeon" in name_lower:
                            vendor = "amd"
                        elif "intel" in name_lower:
                            vendor = "intel"

                        vram = None
                        try:
                            vram = int(current.get("AdapterRAM", "0")) // (1024 * 1024)
                        except (ValueError, TypeError):
                            pass

                        gpus.append(
                            GPUInfo(
                                name=name,
                                vendor=vendor,
                                vram_mb=vram if vram else None,
                                driver_version=current.get("DriverVersion"),
                            )
                        )
                    current = {}
            # Handle last entry
            if current and current.get("Name"):
                name = current["Name"]
                vendor = "unknown"
                name_lower = name.lower()
                if "nvidia" in name_lower:
                    vendor = "nvidia"
                elif "amd" in name_lower or "radeon" in name_lower:
                    vendor = "amd"
                elif "intel" in name_lower:
                    vendor = "intel"
                gpus.append(GPUInfo(name=name, vendor=vendor))

        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not gpus:
        gpus.append(GPUInfo(name="Unknown GPU", vendor="unknown"))

    return gpus


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
