"""Download distro ISO files."""

from __future__ import annotations

from pathlib import Path

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from quickhatch.console import console, print_error

# Known ISO download URLs (latest stable versions)
ISO_URLS: dict[str, str] = {
    "ubuntu": "https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso",
    "linuxmint": "https://mirrors.edge.kernel.org/linuxmint/stable/22/linuxmint-22-cinnamon-64bit.iso",
    "fedora": "https://download.fedoraproject.org/pub/fedora/linux/releases/41/Workstation/x86_64/iso/Fedora-Workstation-Live-x86_64-41-1.4.iso",
    "popos": "https://iso.pop-os.org/22.04/amd64/intel/45/pop-os_22.04_amd64_intel_45.iso",
    "arch": "https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso",
    "manjaro": "https://download.manjaro.org/kde/24.0/manjaro-kde-24.0-240910-linux69.iso",
    "debian": "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-12.7.0-amd64-netinst.iso",
    "opensuse": "https://download.opensuse.org/tumbleweed/iso/openSUSE-Tumbleweed-DVD-x86_64-Current.iso",
    "zorin": "https://zorinos.com/download/17/core",
    "elementary": "https://ams3.dl.elementary.io/download/elementary-os-7.1-stable.20231213-amd64.iso",
    "nobara": "https://nobara-images.nobaraproject.org/Nobara-40-Official-2024-09-07.iso",
    "endeavouros": "https://mirror.alpix.eu/endeavouros/iso/EndeavourOS_Endeavour-2024.09.22.iso",
}


def get_iso_url(distro: str) -> str | None:
    """Get the ISO download URL for a distribution."""
    key = distro.lower().replace(" ", "").replace("_", "").replace("!", "").replace("'", "")

    # Direct match
    if key in ISO_URLS:
        return ISO_URLS[key]

    # Fuzzy match
    for db_key, url in ISO_URLS.items():
        if db_key in key or key in db_key:
            return url

    return None


def download_iso(url: str, distro: str, dest_dir: Path | None = None) -> Path | None:
    """Download an ISO file with progress bar."""
    if dest_dir is None:
        dest_dir = Path.home() / "Downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = url.split("/")[-1]
    if not filename.endswith(".iso"):
        filename = f"{distro.lower().replace(' ', '-')}.iso"
    dest_path = dest_dir / filename

    if dest_path.exists():
        console.print(f"[info]ISO already downloaded:[/info] {dest_path}")
        return dest_path

    try:
        console.print(f"Downloading {distro} ISO...")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Downloading {filename}", total=total_size)

            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    progress.advance(task, len(chunk))

        console.print(f"[success]Downloaded:[/success] {dest_path}")
        return dest_path

    except requests.RequestException as e:
        print_error(f"Download failed: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return None
