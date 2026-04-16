# Fedora Workstation card

Family: **fedora** — uses `families/fedora-family.md` as the base procedure.
Point-release (6-month cadence), GNOME default, classic `dnf` + `rpm`.
Fedora IS the family reference; this card lists the specific values.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Live user `liveuser`, auto-logged-in to GNOME |
| Variant | Classic (package-based, not ostree) |
| Releasever | `41` (current) — agent reads `/etc/fedora-release` to confirm |
| Package group | `@core @standard @workstation-product` |
| Installer | Anaconda + kickstart (primary); `dnf --installroot` as fallback |
| sudo group | `wheel` |

## Kickstart %packages block

```
%packages
@core
@standard
@workstation-product
openssh-server
sudo
NetworkManager
%end
```

## Fedora spins (alternate desktops)

Swap the package group for a different spin:
- `@kde-desktop-environment` — Fedora KDE
- `@xfce-desktop-environment` — Fedora XFCE
- `@mate-desktop-environment` — Fedora MATE
- `@cinnamon-desktop-environment` — Fedora Cinnamon

## Distro-specific pitfalls

- **RPMFusion for codecs/NVIDIA** — Fedora doesn't ship non-free codecs or
  NVIDIA proprietary drivers. Add RPMFusion:
  ```
  dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \
    https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
  ```
- **SELinux enforcing** — leave it alone unless user specifically asks.
- **Anaconda config file location** — `--kickstart=` flag is portable;
  kernel `inst.ks=` param also works.
- **Secure Boot** — Fedora's shim is signed; works out of the box.
