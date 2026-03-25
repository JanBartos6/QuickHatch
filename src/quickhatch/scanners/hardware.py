"""Scan system hardware information."""

from __future__ import annotations

import os
import platform
import subprocess
import sys

import psutil

from quickhatch.config import DiskInfo, GPUInfo, HardwareInfo


def scan() -> HardwareInfo:
    """Scan system hardware."""
    info = HardwareInfo()

    # CPU
    info.cpu = platform.processor() or _get_cpu_name()
    info.cpu_cores = psutil.cpu_count(logical=True) or 0

    # RAM
    mem = psutil.virtual_memory()
    info.ram_gb = round(mem.total / (1024 ** 3), 1)

    # Disks
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            info.disks.append(DiskInfo(
                device=part.device,
                mountpoint=part.mountpoint,
                filesystem=part.fstype,
                total_gb=round(usage.total / (1024 ** 3), 1),
                used_gb=round(usage.used / (1024 ** 3), 1),
                free_gb=round(usage.free / (1024 ** 3), 1),
            ))
        except (PermissionError, OSError):
            pass

    # GPU
    info.gpus = _scan_gpus()

    # WiFi & Bluetooth
    info.wifi_adapters = _scan_wifi()
    info.bluetooth = _check_bluetooth()

    # EFI
    info.efi = _check_efi()

    return info


def _get_cpu_name() -> str:
    """Get CPU name string."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "cpu", "get", "name", "/format:list"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().split("\n"):
                if line.startswith("Name="):
                    return line.split("=", 1)[1].strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except (FileNotFoundError, PermissionError):
            pass
    return "Unknown CPU"


def _scan_gpus() -> list[GPUInfo]:
    """Scan for GPUs."""
    gpus = []

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get",
                 "name,AdapterRAM,DriverVersion", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().split("\n")[1:]:
                parts = line.strip().split(",")
                if len(parts) >= 4:
                    vram = 0
                    try:
                        vram = int(parts[1]) // (1024 * 1024) if parts[1] else 0
                    except ValueError:
                        pass
                    name = parts[3].strip() if len(parts) > 3 else parts[2].strip()
                    vendor = _detect_gpu_vendor(name)
                    gpus.append(GPUInfo(
                        name=name,
                        vendor=vendor,
                        vram_mb=vram,
                        driver_version=parts[2].strip() if len(parts) > 2 else "",
                    ))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    else:
        # Linux: try lspci
        try:
            result = subprocess.run(
                ["lspci", "-nn"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.split("\n"):
                if "VGA" in line or "3D" in line or "Display" in line:
                    name = line.split(":", 2)[-1].strip() if ":" in line else line
                    vendor = _detect_gpu_vendor(name)
                    gpus.append(GPUInfo(name=name, vendor=vendor))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return gpus


def _detect_gpu_vendor(name: str) -> str:
    """Detect GPU vendor from name string."""
    name_lower = name.lower()
    if "nvidia" in name_lower or "geforce" in name_lower or "quadro" in name_lower:
        return "nvidia"
    elif "amd" in name_lower or "radeon" in name_lower:
        return "amd"
    elif "intel" in name_lower:
        return "intel"
    return "unknown"


def _scan_wifi() -> list[str]:
    """Scan for WiFi adapters."""
    adapters = []

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "drivers"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.split("\n"):
                if ":" in line and any(kw in line.lower() for kw in ["driver", "vendor", "description"]):
                    adapters.append(line.split(":", 1)[1].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            result = subprocess.run(
                ["iwconfig"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.split("\n"):
                if "ESSID" in line or "IEEE" in line:
                    parts = line.split()
                    if parts:
                        adapters.append(parts[0])
        except FileNotFoundError:
            # Try ip link as fallback
            try:
                result = subprocess.run(
                    ["ip", "link", "show"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in result.stdout.split("\n"):
                    if "wl" in line and ":" in line:
                        name = line.split(":")[1].strip()
                        adapters.append(name)
            except FileNotFoundError:
                pass

    return adapters


def _check_bluetooth() -> bool:
    """Check if Bluetooth is available."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "path", "Win32_PnPEntity", "where",
                 "caption like '%Bluetooth%'", "get", "caption"],
                capture_output=True, text=True, timeout=10,
            )
            return "Bluetooth" in result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    else:
        try:
            result = subprocess.run(
                ["bluetoothctl", "show"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except FileNotFoundError:
            # Check if bluetooth module exists
            try:
                result = subprocess.run(
                    ["lsmod"],
                    capture_output=True, text=True, timeout=5,
                )
                return "bluetooth" in result.stdout.lower()
            except FileNotFoundError:
                return False


def _check_efi() -> bool:
    """Check if system uses UEFI."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["bcdedit", "/enum", "firmware"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except FileNotFoundError:
            # Check for EFI partition
            return os.path.exists(r"\\.\EFI")
    else:
        return os.path.isdir("/sys/firmware/efi")
