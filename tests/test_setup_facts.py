from __future__ import annotations

from quickhatch.setup.facts import probe_setup_facts
from quickhatch.setup.types import DesktopRuntimeState


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
Output: 1 HDMI-A-1
    enabled
    connected
    Modes:  0:3840x2160@60*! 1:2560x1440@60
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
    assert facts.desktop_runtime_state == DesktopRuntimeState.LIVE_APPLY_SUCCEEDED


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
