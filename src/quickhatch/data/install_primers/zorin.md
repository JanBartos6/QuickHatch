# Zorin OS install primer

Ubuntu LTS-based, explicitly designed to feel like Windows (or macOS via
"Zorin Appearance"). Beginner-friendly, ideal for Windows→Linux migrants
who want visual familiarity.

## Live environment

- Boots to GNOME + Zorin's custom shell extensions as `zorin` user.
- Installer: Zorin's fork of Ubiquity. Autoinstall/cloud-init supported.

## Approach — autoinstall (Ubuntu-compatible)

Same `user-data` format as Ubuntu:

```yaml
#cloud-config
autoinstall:
  version: 1
  identity:
    realname: "<REAL_NAME>"
    username: <USER>
    password: "<HASHED_PASSWORD>"
    hostname: <HOSTNAME>
  ssh:
    install-server: true
    authorized-keys:
      - "<PUBKEY>"
  packages:
    - sudo
    - openssh-server
    - network-manager
  storage:
    layout:
      name: direct
  late-commands:
    - curtin in-target --target=/target -- systemctl enable ssh NetworkManager
    - curtin in-target --target=/target -- usermod -aG sudo <USER>
  shutdown: reboot
```

```bash
sudo ubiquity --automation-file=/tmp/user-data
```

## Alternative — debootstrap + Zorin repos

```bash
sudo debootstrap --arch=amd64 jammy /mnt \
  http://archive.ubuntu.com/ubuntu
# Zorin 17 = Ubuntu 22.04 jammy
# Zorin 18 = Ubuntu 24.04 noble

sudo chroot /mnt /bin/bash <<'EOF'
# Add Zorin's PPA
add-apt-repository -y ppa:zorinos/stable
apt update
apt install -y zorin-os-desktop   # the full Zorin shell + themes

# ... rest matches Ubuntu primer
EOF
```

## What makes Zorin feel "Windows-like"

- **Zorin Appearance** app — switcher between Windows-11 / Windows-classic /
  macOS / GNOME / Ubuntu layouts. Post-install: `zorin-appearance` runs it.
- **Themes** — `ZorinBlue-Dark`, `ZorinGreen-Dark`, etc. already shipped.
- **Zorin Menu** — start-menu-style launcher preconfigured.
- **Wine + PlayOnLinux** — pre-installed on Pro edition.

## Zorin Core vs Pro

- **Core** (free) — enough for most users. What the autoinstall covers.
- **Pro** (paid) — adds "macOS", "Windows 11", "Windows Classic" layouts,
  premium icon packs, more preinstalled apps (GIMP, Blender, Krita, LibreOffice
  with templates).

QuickHatch can't auto-install Pro (paid download). Stick with Core and add
individual apps.

## Common pitfalls

- **Old Ubuntu base** — Zorin releases lag Ubuntu LTS. Newer hardware may
  need HWE kernel: `apt install linux-generic-hwe-22.04`.
- **Layout switcher needs re-login** — changing Zorin Appearance requires
  logout/login, not just apply. Agent should note this.
- **Wine edition** — on Core, `winetricks` is installed but not `wine` by
  default. Agent adds it if user scanned any Windows .exe apps.
