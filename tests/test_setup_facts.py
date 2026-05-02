from __future__ import annotations

from quickhatch.setup.facts import probe_setup_facts
from quickhatch.setup.types import DesktopRuntimeState, SetupFacts


def test_probe_setup_facts_parses_kscreen_display_runtime_block():
    output = """\
QH_PM=pacman
QH_AUR=yay
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_KERNEL_RELEASE=6.19.14-arch1-1
QH_VIRT=oracle
QH_SYS_VENDOR=innotek GmbH
QH_PRODUCT_NAME=VirtualBox
QH_INIT=systemd
QH_BOOT=efi
QH_SECURE_BOOT=disabled
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=sddm
QH_DM_ENABLED=yes
QH_DM_ACTIVE=yes
QH_GPU_VENDORS=nvidia
QH_GPU_INVENTORY=NVIDIA GeForce RTX 3080
QH_NVIDIA_SMI=ok
QH_NVIDIA_KERNEL_DRIVER=nvidia
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=nvidia_drm,nvidia
QH_DISPLAYS=
QH_KSCREEN_BEGIN
Output: 1 HDMI-A-1
    enabled
    connected
    Modes:  0:3840x2160@60*! 1:2560x1440@60
    priority 1
    Geometry: 0,0 3840x2160
    Scale: 1.75
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.displays[0].name == "HDMI-A-1"
    assert facts.displays[0].current_mode == "3840x2160"
    assert facts.displays[0].refresh_hz == 60
    assert facts.displays[0].scale_percent == 175
    assert facts.displays[0].primary is True
    assert facts.desktop_runtime_state == DesktopRuntimeState.LIVE_APPLY_SUCCEEDED
    assert facts.virtualization == "oracle"
    assert facts.kernel_release == "6.19.14-arch1-1"
    assert facts.system_vendor == "innotek GmbH"
    assert facts.product_name == "VirtualBox"
    assert facts.secure_boot_state == "disabled"


def test_probe_setup_facts_parses_secure_boot_enabled():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_SECURE_BOOT=enabled
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=
QH_DM_ENABLED=no
QH_DM_ACTIVE=no
QH_GPU_VENDORS=nvidia
QH_GPU_INVENTORY=NVIDIA GeForce GTX 1080
QH_NVIDIA_SMI=present_but_failing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.secure_boot_state == "enabled"
    assert facts.boot_mode == "efi"


def test_probe_setup_facts_can_promote_fedora_live_marker_from_raw_probe():
    output = """\
QH_PM=dnf
QH_AUR=none
QH_USER=liveuser
QH_LOGIN_USER=liveuser
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=fedora
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_SECURE_BOOT=disabled
QH_TARGET_ROOT=no
QH_IS_LIVE=yes
QH_LIVE_MARKER=LIVE:marker=/run/initramfs/live
QH_SESSION=
QH_DM=
QH_DM_ENABLED=no
QH_DM_ACTIVE=no
QH_GPU_VENDORS=
QH_GPU_INVENTORY=
QH_NVIDIA_SMI=missing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(
        ssh_exec,
        "192.0.2.10",
        "liveuser",
        None,
        is_live=False,
        live_marker="INSTALLED",
    )

    assert facts.is_live is True
    assert facts.live_marker == "LIVE:marker=/run/initramfs/live"


