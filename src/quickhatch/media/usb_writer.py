"""Write ISO to USB drive."""

from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
from pathlib import Path

from quickhatch.console import console, print_error, print_info, print_warning


def list_usb_drives() -> list[dict]:
    """List available USB drives."""
    drives = []

    if sys.platform == "win32":
        drives = _list_usb_windows()
    else:
        drives = _list_usb_linux()

    return drives


def write_iso_to_usb(iso_path: Path, device: str) -> bool:
    """Write an ISO file to a USB drive."""
    if sys.platform == "win32":
        return _write_windows(iso_path, device)
    else:
        return _write_linux(iso_path, device)


def _list_usb_windows() -> list[dict]:
    """List USB drives on Windows using PowerShell."""
    try:
        result = subprocess.run(
            [
                "powershell", "-Command",
                "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
                "Select-Object Number, FriendlyName, @{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}} | "
                "ConvertTo-Json"
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []

        import json
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]

        return [
            {
                "device": f"\\\\.\\PhysicalDrive{d['Number']}",
                "name": d.get("FriendlyName", f"Disk {d['Number']}"),
                "size": f"{d.get('SizeGB', '?')} GB",
            }
            for d in data
        ]
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []


def _list_usb_linux() -> list[dict]:
    """List USB drives on Linux."""
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,TRAN,MODEL,RM"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        import json
        data = json.loads(result.stdout)
        drives = []

        for device in data.get("blockdevices", []):
            # Look for removable USB devices
            if device.get("type") == "disk" and (
                device.get("tran") == "usb" or device.get("rm") is True
            ):
                drives.append({
                    "device": f"/dev/{device['name']}",
                    "name": (device.get("model") or device["name"]).strip(),
                    "size": device.get("size", "?"),
                })

        return drives
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []


def _write_windows(iso_path: Path, device: str) -> bool:
    """Write ISO to USB on Windows."""
    # Check for admin privileges
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except AttributeError:
        is_admin = False

    if not is_admin:
        print_warning("Administrator privileges required to write to USB.")
        print_info("Please run QuickHatch as administrator, or use Rufus/Ventoy manually.")
        return False

    # Try using PowerShell to write (basic dd-like operation)
    try:
        console.print("Writing ISO to USB drive...")
        # Use PowerShell's Copy-Item for raw disk write
        # Escape single quotes for safe PowerShell string interpolation
        safe_iso = str(iso_path).replace("'", "''")
        safe_device = device.replace("'", "''")
        ps_script = f"""
        $iso = '{safe_iso}'
        $disk = '{safe_device}'
        # Clear the disk first
        Clear-Disk -Number ($disk -replace '.*?(\\d+)$','$1') -RemoveData -Confirm:$false
        # Write ISO
        $source = [System.IO.File]::OpenRead($iso)
        $dest = [System.IO.File]::OpenWrite($disk)
        $source.CopyTo($dest)
        $source.Close()
        $dest.Close()
        """
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=600,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print_error(f"Failed to write ISO: {e}")
        return False


def _write_linux(iso_path: Path, device: str) -> bool:
    """Write ISO to USB on Linux using dd."""
    try:
        # Unmount any partitions on the device first
        import glob
        for part in glob.glob(f"{device}*"):
            if part != device:
                subprocess.run(
                    ["sudo", "umount", part],
                    capture_output=True, timeout=10,
                )

        console.print(f"Writing {iso_path.name} to {device}...")
        console.print("[dim]This may take several minutes...[/dim]")

        result = subprocess.run(
            [
                "sudo", "dd",
                f"if={iso_path}",
                f"of={device}",
                "bs=4M",
                "status=progress",
                "oflag=sync",
            ],
            timeout=1800,  # 30 minute timeout
        )

        if result.returncode == 0:
            subprocess.run(["sync"], timeout=60)
            return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print_error(f"Failed to write ISO: {e}")
        return False
