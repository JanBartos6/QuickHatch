# Fedora Silverblue / Kinoite card

Family: **fedora** — uses `families/fedora-family.md` as the base procedure.
Immutable Fedora, managed via `rpm-ostree`. Silverblue ships GNOME, Kinoite
ships KDE Plasma. Same ostree engine underneath both.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Live user `liveuser`, auto-logged to GNOME (SB) or Plasma (Kinoite) |
| Variant | **Ostree (immutable)** — NOT package-based |
| Installer | Anaconda with `ostreesetup` directive in kickstart |
| sudo group | `wheel` |

## Kickstart: swap `%packages` for `ostreesetup`

Silverblue:

```
ostreesetup --osname="fedora" --remote="fedora" \
  --url="https://ostree.fedoraproject.org" \
  --ref="fedora/41/x86_64/silverblue" \
  --nogpg
```

Kinoite:

```
ostreesetup --osname="fedora" --remote="fedora" \
  --url="https://ostree.fedoraproject.org" \
  --ref="fedora/41/x86_64/kinoite" \
  --nogpg
```

No `%packages` block needed — the ostree ref already contains the full
base image. App-level additions happen post-install via flatpak / rpm-
ostree layering.

## Post-install: the immutable app model

Root filesystem is read-only. Install apps via (in preference order):

1. **Flatpak** (user apps): `flatpak install -y flathub <app>`
2. **Toolbox** (dev CLIs): `toolbox create -r 41 && toolbox enter`
3. **Distrobox** (richer containers): `distrobox create -n dev -i fedora:41`
4. **Homebrew** (macOS-style userspace): standard curl installer.
5. **rpm-ostree layer** (system, requires reboot): `rpm-ostree install <pkg>`.

Agent priority: **flatpak > toolbox/distrobox > homebrew > rpm-ostree**.
rpm-ostree layering stages into the NEXT deployment and requires a reboot
to take effect; expensive, use sparingly.

## Silverblue vs Kinoite

| Feature | Silverblue | Kinoite |
|---|---|---|
| DE | GNOME | KDE Plasma |
| Theming | Adwaita | Breeze |
| Aliases (in primer index) | `silverblue`, `fedora-silverblue` | `kinoite`, `fedora-kinoite` |

The primer file is shared; the `--ref` in the kickstart is the only
difference.

## Distro-specific pitfalls

- **`dnf install` fails** — root is read-only. Tell the user it's normal;
  use flatpak/toolbox. Don't hack around immutability.
- **Kernel parameters** — use `rpm-ostree kargs --append=...`; direct edits
  to `/etc/default/grub` don't persist.
- **NVIDIA proprietary** — layer via `rpm-ostree install akmod-nvidia
  xorg-x11-drv-nvidia`. Needs RPMFusion layered in first (another
  rpm-ostree install). Reboot between steps.
- **User-level theming works** — `~/.config/gtk-4.0`, KDE's Breeze settings
  in `~/.config` are fine. System-wide theming (stylix-style) is limited
  by the immutable model.
- **Rollback is cheap** — `rpm-ostree rollback` reverts to the previous
  deployment. Good safety net when layering experiments fail.
