"""Typed runtime facts for the staged setup executor."""

from __future__ import annotations

import re
from collections.abc import Callable

from .types import DesktopDesignState, DesktopRuntimeState, DisplayRuntimeInfo, SetupFacts

_FACT_PREFIX = "QH_"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def setup_fact_probe_command() -> str:
    return r"""
echo 'QH_PM='$(if command -v pacman >/dev/null 2>&1; then echo pacman; elif command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then echo apt; elif command -v dnf >/dev/null 2>&1; then echo dnf; elif command -v zypper >/dev/null 2>&1; then echo zypper; elif command -v apk >/dev/null 2>&1; then echo apk; elif command -v xbps-install >/dev/null 2>&1; then echo xbps; elif command -v emerge >/dev/null 2>&1; then echo emerge; else echo unknown; fi)
echo 'QH_AUR='$(if command -v yay >/dev/null 2>&1; then echo yay; elif command -v paru >/dev/null 2>&1; then echo paru; else echo none; fi)
echo 'QH_USER='$(whoami 2>/dev/null || echo unknown)
echo 'QH_LOGIN_USER='$(logname 2>/dev/null || whoami 2>/dev/null || echo unknown)
echo 'QH_IS_ROOT='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; else echo no; fi)
echo 'QH_SUDO_NOPASSWD='$(if [ "$(id -u 2>/dev/null)" = "0" ]; then echo yes; elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi)
echo 'QH_OS_ID='$(. /etc/os-release 2>/dev/null; echo ${ID:-unknown})
echo 'QH_OS_LIKE='$(. /etc/os-release 2>/dev/null; echo ${ID_LIKE:-})
echo 'QH_OS_VERSION_ID='$(. /etc/os-release 2>/dev/null; echo ${VERSION_ID:-})
echo 'QH_KERNEL_RELEASE='$(uname -r 2>/dev/null || true)
echo 'QH_VIRT='$(systemd-detect-virt 2>/dev/null || true)
echo 'QH_SYS_VENDOR='$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null | tr '|' '/')
echo 'QH_PRODUCT_NAME='$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr '|' '/')
echo 'QH_INIT='$(if command -v systemctl >/dev/null 2>&1; then echo systemd; elif command -v rc-service >/dev/null 2>&1; then echo openrc; elif command -v sv >/dev/null 2>&1; then echo runit; else echo unknown; fi)
echo 'QH_BOOT='$(if [ -d /sys/firmware/efi/efivars ]; then echo efi; else echo bios; fi)
qh_secure_boot=unknown
if [ ! -d /sys/firmware/efi/efivars ]; then
  qh_secure_boot=not_efi
elif command -v mokutil >/dev/null 2>&1; then
  qh_sb_state=$(mokutil --sb-state 2>/dev/null || true)
  case "$qh_sb_state" in
    *enabled*) qh_secure_boot=enabled ;;
    *disabled*) qh_secure_boot=disabled ;;
  esac
else
  qh_sb_var=$(ls /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | head -n 1)
  if [ -r "$qh_sb_var" ] && command -v od >/dev/null 2>&1; then
    qh_sb_byte=$(od -An -t u1 -j 4 -N 1 "$qh_sb_var" 2>/dev/null | tr -d ' ')
    [ "$qh_sb_byte" = "1" ] && qh_secure_boot=enabled
    [ "$qh_sb_byte" = "0" ] && qh_secure_boot=disabled
  fi
fi
echo 'QH_SECURE_BOOT='$qh_secure_boot
echo 'QH_TARGET_ROOT='$(if mount | grep -Eq ' on /(mnt|target)( |$)'; then echo yes; else echo no; fi)
qh_live_marker=INSTALLED
if grep -qE '(^|[[:space:]])(boot=live|live-media|toram|rd\.live\.image|rd\.live\.overlay|archisolabel|archisodevice|boot=casper)([[:space:]]|$)' /proc/cmdline 2>/dev/null; then
  qh_live_marker='LIVE:cmdline'
else
  for qh_marker in /run/live /rofs /cdrom/casper /run/archiso /run/initramfs/live /run/rootfsbase /lib/live /run/calamares; do
    if [ -d "$qh_marker" ]; then
      qh_live_marker="LIVE:marker=$qh_marker"
      break
    fi
  done
fi
if [ "$qh_live_marker" = "INSTALLED" ] && mount 2>/dev/null | grep -qE ' on / type (overlay|aufs|squashfs|erofs|tmpfs)\b'; then
  qh_live_marker='LIVE:overlay-root'
fi
if [ "$qh_live_marker" = "INSTALLED" ] && findmnt -n -o FSTYPE / 2>/dev/null | grep -qE '^(overlay|aufs|squashfs|erofs|tmpfs)$'; then
  qh_live_marker='LIVE:root-fstype'
fi
if [ "$qh_live_marker" = "INSTALLED" ]; then
  case "$(whoami 2>/dev/null)" in
    liveuser|live|kali|parrot|manjaro|endeavour|artix|voidlive)
      if command -v unsquashfs >/dev/null 2>&1 || [ -x /usr/lib/live/boot ] || [ -d /run/initramfs/live ]; then
        qh_live_marker='LIVE:user'
      fi
      ;;
  esac
fi
echo 'QH_IS_LIVE='$(case "$qh_live_marker" in LIVE:*) echo yes ;; *) echo no ;; esac)
echo 'QH_LIVE_MARKER='$qh_live_marker
qh_ssh_session=${XDG_SESSION_TYPE:-}
qh_gui_record=$(
  if command -v loginctl >/dev/null 2>&1; then
    for qh_sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
      qh_type=$(loginctl show-session "$qh_sid" -p Type --value 2>/dev/null || true)
      qh_remote=$(loginctl show-session "$qh_sid" -p Remote --value 2>/dev/null || true)
      if { [ "$qh_type" = "wayland" ] || [ "$qh_type" = "x11" ]; } && [ "$qh_remote" != "yes" ]; then
        qh_name=$(loginctl show-session "$qh_sid" -p Name --value 2>/dev/null || true)
        qh_state=$(loginctl show-session "$qh_sid" -p State --value 2>/dev/null || true)
        qh_active=$(loginctl show-session "$qh_sid" -p Active --value 2>/dev/null || true)
        printf '%s|%s|%s|%s|%s\n' "$qh_sid" "$qh_name" "$qh_type" "$qh_state" "$qh_active"
        break
      fi
    done
  fi
)
qh_gui_session=$(printf '%s' "$qh_gui_record" | cut -d'|' -f1)
qh_gui_user=$(printf '%s' "$qh_gui_record" | cut -d'|' -f2)
qh_gui_type=$(printf '%s' "$qh_gui_record" | cut -d'|' -f3)
qh_gui_state=$(printf '%s' "$qh_gui_record" | cut -d'|' -f4)
qh_gui_active=$(printf '%s' "$qh_gui_record" | cut -d'|' -f5)
qh_gui_uid=$(id -u "$qh_gui_user" 2>/dev/null || id -u 2>/dev/null || echo 1000)
qh_gui_pid=$(
  if [ -n "$qh_gui_user" ]; then
    pgrep -u "$qh_gui_user" -n plasmashell 2>/dev/null || pgrep -u "$qh_gui_user" -n kwin_wayland 2>/dev/null || pgrep -u "$qh_gui_user" -n kwin_x11 2>/dev/null || true
  fi
)
qh_env_value() {
  qh_key=$1
  if [ -n "$qh_gui_pid" ] && [ -r "/proc/$qh_gui_pid/environ" ]; then
    tr '\000' '\n' < "/proc/$qh_gui_pid/environ" | awk -F= -v key="$qh_key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }'
  fi
}
qh_gui_dbus=$(qh_env_value DBUS_SESSION_BUS_ADDRESS)
qh_gui_display=$(qh_env_value DISPLAY)
qh_gui_wayland=$(qh_env_value WAYLAND_DISPLAY)
qh_gui_xauth=$(qh_env_value XAUTHORITY)
qh_gui_current_desktop=$(qh_env_value XDG_CURRENT_DESKTOP)
qh_gui_home=$(getent passwd "$qh_gui_user" 2>/dev/null | cut -d: -f6)
[ -n "$qh_gui_home" ] || qh_gui_home=$HOME
qh_ini_value() {
  qh_file=$1
  qh_group=$2
  qh_key=$3
  awk -F= -v section="[$qh_group]" -v key="$qh_key" '
    $0 == section { flag=1; next }
    /^\[/ { flag=0 }
    flag && $1 == key { sub(/^[^=]*=/, ""); print; exit }
  ' "$qh_file" 2>/dev/null
}
qh_gui_run() {
  if [ -n "$qh_gui_user" ] && [ "$(id -un 2>/dev/null)" != "$qh_gui_user" ] && command -v sudo >/dev/null 2>&1; then
    sudo -u "$qh_gui_user" env XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$qh_gui_uid}" DBUS_SESSION_BUS_ADDRESS="${qh_gui_dbus:-unix:path=/run/user/$qh_gui_uid/bus}" WAYLAND_DISPLAY="$qh_gui_wayland" DISPLAY="$qh_gui_display" XAUTHORITY="$qh_gui_xauth" "$@"
  else
    env XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$qh_gui_uid}" DBUS_SESSION_BUS_ADDRESS="${qh_gui_dbus:-unix:path=/run/user/$qh_gui_uid/bus}" WAYLAND_DISPLAY="$qh_gui_wayland" DISPLAY="$qh_gui_display" XAUTHORITY="$qh_gui_xauth" "$@"
  fi
}
qh_gsettings_value() {
  qh_schema=$1
  qh_key=$2
  if command -v gsettings >/dev/null 2>&1; then
    qh_gui_run gsettings get "$qh_schema" "$qh_key" 2>/dev/null | sed "s/^'//;s/'$//"
  fi
}
qh_xfconf_value() {
  qh_channel=$1
  qh_property=$2
  if command -v xfconf-query >/dev/null 2>&1; then
    if command -v dbus-run-session >/dev/null 2>&1; then
      dbus-run-session xfconf-query -c "$qh_channel" -p "$qh_property" 2>/dev/null
    else
      qh_gui_run xfconf-query -c "$qh_channel" -p "$qh_property" 2>/dev/null
    fi
  fi
}
echo 'QH_SSH_SESSION='$qh_ssh_session
echo 'QH_GUI_SESSION_ID='$qh_gui_session
echo 'QH_GUI_SESSION_TYPE='$qh_gui_type
echo 'QH_GUI_SESSION_STATE='$qh_gui_state
echo 'QH_GUI_SESSION_ACTIVE='$qh_gui_active
echo 'QH_GUI_USER='$qh_gui_user
echo 'QH_GUI_PID='$qh_gui_pid
echo 'QH_GUI_DISPLAY='$qh_gui_display
echo 'QH_GUI_WAYLAND='$qh_gui_wayland
echo 'QH_GUI_XAUTHORITY='$qh_gui_xauth
echo 'QH_GUI_DBUS='$qh_gui_dbus
echo 'QH_SESSION='$qh_gui_type
echo 'QH_DESKTOP_ENV='$qh_gui_current_desktop
echo 'QH_KDE_COLOR_SCHEME='$(qh_ini_value "$qh_gui_home/.config/kdeglobals" General ColorScheme)
echo 'QH_KDE_ICON_THEME='$(qh_ini_value "$qh_gui_home/.config/kdeglobals" Icons Theme)
echo 'QH_KDE_CURSOR_THEME='$(qh_ini_value "$qh_gui_home/.config/kcminputrc" Mouse cursorTheme)
echo 'QH_KDE_WOBBLY='$(qh_ini_value "$qh_gui_home/.config/kwinrc" Plugins wobblywindowsEnabled)
echo 'QH_KDE_MAGIC_LAMP='$(qh_ini_value "$qh_gui_home/.config/kwinrc" Plugins magiclampEnabled)
echo 'QH_KDE_WALLPAPER='$(awk -F= '/^(Image|Wallpaper|wallpaper)=/ {print $2; exit}' "$qh_gui_home/.config/plasma-org.kde.plasma.desktop-appletsrc" 2>/dev/null)
echo 'QH_GNOME_GTK_THEME='$(qh_gsettings_value org.gnome.desktop.interface gtk-theme)
echo 'QH_GNOME_ICON_THEME='$(qh_gsettings_value org.gnome.desktop.interface icon-theme)
echo 'QH_GNOME_CURSOR_THEME='$(qh_gsettings_value org.gnome.desktop.interface cursor-theme)
echo 'QH_GNOME_WALLPAPER='$(qh_gsettings_value org.gnome.desktop.background picture-uri)
echo 'QH_XFCE_GTK_THEME='$(qh_xfconf_value xsettings /Net/ThemeName)
echo 'QH_XFCE_ICON_THEME='$(qh_xfconf_value xsettings /Net/IconThemeName)
echo 'QH_XFCE_WINDOW_THEME='$(qh_xfconf_value xfwm4 /general/theme)
echo 'QH_XFCE_WALLPAPER='$(if command -v xfconf-query >/dev/null 2>&1 && command -v dbus-run-session >/dev/null 2>&1; then qh_wallpaper_prop=$(dbus-run-session xfconf-query -c xfce4-desktop -l 2>/dev/null | grep -E '/last-image$|/image-path$' | head -n 1); [ -n "$qh_wallpaper_prop" ] && dbus-run-session xfconf-query -c xfce4-desktop -p "$qh_wallpaper_prop" 2>/dev/null; fi)
echo 'QH_DM='$(basename "$(readlink /etc/systemd/system/display-manager.service 2>/dev/null)" 2>/dev/null | sed 's/\.service$//' || true)
echo 'QH_DM_ENABLED='$(if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled display-manager >/dev/null 2>&1; then echo yes; else echo no; fi)
echo 'QH_DM_ACTIVE='$(if command -v systemctl >/dev/null 2>&1 && systemctl is-active display-manager >/dev/null 2>&1; then echo yes; else echo no; fi)
qh_probe_ok() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 8s "$@" >/dev/null 2>&1
  else
    "$@" >/dev/null 2>&1
  fi
}
qh_gpu_sysfs_vendors() {
  for d in /sys/bus/pci/devices/*; do
    class=$(cat "$d/class" 2>/dev/null || true)
    vendor=$(cat "$d/vendor" 2>/dev/null || true)
    printf '%s' "$class" | grep -q '^0x03' || continue
    case "$vendor" in
      0x10de) echo nvidia ;;
      0x1002) echo amd ;;
      0x8086) echo intel ;;
    esac
  done
}
qh_gpu_sysfs_inventory() {
  for d in /sys/bus/pci/devices/*; do
    class=$(cat "$d/class" 2>/dev/null || true)
    vendor=$(cat "$d/vendor" 2>/dev/null || true)
    device=$(cat "$d/device" 2>/dev/null || true)
    driver=$(basename "$(readlink -f "$d/driver" 2>/dev/null || echo none)")
    printf '%s' "$class" | grep -q '^0x03' || continue
    case "$vendor" in
      0x10de) vendor_name=NVIDIA ;;
      0x1002) vendor_name=AMD ;;
      0x8086) vendor_name=Intel ;;
      *) vendor_name="PCI vendor $vendor" ;;
    esac
    printf '%s GPU device %s driver %s\n' "$vendor_name" "$device" "$driver"
  done
}
echo 'QH_GPU_VENDORS='$({ lspci 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /vga|3d|display/ { if ($0 ~ /nvidia/) print "nvidia"; else if ($0 ~ /amd|advanced micro devices|radeon/) print "amd"; else if ($0 ~ /intel/) print "intel"; }'; qh_gpu_sysfs_vendors; } | sort -u | paste -sd',' -)
echo 'QH_GPU_INVENTORY='$(if command -v lspci >/dev/null 2>&1; then lspci 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /vga|3d|display/ {sub(/^.*: /,""); gsub(/\|/, "/", $0); print $0}' | paste -sd'|' -; else qh_gpu_sysfs_inventory | paste -sd'|' -; fi)
echo 'QH_NVIDIA_SMI='$(if command -v nvidia-smi >/dev/null 2>&1; then if qh_probe_ok nvidia-smi; then echo ok; else echo present_but_failing; fi; else echo missing; fi)
echo 'QH_NVIDIA_KERNEL_DRIVER='$(for d in /sys/bus/pci/devices/*; do class=$(cat "$d/class" 2>/dev/null || true); vendor=$(cat "$d/vendor" 2>/dev/null || true); if [ "$vendor" = "0x10de" ] && printf '%s' "$class" | grep -q '^0x03'; then basename "$(readlink -f "$d/driver" 2>/dev/null || echo none)"; fi; done | sort -u | paste -sd',' -)
echo 'QH_NOUVEAU_BOUND='$(for d in /sys/bus/pci/devices/*; do class=$(cat "$d/class" 2>/dev/null || true); vendor=$(cat "$d/vendor" 2>/dev/null || true); driver=$(basename "$(readlink -f "$d/driver" 2>/dev/null || echo none)"); if [ "$vendor" = "0x10de" ] && printf '%s' "$class" | grep -q '^0x03' && [ "$driver" = "nouveau" ]; then echo yes; exit 0; fi; done; echo no)
echo 'QH_AMD_KERNEL_DRIVER='$(for d in /sys/bus/pci/devices/*; do class=$(cat "$d/class" 2>/dev/null || true); vendor=$(cat "$d/vendor" 2>/dev/null || true); if [ "$vendor" = "0x1002" ] && printf '%s' "$class" | grep -q '^0x03'; then basename "$(readlink -f "$d/driver" 2>/dev/null || echo none)"; fi; done | sort -u | paste -sd',' -)
echo 'QH_INTEL_KERNEL_DRIVER='$(for d in /sys/bus/pci/devices/*; do class=$(cat "$d/class" 2>/dev/null || true); vendor=$(cat "$d/vendor" 2>/dev/null || true); if [ "$vendor" = "0x8086" ] && printf '%s' "$class" | grep -q '^0x03'; then basename "$(readlink -f "$d/driver" 2>/dev/null || echo none)"; fi; done | sort -u | paste -sd',' -)
echo 'QH_VULKAN_STATE='$(if command -v vulkaninfo >/dev/null 2>&1; then if qh_probe_ok vulkaninfo --summary; then echo ok; else echo present_but_failing; fi; else echo missing; fi)
echo 'QH_VULKAN_DEVICES='$(if command -v vulkaninfo >/dev/null 2>&1; then vulkaninfo --summary 2>/dev/null | awk -F= '/deviceName/ {value=$2; gsub(/^[ \t]+|[ \t]+$/, "", value); gsub(/\|/, "/", value); print value}' | paste -sd'|' -; fi)
echo 'QH_VAAPI_STATE='$(if command -v vainfo >/dev/null 2>&1; then if qh_probe_ok vainfo; then echo ok; else echo present_but_failing; fi; else echo missing; fi)
echo 'QH_ROCM_STATE='$(if command -v rocminfo >/dev/null 2>&1; then if qh_probe_ok rocminfo; then echo ok; else echo present_but_failing; fi; else echo missing; fi)
echo 'QH_DRIVER_MODULES='$(lsmod 2>/dev/null | awk '/^(nvidia|amdgpu|radeon|i915|xe)/ {print $1}' | paste -sd',' -)
echo 'QH_DISPLAYS='$(if command -v xrandr >/dev/null 2>&1; then DISPLAY=${qh_gui_display:-${DISPLAY:-:0}} XAUTHORITY=${qh_gui_xauth:-${XAUTHORITY:-$HOME/.Xauthority}} xrandr --query 2>/dev/null | awk '/ connected/ {name=$1; primary=($0 ~ / primary / ? "primary" : "secondary"); mode=""; if (match($0, /[0-9]+x[0-9]+\+[0-9]+\+[0-9]+/)) { mode=substr($0, RSTART, RLENGTH); sub(/\+.*/, "", mode); } print name ":" primary ":" mode}' | paste -sd'|' -; fi)
echo 'QH_KSCREEN_BEGIN'
if command -v kscreen-doctor >/dev/null 2>&1; then XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$qh_gui_uid} DBUS_SESSION_BUS_ADDRESS=${qh_gui_dbus:-${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$qh_gui_uid/bus}} WAYLAND_DISPLAY=${qh_gui_wayland:-${WAYLAND_DISPLAY:-}} DISPLAY=${qh_gui_display:-${DISPLAY:-}} XAUTHORITY=${qh_gui_xauth:-${XAUTHORITY:-}} XDG_SESSION_TYPE=${qh_gui_type:-${XDG_SESSION_TYPE:-}} QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-wayland} kscreen-doctor -o 2>/dev/null || true; fi
echo 'QH_KSCREEN_END'
echo 'QH_XRANDR_BEGIN'
if command -v xrandr >/dev/null 2>&1; then DISPLAY=${qh_gui_display:-${DISPLAY:-:0}} XAUTHORITY=${qh_gui_xauth:-${XAUTHORITY:-$HOME/.Xauthority}} xrandr --query 2>/dev/null || true; fi
echo 'QH_XRANDR_END'
"""


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _parse_display_entry(entry: str) -> DisplayRuntimeInfo | None:
    parts = (entry or "").split(":")
    if not parts or not parts[0].strip():
        return None
    return DisplayRuntimeInfo(
        name=parts[0].strip(),
        primary=len(parts) > 1 and parts[1].strip() == "primary",
        current_mode=parts[2].strip() if len(parts) > 2 else "",
    )


