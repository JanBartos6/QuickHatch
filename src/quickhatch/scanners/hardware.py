"""Scan hardware information."""

from __future__ import annotations

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


def _coerce_int(value: object) -> int | None:
    try:
        ivalue = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None


def _coerce_refresh_hz(value: object) -> float | None:
    try:
        fvalue = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return round(fvalue, 2) if fvalue > 0 else None


def _get_displays() -> list[DisplayInfo]:
    """Detect connected displays and their current mode."""
    displays: list[DisplayInfo] = []

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    r"""
Add-Type -AssemblyName System.Windows.Forms
$refresh = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
  Where-Object { $_.CurrentRefreshRate } |
  Select-Object -First 1 -ExpandProperty CurrentRefreshRate
[System.Windows.Forms.Screen]::AllScreens |
  ForEach-Object {
    [pscustomobject]@{
      Name = $_.DeviceName
      Width = $_.Bounds.Width
      Height = $_.Bounds.Height
      RefreshHz = $refresh
      ScalePercent = $null
      Primary = $_.Primary
      Source = 'winforms'
    }
  } | ConvertTo-Json -Compress
""",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            raw = (result.stdout or "").strip()
            if raw:
                data = json.loads(raw)
                rows = data if isinstance(data, list) else [data]
                for row in rows:
                    name = str((row or {}).get("Name") or "").strip()
                    if not name:
                        continue
                    displays.append(
                        DisplayInfo(
                            name=name,
                            width_px=_coerce_int((row or {}).get("Width")),
                            height_px=_coerce_int((row or {}).get("Height")),
                            refresh_hz=_coerce_refresh_hz((row or {}).get("RefreshHz")),
                            scale_percent=_coerce_int((row or {}).get("ScalePercent")),
                            primary=bool((row or {}).get("Primary")),
                            source=str((row or {}).get("Source") or "winforms"),
                        )
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    elif sys.platform == "linux":
        try:
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            current_name = None
            current_primary = False
            for raw_line in (result.stdout or "").splitlines():
                line = raw_line.rstrip()
                if " connected" in line and not line.startswith(" "):
                    parts = line.split()
                    current_name = parts[0]
                    current_primary = " primary " in f" {line} "
                    width = height = None
                    if len(parts) >= 3:
                        for part in parts[2:]:
                            if "x" in part and "+" in part:
                                mode = part.split("+", 1)[0]
                                if "x" in mode:
                                    try:
                                        width, height = [int(x) for x in mode.split("x", 1)]
                                    except ValueError:
                                        width = height = None
                                break
                    displays.append(
                        DisplayInfo(
                            name=current_name,
                            width_px=width,
                            height_px=height,
                            primary=current_primary,
                            source="xrandr",
                        )
                    )
                    continue
                if current_name and line.startswith(" ") and "*" in line and displays:
                    mode_bits = line.split()
                    if mode_bits:
                        mode = mode_bits[0]
                        if "x" in mode:
                            try:
                                displays[-1].width_px, displays[-1].height_px = [
                                    int(x) for x in mode.split("x", 1)
                                ]
                            except ValueError:
                                pass
                    for token in mode_bits[1:]:
                        if token.endswith("*"):
                            token = token.rstrip("*+")
                        try:
                            displays[-1].refresh_hz = round(float(token.rstrip("+*")), 2)
                            break
                        except ValueError:
                            continue
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
            is_first = True
            for gpu in data.get("SPDisplaysDataType", []):
                for display in gpu.get("spdisplays_ndrvs", []):
                    name = (
                        display.get("_name")
                        or display.get("spdisplays_display-product-name")
                        or gpu.get("sppci_model")
                        or "Display"
                    )
                    resolution = str(display.get("_spdisplays_resolution") or "")
                    width = height = None
                    if " x " in resolution:
                        try:
                            width, height = [int(x.strip()) for x in resolution.split(" x ", 1)]
                        except ValueError:
                            width = height = None
                    displays.append(
                        DisplayInfo(
                            name=name,
                            width_px=width,
                            height_px=height,
                            refresh_hz=_coerce_refresh_hz(display.get("spdisplays_refresh_rate")),
                            primary=is_first,
                            source="system_profiler",
                        )
                    )
                    is_first = False
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    return displays


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


def scan() -> HardwareInfo:
    """Scan all hardware information."""
    print_info("Scanning hardware...")

    cpu_name, cpu_cores = _get_cpu()
    ram = _get_ram()
    gpus = _get_gpus()
    displays = _get_displays()
    wifi = _get_wifi()
    bluetooth = _get_bluetooth()
    disks = _get_disks()
    efi = _is_efi()

    info = HardwareInfo(
        cpu=cpu_name,
        cpu_cores=cpu_cores,
        ram_gb=ram,
        gpus=gpus,
        displays=displays,
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
    for display in displays:
        mode = ""
        if display.width_px and display.height_px:
            mode = f" {display.width_px}x{display.height_px}"
            if display.refresh_hz:
                mode += f" @{display.refresh_hz:g}Hz"
        primary = " [primary]" if display.primary else ""
        print_info(f"Display: {display.name}{mode}{primary}")
    print_info(f"Disks: {len(disks)} partitions found")
    print_info(f"WiFi adapters: {len(wifi)}")
    print_info(f"Bluetooth: {'Yes' if bluetooth else 'No'}")
    print_info(f"EFI boot: {'Yes' if efi else 'No'}")

    return info
