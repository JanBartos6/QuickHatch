"""Step 6: Bootable USB creation."""

import platform
import subprocess

import questionary
from rich.panel import Panel

from quickhatch.config import UserProfile
from quickhatch.console import console, print_step, print_success, print_warning, print_info, print_error
from quickhatch.media.iso_download import get_iso_url, download_iso
from quickhatch.media.usb_writer import list_usb_drives, write_iso_to_usb


def run(profile: UserProfile) -> None:
    """Run the bootable USB creation step."""
    print_step(6, "Bootable USB Creation")

    if not profile.recommended_distro:
        print_warning("No distro selected. Skipping USB creation.")
        profile.completed_steps.append("usb")
        return

    distro = profile.recommended_distro
    console.print(
        f"We'll now create a bootable USB drive with [highlight]{distro}[/highlight].\n"
    )

    # Ask if they want to proceed
    proceed = questionary.confirm(
        "Do you want to create a bootable USB drive now?",
        default=True,
    ).ask()

    if not proceed:
        print_info("Skipping USB creation. You can create it manually later.")
        profile.completed_steps.append("usb")
        return

    # Get ISO URL
    iso_url = get_iso_url(distro)
    if iso_url:
        console.print(f"ISO download URL: [link]{iso_url}[/link]\n")
    else:
        print_warning(f"No automatic download URL for {distro}.")
        iso_url = questionary.text(
            "Please paste the ISO download URL:",
        ).ask()
        if not iso_url:
            print_warning("No URL provided. Skipping USB creation.")
            profile.completed_steps.append("usb")
            return

    # Detect USB drives
    drives = list_usb_drives()

    if not drives:
        print_warning(
            "No USB drives detected. Please insert a USB drive and try again,\n"
            "or create the bootable drive manually using Rufus, Ventoy, or Etcher."
        )
        console.print()
        console.print(Panel(
            "[bold]Manual options:[/bold]\n"
            "  - Rufus: https://rufus.ie\n"
            "  - Ventoy: https://www.ventoy.net\n"
            "  - balenaEtcher: https://etcher.balena.io",
            title="Alternative Tools",
            border_style="cyan",
        ))
        profile.completed_steps.append("usb")
        return

    # Let user pick a drive
    drive_choices = [
        questionary.Choice(
            f"{d['name']} ({d['size']})",
            value=d["device"],
        )
        for d in drives
    ]
    drive_choices.append(questionary.Choice("Cancel - I'll do it manually", value=None))

    selected_drive = questionary.select(
        "Select the USB drive to use (ALL DATA WILL BE ERASED):",
        choices=drive_choices,
    ).ask()

    if not selected_drive:
        print_info("USB creation cancelled.")
        profile.completed_steps.append("usb")
        return

    # Final confirmation
    console.print()
    console.print(Panel(
        f"[bold red]WARNING:[/bold red] This will ERASE ALL DATA on {selected_drive}!\n\n"
        f"  Distro: {distro}\n"
        f"  Target: {selected_drive}\n"
        f"  ISO:    {iso_url}",
        title="Confirm USB Creation",
        border_style="red",
    ))

    confirm = questionary.confirm(
        "Are you absolutely sure? This cannot be undone!",
        default=False,
    ).ask()

    if not confirm:
        print_info("USB creation cancelled.")
        profile.completed_steps.append("usb")
        return

    # Download and write
    console.print()
    iso_path = download_iso(iso_url, distro)
    if iso_path:
        success = write_iso_to_usb(iso_path, selected_drive)
        if success:
            profile.usb_device = selected_drive
            print_success(f"Bootable USB created successfully on {selected_drive}!")
        else:
            print_error("Failed to write ISO to USB. Try using Rufus or Ventoy manually.")
    else:
        print_error("Failed to download ISO. Check your internet connection.")

    profile.completed_steps.append("usb")