def _collect_probe_block(output: str, name: str) -> str:
    begin = f"QH_{name}_BEGIN"
    end = f"QH_{name}_END"
    collecting = False
    lines: list[str] = []
    for line in output.splitlines():
        marker = line.strip()
        if marker == begin:
            collecting = True
            continue
        if marker == end:
            break
        if collecting:
            lines.append(line)
    return "\n".join(lines).strip()


def _parse_kscreen_output(output: str) -> list[DisplayRuntimeInfo]:
    displays: list[DisplayRuntimeInfo] = []
    current: DisplayRuntimeInfo | None = None

    def flush() -> None:
        nonlocal current
        if current and current.name:
            displays.append(current)
        current = None

    for raw_line in _strip_ansi(output).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Output:"):
            flush()
            parts = line.split()
            name = parts[2] if len(parts) >= 3 else parts[-1].rstrip(":")
            current = DisplayRuntimeInfo(name=name, primary=" primary" in f" {line.lower()} ")
            continue
        if current is None:
            continue
        geometry = re.search(r"\bGeometry:\s+\d+,\d+\s+(\d+x\d+)", line)
        if geometry:
            current.current_mode = geometry.group(1)
        if re.search(r"\bpriority\s+1\b", line, re.IGNORECASE):
            current.primary = True
        scale = re.search(r"\bScale:\s+([0-9.]+)", line)
        if scale:
            try:
                current.scale_percent = round(float(scale.group(1)) * 100)
            except ValueError:
                pass
        if "Modes:" in line:
            for match in re.finditer(r"(\d+x\d+)@([0-9.]+)([!*+]*)", line):
                mode, refresh, markers = match.groups()
                current.advertised_modes.append(f"{mode}@{refresh}")
                if "*" in markers or (
                    current.current_mode == mode
                    and current.refresh_hz is None
                    and ("!" in markers or "+" in markers)
                ):
                    current.current_mode = mode
                    try:
                        current.refresh_hz = float(refresh)
                    except ValueError:
                        pass
    flush()
    return displays


