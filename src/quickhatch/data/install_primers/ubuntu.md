# Ubuntu LTS card

Family: **debian** — uses `families/debian-family.md` as the base install
procedure.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `ubuntu` (desktop) / `installer` (server) |
| Release codename | `noble` (24.04 LTS) / `jammy` (22.04 LTS) |
| Mirror | `http://archive.ubuntu.com/ubuntu` |
| Extra repos | None required. Snap is enabled by default. |
| Desktop metapackage | `ubuntu-desktop` (GNOME) / `kubuntu-desktop` (KDE) / `xubuntu-desktop` / `ubuntu-server` (headless) |
| Installer | `subiquity` (server, cloud-init autoinstall) / `ubiquity` (desktop) |
| sudo group | `sudo` |
| non-free firmware | Ubuntu ships it by default via the `restricted` component |

## Installer-specific path (subiquity/ubiquity with cloud-init)

The preferred Path A config for Ubuntu is cloud-init `user-data`:

```yaml
#cloud-config
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  identity:
    realname: "<REAL_NAME>"
    username: <USER>
    password: "<HASHED_PASSWORD>"
    hostname: <HOSTNAME>
  ssh:
    install-server: true
    allow-pw: false
    authorized-keys:
      - "<PUBKEY>"
  packages:
    - sudo
    - network-manager
    - curl
  storage:
    layout:
      name: direct
  late-commands:
    - curtin in-target --target=/target -- systemctl enable ssh NetworkManager
    - curtin in-target --target=/target -- usermod -aG sudo <USER>
  shutdown: reboot
```

```bash
sudo subiquity --autoinstall-url=file:///tmp/user-data        # server
sudo ubiquity --automation-file=/tmp/user-data                # desktop
```

## Distro-specific pitfalls

- **Snap vs apt confusion** — default ISO installs Firefox/Chromium/Thunderbird
  as snaps. If the user prefers apt packages, disable snap via
  `apt remove snapd` and use the Mozilla PPA or flatpak.
- **Ubuntu Pro nag** — harmless, disable with `pro config set apt_news=false`.
- **HWE kernel** — newer hardware may need `linux-generic-hwe-24.04`.
- **GRUB EFI-only on UEFI machines** — swap `grub-efi-amd64` for `grub-pc`
  on BIOS-only targets.
