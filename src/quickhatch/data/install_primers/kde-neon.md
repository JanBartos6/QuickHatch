# KDE Neon card

Family: **debian** — uses `families/debian-family.md` as the base.
Ubuntu LTS base + bleeding-edge KDE Plasma (built directly from KDE master).

## Per-distro values

| Key | Value |
|---|---|
| Live user | `neon` |
| Release codename | `jammy` (22.04 base — Neon stays on older LTS) |
| Mirror (Ubuntu base) | `http://archive.ubuntu.com/ubuntu` |
| Extra repo | `deb http://archive.neon.kde.org/user jammy main` (stable Plasma) |
| Extra key | `curl -fsSL https://origin.archive.neon.kde.org/public.key \| apt-key add -` |
| Desktop metapackages | `neon-desktop sddm plasma-discover neon-settings` |
| Installer | Calamares (automation limited — prefer Path B debootstrap) |
| sudo group | `sudo` |

## Neon editions (pick the right repo)

| Edition | Repo |
|---|---|
| User (stable, default) | `deb http://archive.neon.kde.org/user jammy main` |
| Testing | `deb http://archive.neon.kde.org/testing jammy main` |
| Developer | `deb http://archive.neon.kde.org/developer jammy main` |
| Unstable | `deb http://archive.neon.kde.org/unstable jammy main` |

Regular users want "User". Testing/Developer/Unstable are for Plasma
contributors wanting bleeding-edge builds.

## Installer-specific path

Use Path B (debootstrap) from the family primer. Inside the chroot, add
Neon's repo BEFORE the metapackage install:

```bash
curl -fsSL https://origin.archive.neon.kde.org/public.key | apt-key add -
echo 'deb http://archive.neon.kde.org/user jammy main' \
  > /etc/apt/sources.list.d/neon.list

apt update
apt full-upgrade -y       # Neon pushes Plasma from master — upgrade first
apt install -y neon-desktop sddm plasma-discover neon-settings \
  openssh-server network-manager sudo grub-efi-amd64 efibootmgr \
  linux-generic-hwe-22.04

systemctl enable sddm NetworkManager ssh
```

## Distro-specific pitfalls

- **HWE kernel required** — Neon stays on old Ubuntu LTS bases; modern GPUs/
  Wi-Fi need `linux-generic-hwe-22.04`.
- **Plasma crashes on upgrade** — Neon pushes Plasma versions aggressively.
  Occasionally an upgrade breaks a session. Agent enables systemd-journald
  with persistent logs so post-mortem is possible.
- **App ecosystem = Ubuntu + KDE builds** — not every Plasma app has a Neon
  package. Flatpak for anything missing.
- **Not a general-purpose distro** — Neon exists to showcase+test Plasma.
  For daily-driver Plasma, consider Kubuntu / Fedora KDE / openSUSE
  Tumbleweed KDE as more conservative alternatives.
