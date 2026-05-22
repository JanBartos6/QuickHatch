"""Main CLI orchestrator for the QuickHatch migration wizard."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path

from quickhatch.config import (
    INSTALL_ENCRYPTION_VALUES,
    INSTALL_FILESYSTEM_VALUES,
    INSTALL_PROFILE_VALUES,
    REMOTE_SETUP_MODE_VALUES,
    UserProfile,
    normalize_setup_intent,
)
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

_DESTRUCTIVE_CONFIRMATION = "WIPE_AND_REINSTALL"
_PROVIDER_DEFAULTS = {
    "api_anthropic": {
        "kind": "anthropic",
        "endpoint": "https://api.anthropic.com",
    },
    "api_openai": {
        "kind": "openai-compat",
        "endpoint": "https://api.openai.com/v1",
    },
    "api_gemini": {
        "kind": "openai-compat",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "api_grok": {
        "kind": "openai-compat",
        "endpoint": "https://api.x.ai/v1",
    },
    "api_custom": {
        "kind": "openai-compat",
        "endpoint": "",
    },
    "local_ollama": {
        "kind": "ollama",
        "endpoint": "http://localhost:11434",
    },
    "local_lmstudio": {
        "kind": "openai-compat",
        "endpoint": "http://localhost:1234/v1",
    },
    "codex": {
        "kind": "",
        "endpoint": "",
    },
}


def _split_csv(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for item in str(value or "").split(","):
            item = item.strip()
            if item:
                result.append(item)
    return result


def _redacted_profile_dict(profile: UserProfile) -> dict:
    payload = asdict(profile)
    ai = payload.get("ai_config")
    if isinstance(ai, dict) and ai.get("api_key"):
        ai["api_key"] = "***SET***"
    if payload.get("default_account_password"):
        payload["default_account_password"] = "***REDACTED***"
    return payload


def _safe_profile_summary(profile: UserProfile) -> dict:
    return {
        "ai_provider": profile.ai_config.provider,
        "ai_endpoint": profile.ai_config.endpoint,
        "ai_model": profile.ai_config.model_name,
        "api_key_present": bool(profile.ai_config.api_key),
        "native_search": bool(profile.ai_config.native_search),
        "migration_type": profile.preferences.migration_type,
        "distro_preference": profile.preferences.distro_preference,
        "apps_to_install": list(profile.apps_to_install),
        "linux_ip": profile.linux_ip,
        "linux_user": profile.linux_user,
        "ssh_key_configured": bool(profile.ssh_public_key_path),
        "remote_setup_mode": profile.remote_setup_mode,
        "install_profile": profile.install_profile,
        "install_encryption": profile.install_encryption,
        "install_filesystem": profile.install_filesystem,
        "install_username": profile.install_username,
        "target_hardware_override_present": bool(profile.target_hardware_override.strip()),
    }


def _load_profile(path: str | None) -> UserProfile:
    if not path:
        return UserProfile.load_checkpoint() or UserProfile()
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not load profile {source}: {exc}") from exc
    profile = UserProfile.from_dict(payload)
    if profile is None:
        raise SystemExit(f"Could not load profile {source}: invalid profile payload")
    return profile


def _api_key_from_args(args) -> str | None:
    if getattr(args, "api_key", None):
        return str(args.api_key)
    env_name = getattr(args, "api_key_env", None)
    if env_name:
        return os.environ.get(env_name) or None
    return None


def _provider_endpoint(provider: str | None, endpoint: str | None) -> str:
    if endpoint:
        return endpoint.strip()
    spec = _PROVIDER_DEFAULTS.get(provider or "")
    return str((spec or {}).get("endpoint") or "")


def _apply_profile_cli_args(
    profile: UserProfile, args, *, allow_runtime_secrets: bool
) -> UserProfile:
    if getattr(args, "ai_provider", None):
        profile.ai_config.provider = args.ai_provider
        if not getattr(args, "ai_endpoint", None):
            profile.ai_config.endpoint = _provider_endpoint(args.ai_provider, None) or None
    if getattr(args, "ai_endpoint", None) is not None:
        profile.ai_config.endpoint = args.ai_endpoint or None
    if getattr(args, "ai_model", None) is not None:
        profile.ai_config.model_name = args.ai_model or None
    if getattr(args, "native_search", None) is not None:
        profile.ai_config.native_search = bool(args.native_search)
    if allow_runtime_secrets:
        api_key = _api_key_from_args(args)
        if api_key:
            profile.ai_config.api_key = api_key
            if getattr(args, "remember_api_key", False):
                from quickhatch.secrets import store_api_key

                store_api_key(profile.ai_config.provider, profile.ai_config.endpoint, api_key)

    if getattr(args, "migration_type", None) is not None:
        profile.preferences.migration_type = args.migration_type or ""
    if getattr(args, "distro", None) is not None:
        profile.preferences.distro_preference = args.distro or ""
    if getattr(args, "desktop_look", None) is not None:
        profile.preferences.desktop_look = args.desktop_look or ""
    if getattr(args, "additional_notes", None) is not None:
        profile.preferences.additional_notes = args.additional_notes or ""
    if getattr(args, "target_hardware_override", None) is not None:
        profile.target_hardware_override = args.target_hardware_override or ""

    apps = _split_csv(getattr(args, "app", None))
    if apps:
        profile.apps_to_install = apps
    if getattr(args, "data_description", None) is not None:
        profile.data_description = args.data_description or ""
    if getattr(args, "data_transfer_method", None) is not None:
        profile.data_transfer_method = args.data_transfer_method or ""
    if getattr(args, "linux_host", None) is not None:
        profile.linux_ip = args.linux_host or ""
    if getattr(args, "linux_user", None) is not None:
        profile.linux_user = args.linux_user or ""
    if getattr(args, "install_username", None) is not None:
        profile.install_username = args.install_username or ""
    if getattr(args, "ssh_key", None) is not None:
        profile.ssh_public_key_path = args.ssh_key or None

    setup_intent_changed = (
        getattr(args, "remote_setup_mode", None) is not None
        or getattr(args, "install_profile", None) is not None
    )
    if getattr(args, "remote_setup_mode", None) is not None:
        profile.remote_setup_mode = args.remote_setup_mode
        if (
            args.remote_setup_mode == "wipe_and_reinstall"
            and getattr(args, "install_profile", None) is None
            and profile.install_profile in {"", "configure_existing"}
        ):
            profile.install_profile = "auto"
        elif (
            args.remote_setup_mode == "configure_only"
            and getattr(args, "install_profile", None) is None
        ):
            profile.install_profile = "configure_existing"
    if getattr(args, "install_profile", None) is not None:
        profile.install_profile = args.install_profile
    if setup_intent_changed:
        profile.remote_setup_mode, profile.install_profile = normalize_setup_intent(
            profile.remote_setup_mode,
            profile.install_profile,
        )
    if getattr(args, "install_encryption", None) is not None:
        profile.install_encryption = args.install_encryption
    if getattr(args, "install_filesystem", None) is not None:
        profile.install_filesystem = args.install_filesystem
    if getattr(args, "install_partition_notes", None) is not None:
        profile.install_partition_notes = args.install_partition_notes or ""
    if getattr(args, "export_executed_setup_script", None) is not None:
        profile.export_executed_setup_script = bool(args.export_executed_setup_script)
    return profile


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


def _add_common_profile_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from-profile", dest="profile_path", help="Load an existing profile JSON")
    parser.add_argument(
        "--ai-provider",
        choices=sorted(_PROVIDER_DEFAULTS),
        help="AI provider label, e.g. api_gemini, api_openai, local_lmstudio",
    )
    parser.add_argument("--ai-endpoint", help="AI provider base URL")
    parser.add_argument("--ai-model", help="AI model id to use")
    parser.add_argument("--api-key", help=argparse.SUPPRESS)
    parser.add_argument("--api-key-env", help="Environment variable containing the API key")
    parser.add_argument(
        "--remember-api-key",
        action="store_true",
        help="Store the API key in the OS credential store when possible",
    )
    native = parser.add_mutually_exclusive_group()
    native.add_argument("--native-search", dest="native_search", action="store_true")
    native.add_argument("--no-native-search", dest="native_search", action="store_false")
    parser.set_defaults(native_search=None)
    parser.add_argument(
        "--migration-type",
        choices=["fresh_install", "customize_linux"],
        help="Overall workflow mode",
    )
    parser.add_argument("--distro", help="Preferred Linux family/distro")
    parser.add_argument("--desktop-look", help="Desktop look/style preference")
    parser.add_argument("--app", action="append", help="App to request; repeat or comma-separate")
    parser.add_argument("--data-description", help="Data migration notes")
    parser.add_argument(
        "--data-transfer-method",
        choices=["external_drive", "cloud", "keep_windows", "nothing", ""],
        help="How data will be moved",
    )
    parser.add_argument("--additional-notes", help="Additional user notes")
    parser.add_argument("--target-hardware-override", help="Target hardware notes")
    parser.add_argument("--linux-host", help="Linux SSH host or host:port")
    parser.add_argument("--linux-user", help="Linux SSH username")
    parser.add_argument("--ssh-key", help="SSH private/public key path for setup")
    parser.add_argument("--install-username", help="User to create/use after install")
    parser.add_argument(
        "--remote-setup-mode",
        choices=sorted(REMOTE_SETUP_MODE_VALUES),
        help="Remote setup mode",
    )
    parser.add_argument(
        "--install-profile",
        choices=sorted(INSTALL_PROFILE_VALUES),
        help="Install target profile",
    )
    parser.add_argument(
        "--install-encryption",
        choices=sorted(INSTALL_ENCRYPTION_VALUES),
        help="Install encryption preference",
    )
    parser.add_argument(
        "--install-filesystem",
        choices=sorted(INSTALL_FILESYSTEM_VALUES),
        help="Install filesystem preference",
    )
    parser.add_argument("--install-partition-notes", help="Partitioning notes")
    parser.add_argument(
        "--export-executed-setup-script",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Export a redacted replay script from executed setup commands",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the QuickHatch command line parser."""
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
    debug_subparsers.add_parser(
        "catalog-health",
        help="Run advisory app-catalog contract validation",
    )
    smoke_parser = debug_subparsers.add_parser(
        "release-smoke",
        help="Run local non-VM release readiness checks",
    )
    smoke_parser.add_argument(
        "--full-pytest",
        action="store_true",
        help="Run the full pytest suite instead of the targeted smoke subset.",
    )
    smoke_parser.add_argument(
        "--skip-certification-dry-runs",
        action="store_true",
        help="Skip local certification dry-runs.",
    )
    smoke_parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-check timeout in seconds.",
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
    support_parser = debug_subparsers.add_parser(
        "support-bundle",
        aliases=["export-support-bundle"],
        help="Write a redacted support ZIP for a setup run",
    )
    support_parser.add_argument("--runlog", help="Specific setup run JSONL to bundle")
    support_parser.add_argument("--root", help="Setup runlog root to scan for the latest run")
    support_parser.add_argument(
        "--out",
        help="Exact .zip path or output directory for the generated support bundle",
    )
    support_parser.add_argument("--output-dir", help="Directory for the generated support ZIP")

    models_parser = subparsers.add_parser("models", help="List and choose provider models")
    models_subparsers = models_parser.add_subparsers(dest="models_command")
    models_list = models_subparsers.add_parser(
        "list",
        aliases=["resolve"],
        help="Fetch models and optionally choose a preferred Gemini Flash model",
    )
    models_list.add_argument(
        "--provider",
        required=True,
        choices=sorted(_PROVIDER_DEFAULTS),
        help="Provider label",
    )
    models_list.add_argument("--endpoint", help="Provider endpoint override")
    models_list.add_argument("--api-key", help=argparse.SUPPRESS)
    models_list.add_argument("--api-key-env", help="Environment variable containing the API key")
    models_list.add_argument(
        "--prefer-gemini-flash-lite",
        action="store_true",
        help="Select Gemini 3.1 Flash Lite if present, otherwise the closest Flash Lite/Flash model",
    )
    models_list.add_argument(
        "--prefer", help="Preferred model text/id to resolve from fetched models"
    )

    profile_parser = subparsers.add_parser("profile", help="Build CLI setup profiles")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command")
    profile_write = profile_subparsers.add_parser(
        "write",
        aliases=["build"],
        help="Write a QuickHatch profile JSON",
    )
    profile_write.add_argument("--out", required=True, help="Destination profile JSON")
    _add_common_profile_args(profile_write)

    setup_parser = subparsers.add_parser("setup", help="Run remote setup from the CLI")
    setup_subparsers = setup_parser.add_subparsers(dest="setup_command")
    setup_run = setup_subparsers.add_parser(
        "run",
        help="Launch the same remote setup backend used by the GUI",
    )
    _add_common_profile_args(setup_run)
    setup_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved setup profile and safety state without connecting over SSH",
    )
    setup_run.add_argument(
        "--confirm-destructive-setup",
        default="",
        help=f"Required token for wipe/reinstall setup mode: {_DESTRUCTIVE_CONFIRMATION}",
    )

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
    vm_parser.add_argument("--root", default=".quickhatch/certification")
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
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the QuickHatch migration wizard."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.quickhatch_command == "debug":
        return _run_debug_command(args)

    if args.quickhatch_command == "certify":
        return _run_certify_command(args)

    if args.quickhatch_command == "models":
        return _run_models_command(args)

    if args.quickhatch_command == "profile":
        return _run_profile_command(args)

    if args.quickhatch_command == "setup":
        return _run_setup_command(args)

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