def _parse_xrandr_output(output: str) -> list[DisplayRuntimeInfo]:
    displays: list[DisplayRuntimeInfo] = []
    current: DisplayRuntimeInfo | None = None

    def flush() -> None:
        nonlocal current
        if current and current.name:
            displays.append(current)
        current = None

    for raw_line in _strip_ansi(output).splitlines():
        connected = re.match(
            r"^(\S+)\s+connected(?:\s+(primary))?(?:\s+(\d+x\d+)\+\d+\+\d+)?",
            raw_line,
        )
        disconnected = re.match(r"^\S+\s+disconnected\b", raw_line)
        if connected:
            flush()
            current = DisplayRuntimeInfo(
                name=connected.group(1),
                primary=bool(connected.group(2)),
                current_mode=connected.group(3) or "",
            )
            continue
        if disconnected:
            flush()
            continue
        if current is None:
            continue
        mode_line = re.match(r"^\s+(\d+x\d+)\s+(.+)$", raw_line)
        if not mode_line:
            continue
        mode = mode_line.group(1)
        for token in mode_line.group(2).split():
            refresh_match = re.match(r"^([0-9]+(?:\.[0-9]+)?)([*+]*)$", token)
            if not refresh_match:
                continue
            refresh, markers = refresh_match.groups()
            current.advertised_modes.append(f"{mode}@{refresh}")
            if "*" in markers:
                current.current_mode = mode
                try:
                    current.refresh_hz = float(refresh)
                except ValueError:
                    pass
    flush()
    return displays


