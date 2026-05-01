"""Preflight guards for remote setup commands.

These checks catch stable, high-confidence mistakes before a staged command
hits SSH. They intentionally avoid fast-changing package truth; exact package
resolution belongs to the planner/search phase.
"""

from __future__ import annotations

import re
import shlex

from .resolver import arch_nvidia_official_package_supported
from .types import SetupFacts

_ARCH_AUR_ONLY_PACKAGES = frozenset(
    {
        "brave-bin",
        "github-desktop-bin",
        "cursor-bin",
        "lm-studio-bin",
        "mullvad-vpn-bin",
        "claude-desktop-bin",
        "chatgpt-desktop-bin",
        "codex-bin",
        "localsend-bin",
        "stirling-pdf-bin",
        "docker-desktop",
        "catppuccin-kde-theme-git",
        "catppuccin-gtk-theme-mocha",
        "sweet-rainbow-icon-theme-git",
        "bibata-cursor-theme",
    }
)
_AURISH_SUFFIXES = ("-bin", "-git")
_ARCH_BROAD_PACKAGE_GROUPS = frozenset(
    {
        "kde-applications",
    }
)
_NETWORK_SECURITY_SERVICE_TOKENS = frozenset(
    {
        "mullvad-daemon",
        "mullvad-vpn",
        "openvpn",
        "wg-quick",
        "wg-quick@",
        "wireguard",
        "firewalld",
        "ufw",
        "nftables",
        "iptables",
    }
)
_PACKAGE_PROCESS_NAMES = frozenset(
    {
        "apt",
        "apt-get",
        "apt.systemd.daily",
        "debconf",
        "dpkg",
        "packagekit",
        "packagekitd",
        "snapd",
        "unattended-upgrade",
        "unattended-upgrades",
    }
)
_UBUNTU_DESKTOP_METAPACKAGES = frozenset({"ubuntu-desktop", "ubuntu-desktop-minimal"})
_ROOT_WRITE_PREFIXES = (
    "/etc/",
    "/boot/",
    "/usr/",
    "/var/lib/",
    "/var/cache/pacman/",
    "/opt/",
)
_ROOT_WRITE_EXACT_PATHS = {"/etc", "/boot", "/usr", "/opt"}
_ROOT_WRITE_TOOLS = {
    "chmod",
    "chown",
    "cp",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "sed",
    "tee",
    "touch",
}
_ROOT_REQUIRED_PACKAGE_TOOLS = {
    "pacman",
    "apt",
    "apt-get",
    "dnf",
    "zypper",
    "apk",
    "xbps-install",
    "emerge",
}
_ROOT_REQUIRED_MUTATION_TOOLS = {
    "arch-chroot",
    "blkdiscard",
    "bootctl",
    "chroot",
    "debootstrap",
    "dracut",
    "fdisk",
    "genfstab",
    "grub-install",
    "grub-mkconfig",
    "grub2-install",
    "grub2-mkconfig",
    "mount",
    "parted",
    "partprobe",
    "partx",
    "sfdisk",
    "sgdisk",
    "swapoff",
    "swapon",
    "umount",
    "update-grub",
    "update-initramfs",
    "wipefs",
}
_ASSIGNMENT_NAME = r"[A-Za-z_][A-Za-z0-9_]*"