def _model_kind(provider: str) -> str:
    return str(_PROVIDER_DEFAULTS.get(provider, {}).get("kind") or "")


def _model_endpoint(provider: str, endpoint: str | None) -> str:
    return _provider_endpoint(provider, endpoint)


def _model_score(model: str, *, provider: str, prefer: str | None, prefer_gemini: bool) -> int:
    lowered = model.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    score = 0
    if prefer:
        preferred = prefer.lower()
        preferred_compact = "".join(ch for ch in preferred if ch.isalnum())
        if lowered == preferred or compact == preferred_compact:
            score += 1000
        elif preferred_compact and preferred_compact in compact:
            score += 800
        else:
            tokens = [token for token in re_split_model_tokens(preferred) if token]
            score += sum(40 for token in tokens if token in lowered)
    if prefer_gemini or provider == "api_gemini":
        if "gemini" in lowered:
            score += 120
        if "3.1" in lowered or "31" in compact:
            score += 260
        if "flash" in lowered:
            score += 180
        if "lite" in lowered:
            score += 160
        if "flash-lite" in lowered or ("flash" in lowered and "lite" in lowered):
            score += 160
        if "pro" in lowered:
            score -= 30
        if "embed" in lowered:
            score -= 500
    return score


def re_split_model_tokens(text: str) -> list[str]:
    import re

    return re.split(r"[^a-z0-9.]+", text.lower())


