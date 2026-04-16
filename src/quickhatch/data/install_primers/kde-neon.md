# KDE Neon install primer

Ubuntu LTS base + bleeding-edge KDE Plasma (built directly from KDE master).
"KDE's showcase" — the fastest path to newest Plasma features.

## Live environment

- Boots to Plasma 6 as `neon` user.
- Installer: Calamares.

## Approach — pacstrap-equivalent via debootstrap + KDE repos

```bash
DISK=/dev/nvme0n1
# Partition (same as Ubuntu primer)

sudo debootstrap --arch=amd64 jammy /mnt \
  http://archive.ubuntu.com/ubuntu
# Neon stable base = Ubuntu 22.04 jammy (for now)

sudo chroot /mnt /bin/bash <<'EOF'
# Add KDE Neon repos (user edition — stable Plasma releases)
curl -fsSL https://origin.archive.neon.kde.org/public.key | apt-key add -
echo 'deb http://archive.neon.kde.org/user jammy main' \
  > /etc/apt/sources.list.d/neon.list

apt update
apt full-upgrade -y
apt install -y neon-desktop sddm plasma-discover neon-settings \
  openssh-server network-manager sudo grub-efi-amd64 efibootmgr \
  linux-generic-hwe-22.04

# Locale, user, ssh key, hostname — matches Ubuntu primer

systemctl enable sddm NetworkManager ssh
EOF

sudo umount -R /mnt
sudo reboot
```

## Neon editions (pick the right repo)

| Edition       | Repo line                                           |
|---------------|-----------------------------------------------------|
| User (stable) | `deb http://archive.neon.kde.org/user jammy main`   |
| Testing       | `deb http://archive.neon.kde.org/testing jammy main`|
| Developer     | `deb http://archive.neon.kde.org/developer jammy main` |
| Unstable      | `deb http://archive.neon.kde.org/unstable jammy main` |

The user edition is for ordinary daily drivers. Testing/Developer/Unstable
are for developers wanting bleeding-edge builds.

## Calamares alternative

Neon's Calamares works but doesn't have great automation. The debootstrap
path is more agent-friendly.

## Common pitfalls

- **Kernel HWE required** — Neon stays on old Ubuntu LTS bases; modern GPUs
  / Wi-Fi need `linux-generic-hwe-22.04` (or whatever matches the base).
- **Plasma crashes on upgrade** — Neon pushes Plasma versions aggressively;
  occasionally an upgrade breaks a session. Agent should set up
  systemd-journald with enough retention to diagnose.
- **App ecosystem = Ubuntu + KDE builds** — not every Plasma app has a Neon
  package. Use flatpak for those.
- **Not a general-purpose distro** — Neon is specifically to test+showcase
  Plasma. For daily driving, Kubuntu / Fedora KDE / openSUSE Tumbleweed
  KDE are more conservative.
