# Nobara Project card

Family: **fedora** — uses `families/fedora-family.md` as the base procedure.
Fedora Workstation downstream tuned for gaming + content creation (KDE / GNOME
flavors). Ships with NVIDIA drivers, proprietary codecs, tweaked kernel
(`kernel-fsync`), and gaming tooling preinstalled.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Live user `liveuser`, auto-logged-in to KDE/GNOME depending on ISO |
| Variant | Classic (package-based) |
| Installer | Nobara's Calamares-based graphical installer (ISO-bundled) is the primary path. Anaconda/kickstart is NOT the recommended route — Nobara ships Calamares. |
| Package manager | `dnf` + Nobara's `nobara-sync` / `nobara-welcome` helpers post-install |
| sudo group | `wheel` |
| Extra repos | Nobara's own repo + RPM Fusion (preconfigured) |

## Install procedure deltas from Fedora

1. Boot the Nobara ISO. Unlike upstream Fedora, the ISO boots directly into a
   **live desktop with a "Install Nobara" icon** — launch the bundled
   **Calamares** installer rather than Anaconda.
2. Calamares walks through: keyboard → location → partitioning → user →
   summary → install. No kickstart support.
3. First-boot: **Nobara Welcome** appears. Complete it — it enables RPM Fusion,
   installs recommended drivers (including NVIDIA proprietary when a Nvidia
   GPU is present), and queues a full `nobara-sync` update.
4. Post-install package commands use `dnf` exactly like Fedora. Nobara's
   recommended update command is `sudo nobara-sync cli` (wraps dnf + enables
   the kernel-fsync repo correctly) rather than raw `dnf upgrade`.
5. Gaming stack (Steam, Lutris, ProtonUp-Qt, GameMode, MangoHud) is already
   installed — don't re-install from scratch. Check with `rpm -q steam lutris`
   first.

## NVIDIA

Nobara installs the proprietary NVIDIA driver automatically via Nobara
Welcome on NVIDIA hardware. If the user skipped the welcome step, run:

```
sudo dnf install akmod-nvidia xorg-x11-drv-nvidia-cuda
sudo akmods --force
```

Then reboot.

## Notes for the agent

- If the user requested Nobara but Calamares isn't available on the live ISO
  (e.g. they booted upstream Fedora by mistake), STOP and ask — don't silently
  fall back to Anaconda, the resulting system won't be Nobara.
- The `<RELEASEVER>` placeholder for dnf commands matches Fedora's number
  (e.g. 41), which the agent can read from `/etc/fedora-release`.
- Hostname, timezone, user creation follow the fedora-family primer exactly.