def _select_preferred_model(
    models: list[str],
    *,
    provider: str,
    prefer: str | None = None,
    prefer_gemini_flash_lite: bool = False,
) -> tuple[str | None, str]:
    candidates = [model for model in models if isinstance(model, str) and model.strip()]
    if not candidates:
        return None, "no models returned"
    if prefer_gemini_flash_lite and not prefer:
        prefer = "Gemini 3.1 Flash Lite"
    if not prefer and not prefer_gemini_flash_lite:
        return None, "no preferred model requested"
    if prefer:
        preferred = prefer.lower()
        preferred_compact = "".join(ch for ch in preferred if ch.isalnum())
        for model in candidates:
            lowered = model.lower()
            compact = "".join(ch for ch in lowered if ch.isalnum())
            if lowered == preferred or compact == preferred_compact:
                return model, "selected exact fetched model"
    ranked = sorted(
        candidates,
        key=lambda model: (
            _model_score(
                model,
                provider=provider,
                prefer=prefer,
                prefer_gemini=prefer_gemini_flash_lite,
            ),
            -len(model),
        ),
        reverse=True,
    )
    selected = ranked[0]
    score = _model_score(
        selected,
        provider=provider,
        prefer=prefer,
        prefer_gemini=prefer_gemini_flash_lite,
    )
    if score <= 0:
        return None, "no fetched model matched the requested preference"
    if prefer_gemini_flash_lite and selected.lower() != "gemini-3.1-flash-lite":
        return (
            selected,
            "Gemini 3.1 Flash Lite was not exact; selected closest Flash Lite/Flash model",
        )
    return selected, "selected preferred fetched model"


