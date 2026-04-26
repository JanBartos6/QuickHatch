"""Preflight guards for remote setup commands.

These checks catch stable, high-confidence mistakes before a staged command
hits SSH. They intentionally avoid fast-changing package truth; exact package
resolution belongs to the planner/search phase.
"""

from __future__ import annotations

import shlex

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


def _uses_fake_plasma_session_wrapper(cmd: str) -> bool:
    lower = (cmd or "").lower()
    return "dbus-run-session" in lower and (
        "plasma-apply-lookandfeel" in lower or "plasma-apply-wallpaperimage" in lower
    )


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


def preflight_setup_command(
    command: str,
    facts: SetupFacts,
    *,
    allow_network_security_changes: bool = False,
) -> str | None:
    """Return a blocking reason for clearly unsafe/impossible setup commands."""

    package_manager = facts.package_manager or "unknown"
    community_helper = facts.community_helper or "none"
    if not allow_network_security_changes and _activates_network_security_services(command):
        return (
            "Do not enable/start VPN or firewall services automatically. Installing the package "
            "is fine, but activating VPN/firewall services can change routing, DNS, firewall "
            "policy, or kill-switch behavior and cut off remote access."
        )
    if _uses_fake_plasma_session_wrapper(command):
        return (
            "Do not use `dbus-run-session` to apply KDE/Plasma theming. It creates a throwaway "
            "bus, so live apply commands do not affect the user's real desktop session. Use the "
            "logged-in user's actual session bus or write persisted config and require relogin."
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
        if _mutates_root_owned_path(tool, tokens) and not has_root_privilege:
            return (
                f"`{segment}` mutates a root-owned path but is not running as root. "
                "Use `sudo` for root-owned config/system paths, or run the command inside the "
                "installer chroot while the session is root."
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
        if package_manager == "pacman":
            if tool in {"apt", "apt-get", "dnf", "zypper"}:
                return (
                    f"This target is Arch-family (`pacman`), so `{tool}` is the wrong package "
                    "manager. Use `pacman` for repo packages and the AUR helper only for AUR."
                )
            if tool in {"yay", "paru"} and community_helper == "none":
                return (
                    f"The command uses `{tool}` but no AUR helper is installed yet. Install "
                    "the helper first, or build manually with `makepkg -s` + `pacman -U`."
                )
            if tool == "pacman":
                packages = _extract_pacman_install_packages(tokens)
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
        elif package_manager == "apt" and tool in {"pacman", "yay", "paru", "dnf", "zypper"}:
            return f"This target uses apt, so `{tool}` is the wrong package manager."
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
