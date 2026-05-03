"""Main CLI orchestrator for the QuickHatch migration wizard."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path

from quickhatch.config import UserProfile
from quickhatch.console import console, print_error, print_info, print_success

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
    from quickhatch.analysis.app_mapper import map_apps
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
        gpus=[
            GPUInfo(
                name="NVIDIA GeForce RTX 3070",
                vendor="nvidia",
                vram_mb=8192,
                driver_version="546.33",
            )
        ],
        wifi_adapters=["Intel Wi-Fi 6E AX210 160MHz"],
        bluetooth=True,
        efi=True,
        disks=[
            DiskInfo(
                device="C:",
                mount_point="C:\\",
                filesystem="NTFS",
                total_gb=500,
                used_gb=310,
                free_gb=190,
            ),
            DiskInfo(
                device="D:",
                mount_point="D:\\",
                filesystem="NTFS",
                total_gb=1000,
                used_gb=650,
                free_gb=350,
            ),
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
    # Enrich with Linux compatibility info so the Apps step shows proper badges
    try:
        profile.apps = map_apps(profile.apps)
    except Exception:
        pass
    profile.windows_settings = {
        "theme": "dark",
        "default_browser": "Google Chrome",
        "locale": "en_US",
        "keyboard_layouts": ["en-US"],
    }
    profile.files_summary = {
        "total_files": 47832,
        "total_size_mb": 28450,
        "top_types": {
            ".py": 1250,
            ".js": 890,
            ".pdf": 340,
            ".png": 2100,
            ".jpg": 1800,
            ".docx": 120,
            ".mp4": 45,
        },
        "browser_profiles": {
            "chrome": "C:\\Users\\User\\AppData\\Local\\Google\\Chrome\\User Data",
            "firefox": "C:\\Users\\User\\AppData\\Roaming\\Mozilla\\Firefox\\Profiles",
        },
        "scanned_dirs": [
            "C:\\Users\\User\\Documents",
            "C:\\Users\\User\\Desktop",
            "C:\\Users\\User\\Downloads",
        ],
    }
    print_success("Demo profile loaded with sample Windows data")


def main() -> None:
    """Run the QuickHatch migration wizard."""
    parser = argparse.ArgumentParser(description="QuickHatch — Windows to Linux Migration Wizard")
    subparsers = parser.add_subparsers(dest="quickhatch_command")

    debug_parser = subparsers.add_parser("debug", help="Replay and promote setup run artifacts")
    debug_subparsers = debug_parser.add_subparsers(dest="debug_command")
    replay_parser = debug_subparsers.add_parser("replay-run", help="Summarize a setup run JSONL")
    replay_parser.add_argument("runlog")
    replay_parser.add_argument("--summary-only", action="store_true")
    stage_parser = debug_subparsers.add_parser("stage", help="Replay one stage/group from JSONL")
    stage_parser.add_argument("runlog")
    stage_parser.add_argument("--stage", required=True)
    stage_parser.add_argument("--group")
    promote_parser = debug_subparsers.add_parser(
        "promote-regression",
        help="Generate a pytest regression from a runlog event",
    )
    promote_parser.add_argument("runlog")
    promote_parser.add_argument("--event", required=True)
    promote_parser.add_argument("--name", required=True)
    promote_parser.add_argument("--output-dir", default="tests/generated")
    preflight_parser = debug_subparsers.add_parser(
        "preflight",
        help="Run local preflight decisions for one shell command",
    )
    preflight_parser.add_argument("shell_command")
    preflight_parser.add_argument("--facts", help="SetupFacts JSON or runlog facts event")
    preflight_parser.add_argument("--shadow", action="store_true")
    classify_parser = debug_subparsers.add_parser(
        "classify",
        help="Classify a command/model failure without SSH",
    )
    classify_parser.add_argument("--command", dest="failure_command", default="")
    classify_parser.add_argument("--rc", type=int)
    classify_parser.add_argument("--stdout", default="")
    classify_parser.add_argument("--stderr", default="")
    classify_parser.add_argument("--detail", default="")
    adapter_parser = debug_subparsers.add_parser(
        "adapter-spine",
        help="Print deterministic family install spine",
    )
    adapter_parser.add_argument(
        "--family", required=True, choices=["arch", "ubuntu", "debian", "fedora"]
    )
    adapter_parser.add_argument("--profile")
    health_parser = debug_subparsers.add_parser(
        "harness-health",
        help="Run the fast local harness health gate",
    )
    health_parser.add_argument("--root")
    health_parser.add_argument("--keep-artifacts", action="store_true")
    debug_subparsers.add_parser(
        "fuzz-harness",
        help="Run deterministic adversarial harness policy fuzzing",
    )
    latest_parser = debug_subparsers.add_parser(
        "latest-run",
        help="Find and summarize the latest real setup run artifact",
    )
    latest_parser.add_argument("--root")
    latest_parser.add_argument("--summary-only", action="store_true")
    scan_parser = debug_subparsers.add_parser(
        "scan-runlogs",
        help="Aggregate diagnostics across setup run artifacts",
    )
    scan_parser.add_argument("--root")
    scan_parser.add_argument("--limit", type=int, default=50)

    certify_parser = subparsers.add_parser("certify", help="Run certification tooling")
    certify_subparsers = certify_parser.add_subparsers(dest="certify_command")
    vm_parser = certify_subparsers.add_parser("vm", help="Prepare/run VM certification")
    vm_parser.add_argument(
        "--family", required=True, choices=["arch", "ubuntu", "debian", "fedora"]
    )
    vm_parser.add_argument("--iso", required=True)
    vm_parser.add_argument("--profile", required=True)
    vm_parser.add_argument("--backend", default="virtualbox", choices=["virtualbox", "qemu"])
    vm_parser.add_argument("--dry-run", action="store_true")
    vm_parser.add_argument("--ssh-user", default="root")
    vm_parser.add_argument("--ssh-key")
    vm_parser.add_argument(
        "--target-disk",
        help="Target disk inside the VM (defaults to /dev/sda for VirtualBox, /dev/vda for QEMU).",
    )
    vm_parser.add_argument("--post-reboot-user", default=None)
    vm_parser.add_argument(
        "--no-post-reboot",
        action="store_true",
        help="Only run live-ISO install spine; do not detach ISO/reboot into installed OS.",
    )
    parser.add_argument(
        "--gui", action="store_true", help="Launch web GUI instead of terminal wizard"
    )
    parser.add_argument("--demo", action="store_true", help="Load demo data (for testing on Linux)")
    parser.add_argument("--port", type=int, default=0, help="Port for GUI/bridge server (0 = auto)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address for the GUI/bridge server. Default 127.0.0.1. Use 0.0.0.0 to "
        "expose on all interfaces (needed when testing with a VirtualBox VM that has to "
        "curl into the wizard via 10.0.2.2).",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't auto-open browser (GUI mode)"
    )
    args = parser.parse_args()

    if args.quickhatch_command == "debug":
        return _run_debug_command(args)

    if args.quickhatch_command == "certify":
        return _run_certify_command(args)

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

    server, port = make_gui_server(gui_state, port=args.port, host=args.host)
    url = f"http://localhost:{port}"

    console.print(f"\n[bold green]QuickHatch GUI running at:[/bold green] [cyan]{url}[/cyan]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down...[/dim]")
        server.shutdown()


def _print_json(payload: object) -> None:
    console.print(json.dumps(payload, indent=2, sort_keys=True, default=str), markup=False)


def _run_debug_command(args) -> None:
    from quickhatch.setup.debugtools import (
        adapter_spine_payload,
        classify_debug_payload,
        fuzz_harness_payload,
        harness_health_payload,
        load_facts_payload,
        preflight_debug_payload,
    )
    from quickhatch.setup.replay import (
        find_latest_runlog,
        promote_regression_fixture,
        scan_runlogs,
        summarize_runlog,
        summarize_stage,
    )

    if args.debug_command == "replay-run":
        summary = summarize_runlog(Path(args.runlog))
        payload = asdict(summary)
        if args.summary_only:
            _print_json(payload)
        else:
            console.print("[bold]QuickHatch setup replay summary[/bold]")
            _print_json(payload)
        return
    if args.debug_command == "stage":
        _print_json(summarize_stage(Path(args.runlog), stage=args.stage, group_id=args.group))
        return
    if args.debug_command == "promote-regression":
        path = promote_regression_fixture(
            Path(args.runlog),
            event_id=args.event,
            name=args.name,
            output_dir=Path(args.output_dir),
        )
        print_success(f"Generated regression fixture: {path}")
        return
    if args.debug_command == "preflight":
        _print_json(
            preflight_debug_payload(
                args.shell_command,
                facts=load_facts_payload(args.facts),
                shadow_mode=bool(args.shadow),
            )
        )
        return
    if args.debug_command == "classify":
        _print_json(
            classify_debug_payload(
                command=args.failure_command,
                rc=args.rc,
                stdout=args.stdout,
                stderr=args.stderr,
                detail=args.detail,
            )
        )
        return
    if args.debug_command == "adapter-spine":
        _print_json(adapter_spine_payload(family=args.family, profile_path=args.profile))
        return
    if args.debug_command == "harness-health":
        payload = harness_health_payload(
            root=args.root,
            keep_artifacts=bool(args.keep_artifacts),
        )
        _print_json(payload)
        if not payload.get("success"):
            raise SystemExit(1)
        return
    if args.debug_command == "fuzz-harness":
        payload = fuzz_harness_payload()
        _print_json(payload)
        if not payload.get("success"):
            raise SystemExit(1)
        return
    if args.debug_command == "latest-run":
        runlog = find_latest_runlog(Path(args.root) if args.root else None)
        if runlog is None:
            raise SystemExit("No setup runlogs found.")
        summary = asdict(summarize_runlog(runlog))
        payload = {"path": str(runlog), "summary": summary}
        if args.summary_only:
            _print_json(payload)
        else:
            console.print("[bold]QuickHatch latest setup run[/bold]")
            _print_json(payload)
        return
    if args.debug_command == "scan-runlogs":
        _print_json(asdict(scan_runlogs(Path(args.root) if args.root else None, limit=args.limit)))
        return
    raise SystemExit("Missing debug subcommand.")


def _run_certify_command(args) -> None:
    from quickhatch.setup.certify import run_vm_certification

    if args.certify_command == "vm":
        result = run_vm_certification(
            family=args.family,
            iso=Path(args.iso),
            profile=Path(args.profile),
            backend=args.backend,
            dry_run=args.dry_run,
            ssh_user=args.ssh_user,
            ssh_key=args.ssh_key,
            target_disk=args.target_disk,
            post_reboot_user=args.post_reboot_user,
            require_post_reboot=not args.no_post_reboot,
        )
        _print_json(asdict(result))
        if not result.success and result.status == "blocked":
            raise SystemExit(1)
        return
    raise SystemExit("Missing certify subcommand.")


if __name__ == "__main__":
    main()
