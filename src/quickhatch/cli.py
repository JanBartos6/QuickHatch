"""Main CLI orchestrator for the QuickHatch migration wizard."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from quickhatch.config import UserProfile
from quickhatch.console import console, print_error, print_info, print_success, print_warning


WIZARD_STEPS = [
    ("welcome", "Welcome"),
    ("ai_setup", "AI Assistant Setup"),
    ("questionnaire", "Preferences"),
    ("scan", "System Scan"),
    ("additional", "Additional Information"),
    ("bridge", "AI Agent Bridge"),
    ("usb", "Bootable USB Creation"),
    ("ssh", "SSH Key Setup"),
    ("export", "Export Migration Bundle"),
]


def _populate_demo_profile(profile: UserProfile) -> None:
    """Fill profile with realistic demo data for testing on Linux."""
    from quickhatch.config import AIConfig, AppInfo, DiskInfo, GPUInfo, HardwareInfo, Preferences

    profile.preferences = Preferences(
        migration_type="fresh_install",
        desktop_look="windows_like",
        icon_style="flat",
        wallpaper_style="dark",
        use_cases=["development", "gaming", "browsing"],
        distro_preference="",
        additional_notes="",
    )
    profile.ai_config = AIConfig(provider="api")
    profile.hardware = HardwareInfo(
        cpu="AMD Ryzen 7 5800X 8-Core Processor",
        cpu_cores=8,
        ram_gb=32.0,
        gpus=[GPUInfo(name="NVIDIA GeForce RTX 3070", vendor="nvidia", vram_mb=8192, driver_version="546.33")],
        wifi_adapters=["Intel Wi-Fi 6E AX210 160MHz"],
        bluetooth=True,
        efi=True,
        disks=[
            DiskInfo(device="C:", mount_point="C:\\", filesystem="NTFS", total_gb=500, used_gb=310, free_gb=190),
            DiskInfo(device="D:", mount_point="D:\\", filesystem="NTFS", total_gb=1000, used_gb=650, free_gb=350),
        ],
    )
    profile.apps = [
        AppInfo(name="Google Chrome", version="120.0", source="winget"),
        AppInfo(name="Mozilla Firefox", version="121.0", source="winget"),
        AppInfo(name="Visual Studio Code", version="1.85.1", source="winget"),
        AppInfo(name="Steam", version="2.0", source="registry"),
        AppInfo(name="Discord", version="1.0.9036", source="registry"),
        AppInfo(name="Spotify", version="1.2.26", source="registry"),
        AppInfo(name="Adobe Photoshop", version="25.3", source="registry"),
        AppInfo(name="Notepad++", version="8.6.2", source="winget"),
        AppInfo(name="OBS Studio", version="30.0.2", source="winget"),
        AppInfo(name="VLC Media Player", version="3.0.20", source="winget"),
        AppInfo(name="7-Zip", version="23.01", source="registry"),
        AppInfo(name="Docker Desktop", version="4.26", source="winget"),
        AppInfo(name="Microsoft Office", version="2021", source="registry"),
        AppInfo(name="Slack", version="4.35", source="registry"),
        AppInfo(name="PuTTY", version="0.80", source="registry"),
        AppInfo(name="WinSCP", version="6.1", source="registry"),
        AppInfo(name="Blender", version="4.0", source="winget"),
        AppInfo(name="Epic Games Store", version="15.6", source="registry"),
    ]
    profile.windows_settings = {
        "theme": "dark",
        "default_browser": "Google Chrome",
        "locale": "en_US",
        "keyboard_layouts": ["en-US"],
    }
    profile.files_summary = {
        "total_files": 47832,
        "total_size_mb": 28450,
        "top_types": {".py": 1250, ".js": 890, ".pdf": 340, ".png": 2100, ".jpg": 1800, ".docx": 120, ".mp4": 45},
        "browser_profiles": {"chrome": "C:\\Users\\User\\AppData\\Local\\Google\\Chrome\\User Data", "firefox": "C:\\Users\\User\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles"},
        "scanned_dirs": ["C:\\Users\\User\\Documents", "C:\\Users\\User\\Desktop", "C:\\Users\\User\\Downloads"],
    }
    print_success("Demo profile loaded with sample Windows data")


def main() -> None:
    """Run the QuickHatch migration wizard."""
    parser = argparse.ArgumentParser(description="QuickHatch — Windows to Linux Migration Wizard")
    parser.add_argument("--gui", action="store_true", help="Launch web GUI instead of terminal wizard")
    parser.add_argument("--demo", action="store_true", help="Load demo data (for testing on Linux)")
    parser.add_argument("--port", type=int, default=0, help="Port for GUI/bridge server (0 = auto)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser (GUI mode)")
    parser.add_argument("--tauri", action="store_true", help="Machine-readable output for Tauri wrapper")
    args = parser.parse_args()

    if args.gui:
        return _run_gui(args)

    # Terminal mode
    profile = None
    if not args.demo:
        profile = UserProfile.load_checkpoint()
        if profile and profile.completed_steps:
            print_info(
                f"Found a previous session with {len(profile.completed_steps)} "
                f"completed steps: {', '.join(profile.completed_steps)}"
            )
            try:
                import questionary

                resume = questionary.confirm(
                    "Would you like to resume where you left off?", default=True
                ).ask()
                if resume is None:
                    raise KeyboardInterrupt
                if not resume:
                    profile = UserProfile()
            except KeyboardInterrupt:
                console.print("\n[dim]Goodbye![/dim]")
                sys.exit(0)

    if profile is None:
        profile = UserProfile()

    if args.demo:
        _populate_demo_profile(profile)

    # Import wizard steps
    from quickhatch.wizard import (
        additional,
        ai_setup,
        bridge,
        export,
        questionnaire,
        scan,
        ssh,
        usb,
        welcome,
    )

    step_modules = {
        "welcome": welcome,
        "ai_setup": ai_setup,
        "questionnaire": questionnaire,
        "scan": scan,
        "additional": additional,
        "bridge": bridge,
        "usb": usb,
        "ssh": ssh,
        "export": export,
    }

    try:
        for step_name, step_title in WIZARD_STEPS:
            if step_name in profile.completed_steps:
                print_info(f"Skipping completed step: {step_title}")
                continue

            module = step_modules[step_name]
            module.run(profile)

            # Save checkpoint after each step
            profile.save_checkpoint()

        # Final message
        console.print("\n")
        console.print(
            "[bold green]"
            "============================================================\n"
            "  QuickHatch migration preparation complete!\n"
            "============================================================"
            "[/bold green]\n"
        )
        console.print(
            "Your migration bundle is ready. Here's what to do next:\n\n"
            "  1. Boot from the USB drive you prepared\n"
            "  2. Install Linux following the distro's installer\n"
            "  3. After installation, copy the migration bundle\n"
            "  4. Run [cyan]bash quickstart.sh[/cyan] to set up the AI agent\n"
            "  5. Feed [cyan]MIGRATION_INSTRUCTIONS.md[/cyan] to your AI agent\n"
            "  6. The agent will handle the rest: drivers, apps, theming, and more!\n\n"
            "[dim]Thank you for using QuickHatch![/dim]\n"
        )

    except KeyboardInterrupt:
        console.print("\n\n[warning]Interrupted![/warning] Your progress has been saved.")
        profile.save_checkpoint()
        print_info("Run quickhatch again to resume from where you left off.")
        sys.exit(1)
    except Exception as e:
        print_error(f"An error occurred: {e}")
        profile.save_checkpoint()
        print_info("Your progress has been saved. Run quickhatch again to resume.")
        raise


def _run_gui(args) -> None:
    """Launch the web GUI."""
    from quickhatch.bridge.server import BridgeState
    from quickhatch.gui.server import GUIState, make_gui_server

    profile = UserProfile.load_checkpoint() or UserProfile()

    if args.demo:
        _populate_demo_profile(profile)

    bridge_state = BridgeState(profile)
    gui_state = GUIState(profile, bridge_state)

    server, port = make_gui_server(gui_state, port=args.port)
    url = f"http://localhost:{port}"

    if getattr(args, "tauri", False):
        # Machine-readable output for Tauri to parse the port
        sys.stdout.write(f"QUICKHATCH_PORT={port}\n")
        sys.stdout.flush()
    else:
        console.print(f"\n[bold green]QuickHatch GUI running at:[/bold green] [cyan]{url}[/cyan]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    if not args.no_browser and not getattr(args, "tauri", False):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down...[/dim]")
        server.shutdown()


if __name__ == "__main__":
    main()