def _run_models_command(args) -> None:
    if args.models_command not in {"list", "resolve"}:
        raise SystemExit("Missing models subcommand.")
    provider = args.provider
    endpoint = _model_endpoint(provider, args.endpoint)
    kind = _model_kind(provider)
    if not kind:
        raise SystemExit(f"Provider {provider} does not expose a model-list endpoint.")
    api_key = _api_key_from_args(args)
    if not api_key:
        from quickhatch.secrets import load_api_key

        api_key = load_api_key(provider, endpoint)
    from quickhatch.ai.model_list import fetch

    models = fetch(kind, endpoint, api_key)
    selected, reason = _select_preferred_model(
        models,
        provider=provider,
        prefer=args.prefer,
        prefer_gemini_flash_lite=bool(args.prefer_gemini_flash_lite),
    )
    payload = {
        "status": "ok" if models else "no_models",
        "provider": provider,
        "kind": kind,
        "endpoint": endpoint,
        "api_key_present": bool(api_key),
        "models": models,
        "selected_model": selected,
        "selection_reason": reason,
    }
    _print_json(payload)
    if not models:
        raise SystemExit(1)


def _run_profile_command(args) -> None:
    if args.profile_command not in {"write", "build"}:
        raise SystemExit("Missing profile subcommand.")
    profile = _load_profile(args.profile_path)
    _apply_profile_cli_args(profile, args, allow_runtime_secrets=True)
    output = Path(args.out)
    path = profile.save_checkpoint(output)
    _print_json(
        {
            "status": "ok",
            "path": str(path),
            "profile_summary": _safe_profile_summary(profile),
            "profile": _redacted_profile_dict(profile),
        }
    )


