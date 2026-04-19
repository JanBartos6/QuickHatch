"""Step 6: Bootable USB creation wizard."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import questionary

from quickhatch.config import UserProfile
from quickhatch.console import (
    console,
    print_info,
    print_step,
    print_success,
    print_warning,
)


def _list_usb_drives() -> list[dict]:
    """List available USB drives."""
    drives = []
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-Disk | Where-Object { $_.BusType -eq 'USB' } | "
                        "Select-Object Number, FriendlyName, "
                        "@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}} | "
                        "ConvertTo-Json"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for d in data:
                    drives.append(
                        {
                            "number": d.get("Number"),
                            "name": d.get("FriendlyName", "USB Drive"),
                            "size_gb": d.get("SizeGB", 0),
                        }
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    elif sys.platform == "darwin":
        # macOS: use diskutil to find external drives
        try:
            result = subprocess.run(
                ["diskutil", "list", "-plist", "external"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                import plistlib

                data = plistlib.loads(result.stdout.encode())
                for disk_id in data.get("AllDisks", []):
                    if "s" in disk_id:  # Skip partitions, keep whole disks
                        continue
                    try:
                        info = subprocess.run(
                            ["diskutil", "info", "-plist", disk_id],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        dinfo = plistlib.loads(info.stdout.encode())
                        name = dinfo.get("MediaName") or dinfo.get("IORegistryEntryName", disk_id)
                        size_bytes = dinfo.get("TotalSize", 0)
                        drives.append(
                            {
                                "number": f"/dev/{disk_id}",
                                "name": name,
                                "size_gb": round(size_bytes / (1024**3), 1),
                            }
                        )
                    except (subprocess.TimeoutExpired, Exception):
                        pass
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            pass
    else:
        # Linux: use lsblk to find removable drives
        try:
            result = subprocess.run(
                ["lsblk", "-Jdo", "NAME,SIZE,MODEL,RM,TYPE,TRAN"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                for dev in data.get("blockdevices", []):
                    # Filter for USB removable disks
                    is_usb = dev.get("tran") == "usb"
                    is_removable = dev.get("rm") in (1, "1", True, "true")
                    is_disk = dev.get("type") == "disk"
                    if is_disk and (is_usb or is_removable):
                        name = (dev.get("model") or dev.get("name", "USB Drive")).strip()
                        size_str = dev.get("size", "0")
                        # Parse size like "14.5G" or "500M"
                        size_gb = 0.0
                        try:
                            if size_str.endswith("G"):
                                size_gb = float(size_str[:-1])
                            elif size_str.endswith("T"):
                                size_gb = float(size_str[:-1]) * 1024
                            elif size_str.endswith("M"):
                                size_gb = round(float(size_str[:-1]) / 1024, 1)
                        except ValueError:
                            pass
                        drives.append(
                            {
                                "number": f"/dev/{dev['name']}",
                                "name": name,
                                "size_gb": size_gb,
                            }
                        )
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    return drives


def _get_distro_iso_url(distro: str) -> str | None:
    """Get the ISO download URL for a distro."""
    db_path = Path(__file__).parent.parent / "data" / "distros.json"
    if db_path.exists():
        # Explicit utf-8 — distros.json contains non-ASCII names
        # (elementary OS, Pop!_OS, Nobara) that crash under Windows'
        # cp1252 default on some locales.
        distros = json.loads(db_path.read_text(encoding="utf-8"))
        for name, info in distros.items():
            if name.lower() == distro.lower():
                return info.get("iso_url")
    return None


def run(profile: UserProfile) -> None:
    print_step(6, "Bootable USB Creation")

    distro = profile.preferences.distro_preference or "your chosen distro"
    iso_url = _get_distro_iso_url(distro) if profile.preferences.distro_preference else None

    if profile.preferences.distro_preference:
        console.print(
            f"Now let's create a bootable USB drive with [bold cyan]{distro}[/bold cyan].\n"
        )
    else:
        console.print(
            "Now let's prepare a bootable USB drive.\n"
            "Since you haven't picked a distro yet, the AI agent will recommend one\n"
            "based on your data. You can download that ISO and flash it later,\n"
            "or pick one now to get started.\n"
        )

    # Ask what media to use
    method = questionary.select(
        "How would you like to create the bootable drive?",
        choices=[
            questionary.Choice(
                "I'll download the ISO myself and use Rufus/Ventoy/balenaEtcher",
                value="manual",
            ),
            questionary.Choice(
                "Guide me through it step by step",
                value="guided",
            ),
            questionary.Choice(
                "Skip this step (I already have a bootable drive)",
                value="skip",
            ),
        ],
    ).ask()

    if method is None:
        raise KeyboardInterrupt

    if method == "skip":
        print_info("Skipping USB creation.")
        profile.completed_steps.append("usb")
        return

    if method == "manual":
        if iso_url:
            console.print(
                f"\n[bold]Download {distro} from:[/bold]\n"
                f"  [cyan]{iso_url}[/cyan]\n\n"
                "Recommended tools for creating bootable USB:\n"
                "  1. [bold]Ventoy[/bold] - Multi-ISO USB, just copy the ISO file\n"
                "  2. [bold]Rufus[/bold] - Simple and reliable\n"
                "  3. [bold]balenaEtcher[/bold] - Cross-platform, very easy\n"
            )
        else:
            console.print(f"\nSearch for '{distro} download ISO' to get the installer.\n")
        print_success("Follow the tool's instructions to create the bootable USB.")
        profile.completed_steps.append("usb")
        return

    # Guided mode
    console.print("[bold]Guided USB Creation[/bold]\n")

    if iso_url:
        console.print(f"1. Download {distro} ISO from: [cyan]{iso_url}[/cyan]")
    else:
        console.print(f"1. Download the {distro} ISO from the official website")

    console.print(
        "2. Insert a USB drive (8 GB or larger recommended)\n"
        "3. Use one of these tools to write the ISO:\n"
    )

    # Detect USB drives
    drives = _list_usb_drives()
    if drives:
        console.print("[bold]Detected USB drives:[/bold]")
        for d in drives:
            console.print(f"  - Disk {d['number']}: {d['name']} ({d['size_gb']} GB)")
        console.print()
        print_warning(
            "WRITING TO A USB DRIVE WILL ERASE ALL DATA ON IT. "
            "Make sure you select the correct drive!"
        )
    else:
        print_info("No USB drives detected. Insert one and re-run this step if needed.")

    # Recommend Ventoy for simplicity
    console.print(
        "\n[bold]Recommended: Ventoy[/bold]\n"
        "  Ventoy lets you boot from ISOs by simply copying them to the USB.\n"
        "  Steps:\n"
        "    a. Download Ventoy from: https://ventoy.net\n"
        "    b. Run Ventoy2Disk.exe and install to your USB\n"
        "    c. Copy the downloaded ISO file to the USB drive\n"
        "    d. Boot from the USB to start installation\n"
    )

    questionary.confirm("Press Enter when you're ready to continue...", default=True).ask()

    print_success("USB creation step complete!")
    profile.completed_steps.append("usb")
