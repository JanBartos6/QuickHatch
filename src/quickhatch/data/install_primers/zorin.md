# Zorin OS card

Family: **debian** — uses `families/debian-family.md` as the base.
Ubuntu LTS derivative, designed for Windows/macOS refugees.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `zorin` |
| Release codename | Matches Ubuntu LTS: Zorin 18 → `noble` (24.04), Zorin 17 → `jammy` (22.04) |
| Mirror (Ubuntu base) | `http://archive.ubuntu.com/ubuntu` |
| Extra PPA | `ppa:zorinos/stable` |
| Desktop metapackage | `zorin-os-desktop` (Core, GNOME-based) |
| Installer | Ubiquity (Ubuntu-compatible autoinstall YAML works) |
| sudo group | `sudo` |

## Installer-specific path

Use the Ubuntu card's cloud-init `user-data` — Zorin's Ubiquity accepts it.

If using Path B (debootstrap) from the family primer, add the Zorin PPA
before installing `zorin-os-desktop`:

```bash
add-apt-repository -y ppa:zorinos/stable
apt update
apt install -y zorin-os-desktop
```

## What makes it "Windows-like"

- **Zorin Appearance** app — runtime layout switcher (Windows-11 / Windows-
  classic / macOS / GNOME / Ubuntu). Launch post-install: `zorin-appearance`.
- **Zorin Menu** — Start-menu-style launcher, preconfigured.
- **Themes** — ZorinBlue-Dark, ZorinGreen-Dark, etc. already shipped.
- **Wine + PlayOnLinux** — preinstalled on Pro edition only (paid).

## Distro-specific pitfalls

- **Core vs Pro** — Core is free, covers most users. Pro adds macOS +
  Windows 11 layouts, more icon packs, more preinstalled apps. QuickHatch
  can't install Pro (paid download); stick with Core.
- **Old Ubuntu base** — Zorin releases lag Ubuntu LTS by 6+ months. For
  newer hardware add HWE kernel: `apt install linux-generic-hwe-22.04`.
- **Layout switcher needs re-login** — changing Zorin Appearance requires
  logout/login to fully apply, not just "apply" button.
- **Wine on Core** — `winetricks` is installed but `wine` is not. Add it
  if the user scanned Windows .exe apps from the source system.