def _is_destructive_setup_profile(profile: UserProfile) -> bool:
    return profile.remote_setup_mode == "wipe_and_reinstall" or profile.install_profile not in {
        "auto",
        "configure_existing",
    }


def _destructive_confirmation_error() -> str:
    return (
        "Destructive setup mode requires explicit CLI confirmation. Re-run with "
        f"`--confirm-destructive-setup {_DESTRUCTIVE_CONFIRMATION}` after verifying this is a "
        "live/sacrificial target. Disk erase/format operations will still require per-disk "
        "QuickHatch capability confirmation during setup."
    )


def _setup_payload(
    *,
    profile: UserProfile,
    bridge_setup_run: dict,
    summary: str,
    dry_run: bool = False,
) -> dict:
    final_status = str(bridge_setup_run.get("final_status") or bridge_setup_run.get("status") or "")
    return {
        "status": bridge_setup_run.get("status") or ("dry_run" if dry_run else "unknown"),
        "final_status": final_status,
        "summary": summary,
        "run_id": bridge_setup_run.get("run_id", ""),
        "runlog_path": bridge_setup_run.get("runlog_path", ""),
        "blockers": bridge_setup_run.get("blockers", []),
        "waiting_for_input": bool(bridge_setup_run.get("waiting_for_input")),
        "profile_summary": _safe_profile_summary(profile),
        "destructive_mode_armed": _is_destructive_setup_profile(profile),
        "dry_run": dry_run,
    }


