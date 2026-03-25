"""Check Linux driver support for detected hardware."""

from __future__ import annotations

from quickhatch.config import HardwareInfo


def check_drivers(hardware: HardwareInfo) -> list[dict]:
    """
    Check Linux driver support for the detected hardware.

    Returns a list of driver recommendations.
    """
    recommendations = []

    # GPU drivers
    for gpu in hardware.gpus:
        rec = _check_gpu_driver(gpu.name, gpu.vendor)
        if rec:
            recommendations.append(rec)

    # WiFi drivers
    for adapter in hardware.wifi_adapters:
        rec = _check_wifi_driver(adapter)
        if rec:
            recommendations.append(rec)

    # Bluetooth
    if hardware.bluetooth:
        recommendations.append({
            "component": "Bluetooth",
            "status": "supported",
            "action": "Install bluez and blueman: sudo apt install bluez blueman",
            "notes": "Most Bluetooth adapters work out of the box on Linux",
        })

    return recommendations


def _check_gpu_driver(name: str, vendor: str) -> dict | None:
    """Check GPU driver availability."""
    name_lower = name.lower()

    if vendor == "nvidia":
        return {
            "component": f"GPU: {name}",
            "status": "supported",
            "action": "Install NVIDIA proprietary drivers",
            "notes": (
                "Use your distro's driver manager or:\n"
                "  Ubuntu/Mint: sudo ubuntu-drivers install\n"
                "  Fedora: sudo dnf install akmod-nvidia\n"
                "  Arch: sudo pacman -S nvidia nvidia-utils\n"
                "For gaming, also install: vulkan-tools mesa-vulkan-drivers"
            ),
        }
    elif vendor == "amd":
        return {
            "component": f"GPU: {name}",
            "status": "excellent",
            "action": "AMD drivers are built into the Linux kernel (AMDGPU)",
            "notes": (
                "AMD GPUs have excellent open-source support.\n"
                "Install mesa drivers: sudo apt install mesa-vulkan-drivers\n"
                "For the latest: sudo add-apt-repository ppa:kisak/kisak-mesa"
            ),
        }
    elif vendor == "intel":
        return {
            "component": f"GPU: {name}",
            "status": "excellent",
            "action": "Intel drivers are built into the Linux kernel (i915)",
            "notes": "Intel integrated graphics work out of the box on Linux.",
        }

    return None


def _check_wifi_driver(adapter: str) -> dict | None:
    """Check WiFi driver availability."""
    adapter_lower = adapter.lower()

    if "intel" in adapter_lower:
        return {
            "component": f"WiFi: {adapter}",
            "status": "excellent",
            "action": "Intel WiFi works out of the box (iwlwifi driver)",
            "notes": "No additional drivers needed.",
        }
    elif "realtek" in adapter_lower:
        return {
            "component": f"WiFi: {adapter}",
            "status": "supported",
            "action": "May need firmware package",
            "notes": (
                "Install: sudo apt install firmware-realtek\n"
                "Some Realtek adapters need rtl8xxxu or rtw88 drivers."
            ),
        }
    elif "broadcom" in adapter_lower:
        return {
            "component": f"WiFi: {adapter}",
            "status": "needs_driver",
            "action": "Install Broadcom drivers",
            "notes": (
                "Broadcom WiFi often needs proprietary drivers:\n"
                "  Ubuntu: sudo apt install bcmwl-kernel-source\n"
                "  Or: sudo apt install firmware-b43-installer"
            ),
        }
    elif "qualcomm" in adapter_lower or "atheros" in adapter_lower:
        return {
            "component": f"WiFi: {adapter}",
            "status": "excellent",
            "action": "Qualcomm/Atheros WiFi works out of the box (ath9k/ath10k)",
            "notes": "Excellent open-source driver support.",
        }
    elif "mediatek" in adapter_lower:
        return {
            "component": f"WiFi: {adapter}",
            "status": "supported",
            "action": "MediaTek WiFi generally works, may need firmware",
            "notes": "Install: sudo apt install firmware-misc-nonfree",
        }

    return {
        "component": f"WiFi: {adapter}",
        "status": "unknown",
        "action": "Check compatibility after installation",
        "notes": "Most modern WiFi adapters are supported. Check after first boot.",
    }