def _split_command_segments(cmd: str) -> list[str]:
    """Split top-level shell command chains without entering quoted payloads."""

    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    text = cmd or ""
    idx = 0
    while idx < len(text):
        char = text[idx]
        if escaped:
            current.append(char)
            escaped = False
            idx += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            idx += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            idx += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            idx += 1
            continue
        if text.startswith("&&", idx) or text.startswith("||", idx):
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            idx += 2
            continue
        if char == ";":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            idx += 1
            continue
        current.append(char)
        idx += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _normalize_tool(token: str) -> str:
    return (token or "").strip().split("/")[-1].lower()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if "=" in token and not token.startswith("-") and token.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        if token == "env":
            idx += 1
            continue
        if token == "sudo":
            idx += 1
            while idx < len(tokens):
                opt = tokens[idx]
                if opt in {"-u", "-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
                    idx += 2
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        break
    return tokens[idx:]


def _shell_c_payload(tokens: list[str]) -> str:
    for idx, token in enumerate(tokens[1:], start=1):
        if token == "-c":
            return tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if token.startswith("-") and "c" in token and idx + 1 < len(tokens):
            return tokens[idx + 1]
    return ""


def _arch_community_helpers_from_tokens(tokens: list[str]) -> set[str]:
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return set()
    tool = _normalize_tool(tokens[0])
    if tool in {"yay", "paru"}:
        return {tool}
    if tool in {"arch-chroot", "chroot"} and len(tokens) >= 3:
        return _arch_community_helpers_from_tokens(tokens[2:])
    if tool in {"bash", "sh", "zsh", "fish"}:
        payload = _shell_c_payload(tokens)
        return arch_community_helpers_in_command(payload) if payload else set()
    if tool == "runuser":
        if "--" in tokens:
            return _arch_community_helpers_from_tokens(tokens[tokens.index("--") + 1 :])
        payload = _shell_c_payload(tokens)
        return arch_community_helpers_in_command(payload) if payload else set()
    if tool == "su":
        payload = _shell_c_payload(tokens)
        return arch_community_helpers_in_command(payload) if payload else set()
    return set()


def arch_community_helpers_in_command(command: str) -> set[str]:
    helpers: set[str] = set()
    for segment in _split_command_segments(command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        helpers.update(_arch_community_helpers_from_tokens(tokens))
    return helpers


def _has_root_privilege(tokens: list[str], facts: SetupFacts) -> bool:
    if facts.is_root or (facts.current_user or facts.login_user) == "root":
        return True
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if "=" in token and not token.startswith("-") and token.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        if token == "env":
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return False
    if tokens[idx] == "doas":
        return True
    if tokens[idx] != "sudo":
        return False
    idx += 1
    while idx < len(tokens):
        opt = tokens[idx]
        if opt in {"-u", "--user"}:
            return idx + 1 < len(tokens) and tokens[idx + 1] == "root"
        if opt.startswith("--user="):
            return opt == "--user=root"
        if opt in {"-g", "-h", "-D", "-p", "-r", "-t", "-T", "-C", "-R"}:
            idx += 2
            continue
        if opt.startswith("-"):
            idx += 1
            continue
        return True
    return True


def _is_root_owned_path(token: str) -> bool:
    path = token.strip().strip("'\"")
    if not path:
        return False
    if path in _ROOT_WRITE_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _ROOT_WRITE_PREFIXES)


def _root_owned_redirection_target(command: str) -> str | None:
    quote: str | None = None
    escaped = False
    idx = 0
    while idx < len(command):
        char = command[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if char == "\\":
            escaped = True
            idx += 1
            continue
        if quote:
            if char == quote:
                quote = None
            idx += 1
            continue
        if char in {"'", '"'}:
            quote = char
            idx += 1
            continue
        if char == ">":
            op_len = 2 if idx + 1 < len(command) and command[idx + 1] == ">" else 1
            rest = command[idx + op_len :].strip()
            if not rest or rest.startswith("&"):
                idx += op_len
                continue
            try:
                target = shlex.split(rest, posix=True)[0]
            except (IndexError, ValueError):
                target = rest.split(maxsplit=1)[0] if rest else ""
            if _is_root_owned_path(target):
                return target
            idx += op_len
            continue
        idx += 1
    return None


def _mutates_root_owned_path(tool: str, tokens: list[str]) -> bool:
    if tool not in _ROOT_WRITE_TOOLS:
        return False
    if tool == "sed" and not any(arg.startswith("-i") for arg in tokens[1:]):
        return False
    if tool == "tee":
        path_args = [arg for arg in tokens[1:] if not arg.startswith("-")]
        return any(_is_root_owned_path(arg) for arg in path_args)
    return any(_is_root_owned_path(arg) for arg in tokens[1:])


def _extract_pacman_install_packages(tokens: list[str]) -> list[str]:
    if not tokens or _normalize_tool(tokens[0]) not in {"pacman", "yay", "paru"}:
        return []
    args = tokens[1:]
    if not any(arg.startswith("-S") and arg not in {"-Ss", "-Si"} for arg in args):
        return []
    return [arg for arg in args if arg and not arg.startswith("-")]


def _extract_apt_install_packages(tokens: list[str]) -> list[str]:
    if not tokens or _normalize_tool(tokens[0]) not in {"apt", "apt-get"}:
        return []
    args = tokens[1:]
    try:
        install_index = next(idx for idx, arg in enumerate(args) if arg.lower() == "install")
    except StopIteration:
        return []
    packages: list[str] = []
    skip_next = False
    for arg in args[install_index + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-o", "--option", "-c", "--config-file"}:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        if "=" in arg and not arg.startswith("./"):
            continue
        packages.append(arg)
    return packages


def _package_tool_mutates_system(tool: str, tokens: list[str]) -> bool:
    """Return True only for package-manager actions that write system state."""

    args = [arg for arg in tokens[1:] if arg and arg != "--"]
    if tool == "pacman":
        for arg in args:
            if not arg.startswith("-"):
                continue
            # Read-only sync/query probes are safe without root.
            if arg in {"-Q", "-Qs", "-Qi", "-Ql", "-Qo", "-Qq", "-Ss", "-Si", "-Sl"}:
                continue
            if arg.startswith("-Q"):
                continue
            if arg in {"-S", "-Sy", "-Syu", "-Syyu", "-R", "-Rs", "-Rns", "-U"}:
                return True
            if arg.startswith(("-S", "-R", "-U")):
                return True
        return False
    if tool in {"apt", "apt-get"}:
        mutating_verbs = {
            "autoremove",
            "clean",
            "dist-upgrade",
            "full-upgrade",
            "install",
            "purge",
            "remove",
            "upgrade",
            "update",
        }
        return any(arg.lower() in mutating_verbs for arg in args if not arg.startswith("-"))
    if tool == "dnf":
        mutating_verbs = {
            "autoremove",
            "distro-sync",
            "groupinstall",
            "install",
            "makecache",
            "remove",
            "swap",
            "update",
            "upgrade",
        }
        return any(arg.lower() in mutating_verbs for arg in args if not arg.startswith("-"))
    if tool == "zypper":
        mutating_verbs = {
            "addrepo",
            "dup",
            "in",
            "install",
            "modifyrepo",
            "patch",
            "refresh",
            "remove",
            "rm",
            "up",
            "update",
        }
        return any(arg.lower() in mutating_verbs for arg in args if not arg.startswith("-"))
    if tool == "apk":
        mutating_verbs = {"add", "del", "fix", "update", "upgrade"}
        return any(arg.lower() in mutating_verbs for arg in args if not arg.startswith("-"))
    if tool == "emerge":
        return not any(arg in {"--search", "-s", "--info", "--pretend", "-p"} for arg in args)
    return tool == "xbps-install"


def _mount_command_requires_root(tokens: list[str]) -> bool:
    args = tokens[1:]
    if not args:
        return False
    if "|" in args:
        pipe_index = args.index("|")
        pre_pipe_args = [
            arg
            for arg in args[:pipe_index]
            if arg not in {"-l", "--show-labels", "-v", "--verbose"}
        ]
        return bool(pre_pipe_args)
    if all(arg in {"-l", "--show-labels", "-v", "--verbose"} for arg in args):
        return False
    return True


def _tool_requires_root(tool: str, tokens: list[str]) -> bool:
    if not tool:
        return False
    if tool == "mount":
        return _mount_command_requires_root(tokens)
    if tool in _ROOT_REQUIRED_MUTATION_TOOLS:
        return True
    if tool.startswith("mkfs"):
        return True
    return False


def _uses_fake_plasma_session_wrapper(cmd: str) -> bool:
    lower = (cmd or "").lower()
    return "dbus-run-session" in lower and (
        "plasma-apply-lookandfeel" in lower or "plasma-apply-wallpaperimage" in lower
    )


def _uses_fedora_live_anaconda_from_ssh(cmd: str, facts: SetupFacts) -> bool:
    if facts.package_manager != "dnf" or not facts.is_live:
        return False
    lower = (cmd or "").lower()
    if not any(token in lower for token in ("anaconda", "liveinst")):
        return False
    return any(
        token in lower
        for token in (
            "--kickstart",
            "--ks",
            " --ks ",
            "--liveinst",
            "--noninteractive",
            "--cmdline",
            " -c ",
        )
    )


def _uses_ubuntu_live_interactive_installer_from_ssh(cmd: str, facts: SetupFacts) -> bool:
    if facts.package_manager != "apt" or not facts.is_live:
        return False
    os_text = " ".join((facts.os_id or "", facts.os_like or "", facts.distro_family or "")).lower()
    if "ubuntu" not in os_text and "debian" not in os_text:
        return False
    lower = (cmd or "").lower()
    if not any(
        token in lower
        for token in (
            "ubiquity",
            "ubuntu-desktop-bootstrap",
            "ubuntu_bootstrap",
            "subiquity",
        )
    ):
        return False
    return "autoinstall" not in lower and "--automation-file" not in lower


def _is_standalone_shell_assignment(command: str) -> bool:
    stripped = (command or "").strip()
    if not stripped:
        return False
    if re.fullmatch(rf"(?:export\s+)?{_ASSIGNMENT_NAME}=\$\(.+\)", stripped):
        return True
    if re.fullmatch(rf"(?:export\s+)?(?:{_ASSIGNMENT_NAME}=\S+\s*)+", stripped):
        return True
    return False


def _activates_network_security_services(cmd: str) -> bool:
    for segment in _split_command_segments(cmd):
        lower_segment = segment.lower()
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = lower_segment.split()
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_tool(tokens[0])
        args = [token.lower() for token in tokens[1:]]
        if tool == "systemctl" and args:
            verb = args[0]
            if verb in {"enable", "start", "restart", "try-restart"} and any(
                any(token.startswith(service) for service in _NETWORK_SECURITY_SERVICE_TOKENS)
                for token in args[1:]
            ):
                return True
        if tool == "ufw" and args and args[0] in {"enable", "reload"}:
            return True
        if tool in {"firewall-cmd", "nft", "iptables", "ip6tables"}:
            if any(word in lower_segment for word in ("enable", "reload", "default", "policy")):
                return True
    return False


def _uses_broad_package_process_kill(cmd: str) -> bool:
    for segment in _split_command_segments(cmd):
        lower_segment = segment.lower()
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = lower_segment.split()
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_tool(tokens[0])
        if tool not in {"pkill", "killall"}:
            continue
        args = [token.lower() for token in tokens[1:]]
        haystack = " ".join(args)
        if not any(name in haystack for name in _PACKAGE_PROCESS_NAMES):
            continue
        if tool == "killall":
            return True
        if "-f" in args or "--full" in args or "|" in haystack or ".*" in haystack:
            return True
    return False


def preflight_setup_command(
    command: str,
    facts: SetupFacts,
    *,
    allow_network_security_changes: bool = False,
) -> str | None:
    """Return a blocking reason for clearly unsafe/impossible setup commands."""

    package_manager = facts.package_manager or "unknown"
    community_helper = facts.community_helper or "none"
    if _is_standalone_shell_assignment(command):
        return (
            "This command only assigns shell variable(s). QuickHatch runs each command in a "
            "fresh SSH shell, so variables do not persist to later commands. Combine the "
            "assignment with the command that uses it, e.g. `UCODE=$(...) && pacstrap ... "
            '"$UCODE"`.'
        )
    if not allow_network_security_changes and _activates_network_security_services(command):
        return (
            "Do not enable/start VPN or firewall services automatically. Installing the package "
            "is fine, but activating VPN/firewall services can change routing, DNS, firewall "
            "policy, or kill-switch behavior and cut off remote access."
        )
    if _uses_broad_package_process_kill(command):
        return (
            "Do not use broad `pkill`/`killall` patterns against apt/dpkg/debconf/snapd/"
            "PackageKit. They can kill the current SSH command or leave dpkg half-written. "
            "Stop specific services/timers, inspect the lock holder, wait for package work to "
            "finish, or kill only a confirmed stale PID."
        )
    if _uses_fake_plasma_session_wrapper(command):
        return (
            "Do not use `dbus-run-session` to apply KDE/Plasma theming. It creates a throwaway "
            "bus, so live apply commands do not affect the user's real desktop session. Use the "
            "logged-in user's actual session bus or write persisted config and require relogin."
        )
    if _uses_ubuntu_live_interactive_installer_from_ssh(command, facts):
        return (
            "Do not launch Ubuntu's graphical/live installer interactively over SSH. Use the "
            "Debian-family debootstrap path for full live installs, or provide a current, "
            "noninteractive, evidence-backed Subiquity/autoinstall invocation."
        )
    if _uses_fedora_live_anaconda_from_ssh(command, facts):
        return (
            "Do not launch Fedora live Anaconda/Kickstart as a normal SSH command. On live "
            "Workstation ISOs local `--kickstart` is not supported for live installations and "
            "text/cmdline runs can block on installer spokes with no stdin. Ask the user to run "
            "the graphical installer manually, reboot, rerun quickstart.sh, and continue in "
            "configure-only mode."
        )

    for segment in _split_command_segments(command):
        try:
            raw_tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        has_root_privilege = _has_root_privilege(raw_tokens, facts)
        tokens = list(raw_tokens)
        tokens = _strip_wrappers(tokens)
        if not tokens:
            continue
        tool = _normalize_tool(tokens[0])
        if tool in {"runuser", "su"} and not has_root_privilege:
            return (
                f"`{tool}` may not be used by a non-root setup session without an "
                "interactive password prompt. If the target user is already the SSH user, "
                "run the inner command directly; otherwise use `sudo -u ...` only when "
                "NOPASSWD sudo is available."
            )
        redirected_target = _root_owned_redirection_target(segment)
        if redirected_target and not has_root_privilege:
            return (
                f"`{segment}` redirects shell output into root-owned path "
                f"`{redirected_target}` but the shell is not running as root. Shell "
                "redirection happens before `sudo`; use `sudo tee` or "
                "`sudo sh -c '...'` for root-owned config/system paths."
            )
        if _mutates_root_owned_path(tool, tokens) and not has_root_privilege:
            return (
                f"`{segment}` mutates a root-owned path but is not running as root. "
                "Use `sudo` for root-owned config/system paths, or run the command inside the "
                "installer chroot while the session is root."
            )
        if _tool_requires_root(tool, tokens) and not has_root_privilege:
            return (
                f"`{tool}` changes disks, mounts, boot files, or chroot state and needs root "
                "privileges. Use `sudo` unless the remote session is already root."
            )
        if (
            tool in _ROOT_REQUIRED_PACKAGE_TOOLS
            and _package_tool_mutates_system(tool, tokens)
            and not has_root_privilege
        ):
            return (
                f"`{tool}` changes system packages and needs root privileges. "
                "Use `sudo` unless the remote session is already root."
            )
        if tool == "pacstrap" and "--noconfirm" in tokens:
            return (
                "`pacstrap` does not accept `--noconfirm`; that option fails before the "
                "base install starts. Keep `pacstrap -K /mnt ...` and avoid ambiguous "
                "provider prompts by naming concrete provider packages when needed."
            )
        if package_manager == "pacman":
            if tool in {"apt", "apt-get", "dnf", "zypper"}:
                return (
                    f"This target is Arch-family (`pacman`), so `{tool}` is the wrong package "
                    "manager. Use `pacman` for repo packages and the AUR helper only for AUR."
                )
            helper_tools = arch_community_helpers_in_command(segment)
            if helper_tools and community_helper == "none":
                helper = sorted(helper_tools)[0]
                return (
                    f"The command uses `{helper}` but no AUR helper is installed yet. Install "
                    "the helper first, or build manually with `makepkg -s` + `pacman -U`."
                )
            mismatched_helpers = sorted(
                helper for helper in helper_tools if helper != community_helper
            )
            if mismatched_helpers and community_helper in {"yay", "paru"}:
                return (
                    f"The command uses `{mismatched_helpers[0]}`, but the detected AUR helper "
                    f"is `{community_helper}`. Use `{community_helper}` for AUR packages."
                )
            if tool == "pacman":
                packages = _extract_pacman_install_packages(tokens)
                broad_groups = [
                    package for package in packages if package in _ARCH_BROAD_PACKAGE_GROUPS
                ]
                if broad_groups:
                    return (
                        "Do not install broad Arch desktop application groups unless the "
                        "user explicitly requested the full suite. "
                        + ", ".join(broad_groups)
                        + " should be replaced with named requested apps such as kate, "
                        "konsole, dolphin, spectacle, or ark."
                    )
                aur_only = [
                    package
                    for package in packages
                    if package in _ARCH_AUR_ONLY_PACKAGES or package.endswith(_AURISH_SUFFIXES)
                ]
                if aur_only:
                    return (
                        "Do not pass AUR-only packages to `pacman` on Arch. "
                        + ", ".join(aur_only)
                        + " should use an AUR helper or manual `makepkg` flow."
                    )
                if not arch_nvidia_official_package_supported(facts, packages):
                    return (
                        "Detected pre-Turing NVIDIA hardware. Do not install the current "
                        "official Arch NVIDIA packages blindly; resolve the supported legacy "
                        "driver path with repo/AUR/web research before mutating drivers."
                    )
        elif package_manager == "apt" and tool in {"pacman", "yay", "paru", "dnf", "zypper"}:
            return f"This target uses apt, so `{tool}` is the wrong package manager."
        elif package_manager == "apt" and tool in {"apt", "apt-get"}:
            packages = _extract_apt_install_packages(tokens)
            desktop_metapackages = [
                package for package in packages if package in _UBUNTU_DESKTOP_METAPACKAGES
            ]
            if desktop_metapackages:
                return (
                    "Do not install Ubuntu desktop metapackages in debootstrap-driven remote "
                    "setup. They can pull Firefox/Snap transitional packages that block dpkg "
                    "noninteractively. Install explicit GNOME/display-manager packages instead "
                    "(for example gnome-shell, gdm3, ubuntu-session, gnome-terminal, nautilus, "
                    "gnome-control-center, yaru themes, xdg-desktop-portal-gnome)."
                )
        elif package_manager == "dnf" and tool in {
            "pacman",
            "yay",
            "paru",
            "apt",
            "apt-get",
            "zypper",
        }:
            return f"This target uses dnf, so `{tool}` is the wrong package manager."
        elif package_manager == "zypper" and tool in {
            "pacman",
            "yay",
            "paru",
            "apt",
            "apt-get",
            "dnf",
        }:
            return f"This target uses zypper, so `{tool}` is the wrong package manager."
    return None