def _attach_cli_question_handler(bridge) -> None:
    def _answer(question) -> None:
        console.print(f"\n[bold yellow]Setup needs input:[/bold yellow] {question.question}")
        choices = list(question.choices or [])
        if choices:
            for index, choice in enumerate(choices, start=1):
                console.print(f"  {index}. {choice}")
        try:
            raw = input("Answer: ").strip()
        except EOFError:
            raw = ""
        answer = raw
        if choices and raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(choices):
                answer = choices[idx]
        bridge.answer_question(question.id, answer)

    bridge.on_question(_answer)


def _run_setup_agent_cli(profile: UserProfile) -> dict:
    from quickhatch.bridge.server import BridgeState
    from quickhatch.gui.server import GUIState, _build_setup_runtime
    from quickhatch.setup import SetupOrchestrator

    bridge = BridgeState(profile, persist_path=None)
    _attach_cli_question_handler(bridge)
    gui_state = GUIState(profile, bridge)
    runtime = _build_setup_runtime(gui_state, port=0)
    if not runtime.host or not runtime.user:
        raise SystemExit(
            "Setup requires --linux-host and --linux-user or a profile containing them."
        )
    summary = SetupOrchestrator(runtime).run()
    payload = _setup_payload(
        profile=profile,
        bridge_setup_run=bridge.snapshot_setup_run(),
        summary=summary,
    )
    return payload


def _run_setup_command(args) -> None:
    if args.setup_command != "run":
        raise SystemExit("Missing setup subcommand.")
    profile = _load_profile(args.profile_path)
    _apply_profile_cli_args(profile, args, allow_runtime_secrets=True)

    if (
        _is_destructive_setup_profile(profile)
        and (args.confirm_destructive_setup or "") != _DESTRUCTIVE_CONFIRMATION
    ):
        raise SystemExit(_destructive_confirmation_error())

    if args.dry_run:
        _print_json(
            _setup_payload(
                profile=profile,
                bridge_setup_run={"status": "dry_run", "final_status": ""},
                summary="Resolved CLI setup profile; no SSH connection or setup actions executed.",
                dry_run=True,
            )
        )
        return

    payload = _run_setup_agent_cli(profile)
    _print_json(payload)
    if payload.get("status") in {"blocked", "failed"} or payload.get("final_status") == "blocked":
        raise SystemExit(1)


def _run_debug_command(args) -> None:
    from quickhatch.setup.debugtools import (
        adapter_spine_payload,
        catalog_health_payload,
        classify_debug_payload,
        fuzz_harness_payload,
        harness_health_payload,
        load_facts_payload,
        preflight_debug_payload,
        release_smoke_payload,
        support_bundle_payload,
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
    if args.debug_command == "catalog-health":
        _print_json(catalog_health_payload())
        return
    if args.debug_command == "release-smoke":
        payload = release_smoke_payload(
            full_pytest=bool(args.full_pytest),
            include_certification=not bool(args.skip_certification_dry_runs),
            timeout=args.timeout,
        )
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
    if args.debug_command in {"support-bundle", "export-support-bundle"}:
        _print_json(
            support_bundle_payload(
                runlog=args.runlog,
                root=args.root,
                output_dir=args.output_dir,
                out=args.out,
            )
        )
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
            root=args.root,
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
