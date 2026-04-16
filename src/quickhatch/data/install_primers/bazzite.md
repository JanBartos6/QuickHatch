# Bazzite card

Family: **fedora** — uses `families/fedora-family.md` as the base procedure.
Atomic Fedora derivative from Universal Blue, built for gaming. Immutable,
flatpak-first, Steam/Proton/MangoHud/gamemode preinstalled.

## Per-distro values

| Key | Value |
|---|---|
| Live env | Fedora-derived live ISO, boots to GNOME (desktop variant) |
| Variant | **Ostree (immutable), OCI-based** (via Universal Blue) |
| Installer | Anaconda with `ostreesetup` pulling from ghcr.io |
| sudo group | `wheel` |

## Kickstart: ostreesetup pointing at the OCI registry

```
ostreesetup --osname="bazzite" --remote="bazzite" \
  --url="ostree-unverified-registry:ghcr.io/ublue-os/bazzite:latest" \
  --ref="bazzite:latest" \
  --nogpg
```

Other Universal Blue images work the same way, just different refs:
- `ghcr.io/ublue-os/bazzite-deck:latest` — Steam-Deck-optimized
- `ghcr.io/ublue-os/bazzite-gnome:latest` — GNOME instead of KDE
- `ghcr.io/ublue-os/aurora:latest` — KDE daily-driver (not gaming)
- `ghcr.io/ublue-os/bluefin:latest` — GNOME daily-driver (not gaming)

## What's pre-installed (skip re-installing)

- Steam, Lutris, Heroic Games Launcher
- Proton-GE, proton-tricks
- MangoHud, gamemode, gamescope
- Flatpak + Flathub configured
- NVIDIA proprietary drivers (if NVIDIA variant of the image)
- Decky Loader (Steam Deck plugin system, if Deck variant)

Agent should NOT try to install these.

## Post-install app model (same as Silverblue/Kinoite)

1. **Flatpak** (preferred for user apps).
2. **Distrobox** (rich CLI dev envs).
3. **Homebrew** (macOS-style userspace).
4. **rpm-ostree layer** (system, reboot required — avoid for user apps).

## Distro-specific pitfalls

- **`dnf install` fails** — same as Silverblue. Immutable root. Use
  flatpak/distrobox.
- **Gaming stack already there** — don't re-install Steam or MangoHud;
  just configure what's already present.
- **Kernel args** — `rpm-ostree kargs --append=...`.
- **"Gaming Mode" session** — boots into Steam Big Picture for
  Steam-Deck-like feel. Enable via greeter/SDDM session picker.
- **Image variants matter** — the ostree ref determines NVIDIA driver
  availability, Steam Deck tweaks, GNOME vs KDE. Pick based on user
  hardware + preferences. The image channel (`latest` / `stable` /
  `testing`) also matters; default to `stable` for daily-driver users.
