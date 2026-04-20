"""Check Linux driver support for detected hardware."""

from __future__ import annotations

from quickhatch.config import HardwareInfo


def _gpu_vendors(hw: HardwareInfo) -> set[str]:
    """Return normalized GPU vendors present in the hardware snapshot."""
    return {gpu.vendor for gpu in hw.gpus if gpu.vendor in {"nvidia", "amd", "intel"}}


def check_drivers(hw: HardwareInfo) -> list[dict]:
    """Check driver compatibility and return recommendations."""
    recommendations = []
    gpu_vendors = _gpu_vendors(hw)

    if "nvidia" in gpu_vendors:
        recommendations.append(
            {
                "component": "GPU: NVIDIA",
                "status": "action_needed",
                "recommendation": (
                    "Install proprietary NVIDIA drivers. "
                    "Use distros with easy NVIDIA setup (Pop!_OS, Ubuntu, Manjaro). "
                    "Avoid Nouveau open-source driver for best performance."
                ),
                "install_cmd": "sudo ubuntu-drivers autoinstall  # or equivalent for your distro",
            }
        )
    if "amd" in gpu_vendors:
        recommendations.append(
            {
                "component": "GPU: AMD",
                "status": "good",
                "recommendation": (
                    "AMD GPUs have excellent open-source driver support (AMDGPU). "
                    "Drivers are included in the Linux kernel. No additional setup needed."
                ),
                "install_cmd": None,
            }
        )
    if "intel" in gpu_vendors:
        recommendations.append(
            {
                "component": "GPU: Intel",
                "status": "good",
                "recommendation": (
                    "Intel integrated graphics have built-in kernel support. "
                    "No additional drivers needed."
                ),
                "install_cmd": None,
            }
        )

    if hw.wifi_adapters:
        recommendations.append(
            {
                "component": "WiFi",
                "status": "check",
                "recommendation": (
                    "Most WiFi adapters work on Linux. If yours doesn't work out of the box, "
                    "the AI agent will help install the correct firmware/driver. "
                    "Intel and Qualcomm/Atheros adapters have the best Linux support."
                ),
                "install_cmd": "sudo apt install linux-firmware  # ensures firmware is available",
            }
        )

    if hw.bluetooth:
        recommendations.append(
            {
                "component": "Bluetooth",
                "status": "good",
                "recommendation": (
                    "Bluetooth is well-supported on Linux via BlueZ. "
                    "Most adapters work out of the box."
                ),
                "install_cmd": "sudo systemctl enable --now bluetooth",
            }
        )

    return recommendations
