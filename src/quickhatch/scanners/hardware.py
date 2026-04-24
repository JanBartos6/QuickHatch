"""Scan hardware information."""

from __future__ import annotations

import ctypes
import json
import platform
import subprocess
import sys
from pathlib import Path

import psutil

from quickhatch.config import DiskInfo, DisplayInfo, GPUInfo, HardwareInfo
from quickhatch.console import print_info


def _parse_adapter_ram(raw: str | None) -> int | None:
    """Parse wmic's AdapterRAM field to MB.

    Three gotchas:
    - wmic reports AdapterRAM as a signed 32-bit int. A GPU with >= 2 GiB
      VRAM overflows to a negative number; >=4 GiB wraps to ~0.
    - Empty string / "0" → None (genuinely unknown, don't claim 0).
    - Non-numeric → None rather than crash.
    """
    if not raw:
        return None
    try:
        ram = int(raw)
    except (ValueError, TypeError):
        return None
    if ram == 0:
        # Genuinely unknown.
        return None
    if ram < 0:
        # Signed-32-bit overflow of a real unsigned value (common on
        # cards with >= 2 GiB VRAM). Reinterpret as uint32.
        ram = ram + (1 << 32)
        if ram <= 0:
            return None
    mb = ram // (1024 * 1024)
    return mb or None


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
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            val = (result.stdout or "").strip()
            if val:
                cpu_name = val
        except (FileNotFoundError, subprocess.TimeoutExpired):
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
    if "apple" in lower:
        return "apple"
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
                        gpus.append(
                            GPUInfo(
                                name=name,
                                vendor=_vendor_from_name(name),
                                vram_mb=_parse_adapter_ram(current.get("AdapterRAM")),
                                driver_version=current.get("DriverVersion"),
                            )
                        )
                    current = {}
            # Handle last entry — include vram + driver_version like
            # the in-loop case does.
            if current and current.get("Name"):
                name = current["Name"]
                gpus.append(
                    GPUInfo(
                        name=name,
                        vendor=_vendor_from_name(name),
                        vram_mb=_parse_adapter_ram(current.get("AdapterRAM")),
                        driver_version=current.get("DriverVersion"),
                    )
                )

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

    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            data = json.loads(result.stdout or "{}")
            for display in data.get("SPDisplaysDataType", []):
                name = display.get("sppci_model", display.get("_name", "Unknown GPU"))
                vram_str = display.get("sppci_vram", "")
                vram = None
                if vram_str:
                    # e.g. "8 GB" or "1536 MB"
                    try:
                        parts = vram_str.split()
                        val = int(parts[0])
                        if len(parts) > 1 and parts[1].upper() == "GB":
                            vram = val * 1024
                        else:
                            vram = val
                    except (ValueError, IndexError):
                        pass
                vendor = display.get("sppci_vendor", "")
                gpus.append(
                    GPUInfo(
                        name=name,
                        vendor=_vendor_from_name(vendor or name),
                        vram_mb=vram,
                    )
                )
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
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
                                [
                                    "lspci",
                                    "-s",
                                    (phy_path / "uevent")
                                    .read_text()
                                    .split("PCI_SLOT_NAME=")[1]
                                    .split("\n")[0],
                                    "-mm",
                                ],
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
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["system_profiler", "SPAirPortDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout or "{}")
            for iface in data.get("SPAirPortDataType", []):
                name = iface.get("_name", "Wi-Fi")
                card = iface.get("spairport_wifi_card_type", "")
                adapters.append(f"{name} ({card})" if card else name)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            # Fallback: just check if Wi-Fi interface exists
            try:
                result = subprocess.run(
                    ["networksetup", "-listallhardwareports"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                lines = (result.stdout or "").split("\n")
                for line in lines:
                    if "Wi-Fi" in line:
                        adapters.append("Wi-Fi")
                        break
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
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["system_profiler", "SPBluetoothDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout or "{}")
            return bool(data.get("SPBluetoothDataType"))
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
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
    elif sys.platform == "darwin":
        return True  # All Macs use EFI
    return False


def _get_displays() -> list[DisplayInfo]:
    """Best-effort display inventory from the source machine."""
    displays: list[DisplayInfo] = []
    if sys.platform == "win32":
        try:

            class _DEVMODEW(ctypes.Structure):
                _fields_ = [
                    ("dmDeviceName", ctypes.c_wchar * 32),
                    ("dmSpecVersion", ctypes.c_ushort),
                    ("dmDriverVersion", ctypes.c_ushort),
                    ("dmSize", ctypes.c_ushort),
                    ("dmDriverExtra", ctypes.c_ushort),
                    ("dmFields", ctypes.c_ulong),
                    ("dmPositionX", ctypes.c_long),
                    ("dmPositionY", ctypes.c_long),
                    ("dmDisplayOrientation", ctypes.c_ulong),
                    ("dmDisplayFixedOutput", ctypes.c_ulong),
                    ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short),
                    ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short),
                    ("dmCollate", ctypes.c_short),
                    ("dmFormName", ctypes.c_wchar * 32),
                    ("dmLogPixels", ctypes.c_ushort),
                    ("dmBitsPerPel", ctypes.c_ulong),
                    ("dmPelsWidth", ctypes.c_ulong),
                    ("dmPelsHeight", ctypes.c_ulong),
                    ("dmDisplayFlags", ctypes.c_ulong),
                    ("dmDisplayFrequency", ctypes.c_ulong),
                    ("dmICMMethod", ctypes.c_ulong),
                    ("dmICMIntent", ctypes.c_ulong),
                    ("dmMediaType", ctypes.c_ulong),
                    ("dmDitherType", ctypes.c_ulong),
                    ("dmReserved1", ctypes.c_ulong),
                    ("dmReserved2", ctypes.c_ulong),
                    ("dmPanningWidth", ctypes.c_ulong),
                    ("dmPanningHeight", ctypes.c_ulong),
                ]

            class _DISPLAY_DEVICEW(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("DeviceName", ctypes.c_wchar * 32),
                    ("DeviceString", ctypes.c_wchar * 128),
                    ("StateFlags", ctypes.c_ulong),
                    ("DeviceID", ctypes.c_wchar * 128),
                    ("DeviceKey", ctypes.c_wchar * 128),
                ]

            def _display_device(index: int, device_name: str | None = None) -> _DISPLAY_DEVICEW | None:
                device = _DISPLAY_DEVICEW()
                device.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
                parent = device_name if device_name is not None else None
                if ctypes.windll.user32.EnumDisplayDevicesW(parent, index, ctypes.byref(device), 0):
                    return device
                return None

            def _display_mode(device_name: str | None) -> _DEVMODEW | None:
                devmode = _DEVMODEW()
                devmode.dmSize = ctypes.sizeof(_DEVMODEW)
                if ctypes.windll.user32.EnumDisplaySettingsW(
                    device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(devmode)
                ):
                    return devmode
                return None

            def _scale_percent(devmode: _DEVMODEW) -> int | None:
                if not devmode.dmLogPixels:
                    return None
                # 96 DPI is Windows' 100% scale baseline.
                return max(50, round(float(devmode.dmLogPixels) / 96 * 100))

            ENUM_CURRENT_SETTINGS = -1
            DISPLAY_DEVICE_ACTIVE = 0x00000001
            DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
            seen_devices: set[str] = set()
            for index in range(32):
                adapter = _display_device(index)
                if adapter is None:
                    break
                if not adapter.StateFlags & DISPLAY_DEVICE_ACTIVE:
                    continue
                adapter_name = str(adapter.DeviceName or "")
                if adapter_name in seen_devices:
                    continue
                mode = _display_mode(adapter_name)
                if mode is None:
                    continue
                seen_devices.add(adapter_name)
                monitor = _display_device(0, adapter_name)
                monitor_name = ""
                if monitor is not None and monitor.DeviceString:
                    monitor_name = str(monitor.DeviceString).strip()
                adapter_label = str(adapter.DeviceString or adapter_name).strip()
                name = monitor_name or adapter_label or f"Display {index + 1}"
                displays.append(
                    DisplayInfo(
                        name=name,
                        width_px=int(mode.dmPelsWidth or 0),
                        height_px=int(mode.dmPelsHeight or 0),
                        refresh_hz=float(mode.dmDisplayFrequency or 0) or None,
                        scale_percent=_scale_percent(mode),
                        primary=bool(adapter.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
                        source_platform="win32",
                    )
                )

            if not displays:
                # Fallback for unusual Windows sessions where display-device
                # enumeration fails but the current desktop mode is available.
                devmode = _display_mode(None)
            if not displays and devmode is not None:
                displays.append(
                    DisplayInfo(
                        name="Primary Display",
                        width_px=int(devmode.dmPelsWidth or 0),
                        height_px=int(devmode.dmPelsHeight or 0),
                        refresh_hz=float(devmode.dmDisplayFrequency or 0) or None,
                        scale_percent=_scale_percent(devmode),
                        primary=True,
                        source_platform="win32",
                    )
                )
        except Exception:
            pass
    elif sys.platform == "linux":
        try:
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            for line in (result.stdout or "").splitlines():
                if " connected" not in line:
                    continue
                parts = line.split()
                name = parts[0]
                primary = " primary " in f" {line} "
                width = height = 0
                refresh = None
                for token in parts:
                    if "+" in token and "x" in token:
                        mode = token.split("+", 1)[0]
                        if "x" in mode:
                            try:
                                width, height = [int(v) for v in mode.split("x", 1)]
                            except ValueError:
                                width = height = 0
                        break
                displays.append(
                    DisplayInfo(
                        name=name,
                        width_px=width,
                        height_px=height,
                        refresh_hz=refresh,
                        primary=primary,
                        source_platform="linux",
                    )
                )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    return displays


def scan() -> HardwareInfo:
    """Scan all hardware information."""
    print_info("Scanning hardware...")

    cpu_name, cpu_cores = _get_cpu()
    ram = _get_ram()
    gpus = _get_gpus()
    wifi = _get_wifi()
    bluetooth = _get_bluetooth()
    disks = _get_disks()
    displays = _get_displays()
    efi = _is_efi()

    info = HardwareInfo(
        cpu=cpu_name,
        cpu_cores=cpu_cores,
        ram_gb=ram,
        gpus=gpus,
        wifi_adapters=wifi,
        bluetooth=bluetooth,
        disks=disks,
        displays=displays,
        efi=efi,
    )

    print_info(f"CPU: {cpu_name} ({cpu_cores} cores)")
    print_info(f"RAM: {ram} GB")
    for gpu in gpus:
        vram_str = f" ({gpu.vram_mb} MB)" if gpu.vram_mb else ""
        print_info(f"GPU: {gpu.name}{vram_str} [{gpu.vendor}]")
    print_info(f"Disks: {len(disks)} partitions found")
    if displays:
        for display in displays:
            mode = (
                f"{display.width_px}x{display.height_px}"
                if display.width_px and display.height_px
                else "unknown resolution"
            )
            hz = f" @ {display.refresh_hz:.0f} Hz" if display.refresh_hz else ""
            print_info(f"Display: {display.name} ({mode}{hz})")
    print_info(f"WiFi adapters: {len(wifi)}")
    print_info(f"Bluetooth: {'Yes' if bluetooth else 'No'}")
    print_info(f"EFI boot: {'Yes' if efi else 'No'}")

    return info