def _desktop_runtime_state(facts: SetupFacts) -> DesktopRuntimeState:
    if facts.displays:
        return DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
    if (
        facts.gpu_vendors
        and "nvidia" in facts.gpu_vendors
        and facts.nvidia_smi_state
        not in {
            "ok",
            "missing",
        }
    ):
        return DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK
    if "amd" in facts.gpu_vendors and _kernel_driver_missing(facts.amd_kernel_driver):
        return DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK
    if "intel" in facts.gpu_vendors and _kernel_driver_missing(facts.intel_kernel_driver):
        return DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK
    if facts.display_manager_enabled and facts.display_manager_active:
        return DesktopRuntimeState.PENDING_RELOGIN
    return DesktopRuntimeState.BLOCKED_NO_GUI_SESSION


def _kernel_driver_missing(driver: str) -> bool:
    drivers = [item.strip().lower() for item in (driver or "").split(",") if item.strip()]
    return not drivers or all(item in {"none", "unknown"} for item in drivers)


def describe_fact_changes(before: SetupFacts | None, after: SetupFacts | None) -> str:
    if before is None or after is None:
        return ""
    updates: list[str] = []
    tracked = {
        "package_manager": "package manager",
        "community_helper": "community helper",
        "virtualization": "virtualization",
        "kernel_release": "kernel release",
        "system_vendor": "system vendor",
        "product_name": "product name",
        "current_user": "current user",
        "sudo_nopasswd": "sudo NOPASSWD",
        "secure_boot_state": "Secure Boot",
        "nvidia_smi_state": "nvidia-smi",
        "nvidia_kernel_driver": "NVIDIA kernel driver",
        "nvidia_nouveau_bound": "nouveau-bound NVIDIA GPU",
        "amd_kernel_driver": "AMD kernel driver",
        "intel_kernel_driver": "Intel kernel driver",
        "vulkan_state": "Vulkan",
        "vaapi_state": "VA-API",
        "rocm_state": "ROCm",
        "session_type": "session type",
        "display_manager": "display manager",
        "display_manager_active": "display manager active",
    }
    for key, label in tracked.items():
        old = getattr(before, key, None)
        new = getattr(after, key, None)
        if old != new:
            updates.append(f"{label}: {old!s} -> {new!s}")
    if before.gpu_vendors != after.gpu_vendors:
        updates.append(
            f"GPU vendors: {', '.join(before.gpu_vendors) or 'none'} -> {', '.join(after.gpu_vendors) or 'none'}"
        )
    if before.displays != after.displays:
        old_disp = ", ".join(d.name or "unknown" for d in before.displays) or "none"
        new_disp = ", ".join(d.name or "unknown" for d in after.displays) or "none"
        updates.append(f"displays: {old_disp} -> {new_disp}")
    return "; ".join(updates)