def test_probe_setup_facts_parses_amd_intel_acceleration_state():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_SECURE_BOOT=disabled
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=sddm
QH_DM_ENABLED=yes
QH_DM_ACTIVE=yes
QH_GPU_VENDORS=amd,intel
QH_GPU_INVENTORY=AMD Radeon RX 7900 XTX|Intel Arc A770
QH_NVIDIA_SMI=missing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_AMD_KERNEL_DRIVER=amdgpu
QH_INTEL_KERNEL_DRIVER=xe
QH_VULKAN_STATE=ok
QH_VULKAN_DEVICES=AMD Radeon RX 7900 XTX|Intel Arc A770
QH_VAAPI_STATE=ok
QH_ROCM_STATE=missing
QH_DRIVER_MODULES=amdgpu,xe
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.gpu_vendors == ["amd", "intel"]
    assert facts.amd_kernel_driver == "amdgpu"
    assert facts.intel_kernel_driver == "xe"
    assert facts.vulkan_state == "ok"
    assert facts.vulkan_devices == ["AMD Radeon RX 7900 XTX", "Intel Arc A770"]
    assert facts.vaapi_state == "ok"
    assert facts.rocm_state == "missing"
    assert facts.driver_modules == ["amdgpu", "xe"]
    assert facts.desktop_runtime_state == DesktopRuntimeState.PENDING_RELOGIN


def test_probe_setup_facts_marks_missing_amd_driver_as_display_stack_blocked():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=sddm
QH_DM_ENABLED=yes
QH_DM_ACTIVE=yes
QH_GPU_VENDORS=amd
QH_GPU_INVENTORY=AMD Radeon RX 6800 XT
QH_NVIDIA_SMI=missing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_AMD_KERNEL_DRIVER=none
QH_INTEL_KERNEL_DRIVER=
QH_VULKAN_STATE=missing
QH_VULKAN_DEVICES=
QH_VAAPI_STATE=missing
QH_ROCM_STATE=missing
QH_DRIVER_MODULES=
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.desktop_runtime_state == DesktopRuntimeState.BLOCKED_DRIVER_OR_DISPLAY_STACK


def test_probe_setup_facts_prefers_gui_session_over_ssh_tty_and_strips_ansi():
    output = """\
QH_PM=pacman
QH_AUR=yay
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SSH_SESSION=tty
QH_GUI_SESSION_ID=104
QH_GUI_SESSION_TYPE=wayland
QH_GUI_SESSION_STATE=active
QH_GUI_SESSION_ACTIVE=yes
QH_GUI_USER=jan
QH_GUI_PID=145031
QH_SESSION=wayland
QH_DM=sddm
QH_DM_ENABLED=yes
QH_GPU_VENDORS=nvidia
QH_GPU_INVENTORY=NVIDIA GeForce RTX 3080
QH_NVIDIA_SMI=ok
QH_NVIDIA_KERNEL_DRIVER=nvidia
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=nvidia_drm,nvidia
QH_DISPLAYS=
QH_KSCREEN_BEGIN
\x1b[01;32mOutput: \x1b[0;0m1 HDMI-A-1 daa085d9
    enabled
    connected
    \x1b[01;34mModes: \x1b[0;0m 1:3840x2160@29.98! 5:\x1b[01;32m1920x2160@59.99*\x1b[0;0m
    Geometry: 0,0 1920x2160
    Scale: 1
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.session_type == "wayland"
    assert facts.raw["QH_SSH_SESSION"] == "tty"
    assert facts.displays[0].name == "HDMI-A-1"
    assert facts.displays[0].current_mode == "1920x2160"
    assert facts.displays[0].refresh_hz == 59.99


def test_probe_setup_facts_parses_desktop_design_state():
    output = """\
QH_PM=pacman
QH_AUR=yay
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SESSION=wayland
QH_DESKTOP_ENV=KDE
QH_KDE_COLOR_SCHEME=SweetMars
QH_KDE_ICON_THEME=candy-icons
QH_KDE_CURSOR_THEME=Bibata-Modern-Ice
QH_KDE_WOBBLY=true
QH_KDE_MAGIC_LAMP=true
QH_KDE_WALLPAPER=file:///home/jan/Pictures/masterpiece.png
QH_GNOME_GTK_THEME=
QH_GNOME_ICON_THEME=
QH_GNOME_CURSOR_THEME=
QH_GNOME_WALLPAPER=
QH_DM=sddm
QH_DM_ENABLED=yes
QH_GPU_VENDORS=
QH_GPU_INVENTORY=
QH_NVIDIA_SMI=missing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.desktop_design.environment == "KDE"
    assert facts.desktop_design.kde_color_scheme == "SweetMars"
    assert facts.desktop_design.kde_icon_theme == "candy-icons"
    assert facts.desktop_design.kde_cursor_theme == "Bibata-Modern-Ice"
    assert facts.desktop_design.kde_wallpaper == "file:///home/jan/Pictures/masterpiece.png"
    assert facts.desktop_design.kde_wobbly_windows == "true"
    assert facts.desktop_design.kde_magic_lamp == "true"