def probe_setup_facts(
    ssh_exec: Callable[[str, str, str, str | None, int, int], tuple[int, str, str]],
    host: str,
    user: str,
    key_path: str | None,
    *,
    port: int = 22,
    fallback: SetupFacts | None = None,
    is_live: bool = False,
    live_marker: str = "",
    pending_question_id: str = "",
    pending_question_blocking: bool = False,
) -> SetupFacts:
    base = fallback or SetupFacts(current_user=user, login_user=user)
    rc, out, _ = ssh_exec(host, user, setup_fact_probe_command(), key_path, 30, port)
    facts = SetupFacts(
        distro_family=base.distro_family,
        package_manager=base.package_manager,
        community_helper=base.community_helper,
        init_system=base.init_system,
        os_id=base.os_id,
        os_like=base.os_like,
        kernel_release=base.kernel_release,
        virtualization=base.virtualization,
        system_vendor=base.system_vendor,
        product_name=base.product_name,
        is_live=is_live,
        live_marker=live_marker,
        boot_mode=base.boot_mode,
        secure_boot_state=base.secure_boot_state,
        target_root_mounted=base.target_root_mounted,
        current_user=base.current_user or user,
        login_user=base.login_user or user,
        is_root=base.is_root,
        sudo_nopasswd=base.sudo_nopasswd,
        gpu_vendors=list(base.gpu_vendors),
        gpu_inventory=list(base.gpu_inventory),
        nvidia_smi_state=base.nvidia_smi_state,
        nvidia_kernel_driver=base.nvidia_kernel_driver,
        nvidia_nouveau_bound=base.nvidia_nouveau_bound,
        amd_kernel_driver=base.amd_kernel_driver,
        intel_kernel_driver=base.intel_kernel_driver,
        vulkan_state=base.vulkan_state,
        vulkan_devices=list(base.vulkan_devices),
        vaapi_state=base.vaapi_state,
        rocm_state=base.rocm_state,
        driver_modules=list(base.driver_modules),
        session_type=base.session_type,
        display_manager=base.display_manager,
        display_manager_enabled=base.display_manager_enabled,
        desktop_runtime_state=base.desktop_runtime_state,
        displays=list(base.displays),
        desktop_design=base.desktop_design,
        requested_app_states=dict(base.requested_app_states),
        pending_question_id=pending_question_id,
        pending_question_blocking=pending_question_blocking,
        waiting_for_input=bool(pending_question_id and pending_question_blocking),
        fact_probe_ok=rc == 0,
        raw=dict(base.raw),
    )
    if rc != 0:
        facts.raw = {**facts.raw, "QH_FACT_PROBE_RC": str(rc)}
        return facts
    raw: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line or not line.startswith(_FACT_PREFIX):
            continue
        key, value = line.split("=", 1)
        raw[key.strip()] = value.strip()
    facts.raw = dict(raw)
    facts.package_manager = raw.get("QH_PM", facts.package_manager) or facts.package_manager
    facts.community_helper = raw.get("QH_AUR", facts.community_helper) or facts.community_helper
    facts.current_user = raw.get("QH_USER", facts.current_user) or facts.current_user
    facts.login_user = raw.get("QH_LOGIN_USER", facts.login_user) or facts.login_user
    facts.is_root = raw.get("QH_IS_ROOT", "no") == "yes"
    facts.sudo_nopasswd = raw.get("QH_SUDO_NOPASSWD", "no") == "yes"
    facts.os_id = raw.get("QH_OS_ID", facts.os_id) or facts.os_id
    facts.os_like = raw.get("QH_OS_LIKE", facts.os_like) or facts.os_like
    facts.kernel_release = (
        raw.get("QH_KERNEL_RELEASE", facts.kernel_release) or facts.kernel_release
    )
    facts.virtualization = raw.get("QH_VIRT", facts.virtualization) or facts.virtualization
    facts.system_vendor = raw.get("QH_SYS_VENDOR", facts.system_vendor) or facts.system_vendor
    facts.product_name = raw.get("QH_PRODUCT_NAME", facts.product_name) or facts.product_name
    facts.init_system = raw.get("QH_INIT", facts.init_system) or facts.init_system
    facts.boot_mode = raw.get("QH_BOOT", facts.boot_mode) or facts.boot_mode
    facts.secure_boot_state = (
        raw.get("QH_SECURE_BOOT", facts.secure_boot_state) or facts.secure_boot_state
    )
    facts.target_root_mounted = raw.get("QH_TARGET_ROOT", "no") == "yes"
    live_raw = raw.get("QH_IS_LIVE")
    live_marker_raw = raw.get("QH_LIVE_MARKER", "")
    if live_raw == "yes":
        facts.is_live = True
        facts.live_marker = live_marker_raw or facts.live_marker or "LIVE:facts"
    elif live_raw == "no" and not facts.is_live:
        facts.is_live = False
        facts.live_marker = live_marker_raw or facts.live_marker
    session_type = (raw.get("QH_SESSION", "") or "").strip().lower()
    facts.session_type = session_type if session_type in {"wayland", "x11"} else ""
    facts.display_manager = raw.get("QH_DM", facts.display_manager) or ""
    facts.display_manager_enabled = raw.get("QH_DM_ENABLED", "no") == "yes"
    dm_active_raw = raw.get("QH_DM_ACTIVE")
    facts.display_manager_active = (
        dm_active_raw == "yes" if dm_active_raw is not None else facts.display_manager_enabled
    )
    facts.desktop_design = DesktopDesignState(
        environment=raw.get("QH_DESKTOP_ENV", ""),
        kde_color_scheme=raw.get("QH_KDE_COLOR_SCHEME", ""),
        kde_icon_theme=raw.get("QH_KDE_ICON_THEME", ""),
        kde_cursor_theme=raw.get("QH_KDE_CURSOR_THEME", ""),
        kde_wallpaper=raw.get("QH_KDE_WALLPAPER", ""),
        kde_wobbly_windows=raw.get("QH_KDE_WOBBLY", ""),
        kde_magic_lamp=raw.get("QH_KDE_MAGIC_LAMP", ""),
        gnome_gtk_theme=raw.get("QH_GNOME_GTK_THEME", ""),
        gnome_icon_theme=raw.get("QH_GNOME_ICON_THEME", ""),
        gnome_cursor_theme=raw.get("QH_GNOME_CURSOR_THEME", ""),
        gnome_wallpaper=raw.get("QH_GNOME_WALLPAPER", ""),
        xfce_gtk_theme=raw.get("QH_XFCE_GTK_THEME", ""),
        xfce_icon_theme=raw.get("QH_XFCE_ICON_THEME", ""),
        xfce_window_theme=raw.get("QH_XFCE_WINDOW_THEME", ""),
        xfce_wallpaper=raw.get("QH_XFCE_WALLPAPER", ""),
    )
    facts.gpu_vendors = [p for p in raw.get("QH_GPU_VENDORS", "").split(",") if p]
    facts.gpu_inventory = [p for p in raw.get("QH_GPU_INVENTORY", "").split("|") if p]
    facts.nvidia_smi_state = (
        raw.get("QH_NVIDIA_SMI", facts.nvidia_smi_state) or facts.nvidia_smi_state
    )
    facts.nvidia_kernel_driver = raw.get("QH_NVIDIA_KERNEL_DRIVER", facts.nvidia_kernel_driver)
    facts.nvidia_nouveau_bound = raw.get("QH_NOUVEAU_BOUND", "no") == "yes"
    facts.amd_kernel_driver = raw.get("QH_AMD_KERNEL_DRIVER", facts.amd_kernel_driver)
    facts.intel_kernel_driver = raw.get("QH_INTEL_KERNEL_DRIVER", facts.intel_kernel_driver)
    facts.vulkan_state = raw.get("QH_VULKAN_STATE", facts.vulkan_state) or facts.vulkan_state
    facts.vulkan_devices = [p for p in raw.get("QH_VULKAN_DEVICES", "").split("|") if p]
    facts.vaapi_state = raw.get("QH_VAAPI_STATE", facts.vaapi_state) or facts.vaapi_state
    facts.rocm_state = raw.get("QH_ROCM_STATE", facts.rocm_state) or facts.rocm_state
    facts.driver_modules = [p for p in raw.get("QH_DRIVER_MODULES", "").split(",") if p]
    kscreen_displays = _parse_kscreen_output(_collect_probe_block(out, "KSCREEN"))
    xrandr_displays = _parse_xrandr_output(_collect_probe_block(out, "XRANDR"))
    fallback_displays = [
        display
        for display in (
            _parse_display_entry(entry) for entry in raw.get("QH_DISPLAYS", "").split("|")
        )
        if display
    ]
    facts.displays = kscreen_displays or xrandr_displays or fallback_displays
    facts.desktop_runtime_state = _desktop_runtime_state(facts)
    return facts