def test_probe_setup_facts_does_not_treat_ssh_tty_as_live_desktop_success():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SSH_SESSION=tty
QH_SESSION=tty
QH_DM=sddm
QH_DM_ENABLED=yes
QH_GPU_VENDORS=nvidia
QH_GPU_INVENTORY=NVIDIA GeForce RTX 3080
QH_NVIDIA_SMI=ok
QH_NVIDIA_KERNEL_DRIVER=nvidia
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=nvidia_drm,nvidia
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.session_type == ""
    assert facts.desktop_runtime_state == DesktopRuntimeState.PENDING_RELOGIN


def test_probe_setup_facts_distinguishes_enabled_inactive_display_manager():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=gdm
QH_DM_ENABLED=yes
QH_DM_ACTIVE=no
QH_GPU_VENDORS=
QH_GPU_INVENTORY=VMware SVGA II Adapter
QH_NVIDIA_SMI=missing
QH_NVIDIA_KERNEL_DRIVER=
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.display_manager_enabled is True
    assert facts.display_manager_active is False
    assert facts.desktop_runtime_state == DesktopRuntimeState.BLOCKED_NO_GUI_SESSION


def test_probe_setup_facts_uses_xrandr_when_kscreen_is_unavailable():
    output = """\
QH_PM=pacman
QH_AUR=none
QH_USER=jan
QH_LOGIN_USER=jan
QH_IS_ROOT=no
QH_SUDO_NOPASSWD=yes
QH_OS_ID=arch
QH_OS_LIKE=
QH_INIT=systemd
QH_BOOT=efi
QH_TARGET_ROOT=no
QH_SESSION=
QH_DM=sddm
QH_DM_ENABLED=yes
QH_GPU_VENDORS=nvidia
QH_GPU_INVENTORY=NVIDIA GeForce RTX 3080
QH_NVIDIA_SMI=ok
QH_NVIDIA_KERNEL_DRIVER=nvidia
QH_NOUVEAU_BOUND=no
QH_DRIVER_MODULES=nvidia_drm,nvidia
QH_DISPLAYS=
QH_KSCREEN_BEGIN
QH_KSCREEN_END
QH_XRANDR_BEGIN
HDMI-0 connected primary 3840x2160+0+0
   3840x2160     60.00*+  59.94
   1920x1080     60.00
QH_XRANDR_END
"""

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (0, output, "")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None)

    assert facts.displays[0].name == "HDMI-0"
    assert facts.displays[0].primary is True
    assert facts.displays[0].current_mode == "3840x2160"
    assert facts.displays[0].refresh_hz == 60


def test_probe_setup_facts_marks_failed_probe_as_stale():
    fallback = SetupFacts(
        package_manager="pacman",
        current_user="jan",
        login_user="jan",
        gpu_vendors=["nvidia"],
        nvidia_smi_state="ok",
    )

    def ssh_exec(_host, _user, _command, _key_path, _timeout, _port):
        return (255, "", "ssh failed")

    facts = probe_setup_facts(ssh_exec, "192.0.2.10", "jan", None, fallback=fallback)

    assert facts.fact_probe_ok is False
    assert facts.nvidia_smi_state == "ok"
    assert facts.raw["QH_FACT_PROBE_RC"] == "255"
