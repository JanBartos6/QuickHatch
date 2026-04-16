# MX Linux card

Family: **debian** — uses `families/debian-family.md` as the base.
Debian stable + antiX-contributed tooling, curated for daily use.
Consistently #1 on DistroWatch popularity. Supports systemd **and**
sysvinit (user picks at boot).

## Per-distro values

| Key | Value |
|---|---|
| Live env | Boots to XFCE as `demo` user (password `demo`) — or Fluxbox/KDE on other editions |
| Release codename | Matches Debian stable: `bookworm` (MX 23.x), `trixie` (MX 24.x) |
| Mirror (Debian base) | `http://deb.debian.org/debian` |
| Extra repo | `http://mxrepo.com/mx/repo/ bookworm main non-free` (MX's own packages) |
| Extra key | `curl -fsSL https://mxrepo.com/mx/repo/mxlinux.gpg.key \| apt-key add -` |
| Desktop metapackage | `mx-meta-core` + flavor metapackage: `mx-meta-xfce` (default) / `mx-meta-kde` / `mx-meta-fluxbox` / `mx-meta-xfwm4` |
| Installer | `minstall` (MX's GUI installer) — automation is very limited; prefer Path B debootstrap |
| Init choice | systemd (default) OR sysvinit; picked via GRUB "Advanced Options" at boot |
| sudo group | `sudo` |

## Installer-specific path

`minstall` is GUI-only with no meaningful automation path. The agent
uses Path B (debootstrap from family primer) with Debian stable's mirror,
then adds MX's repo inside the chroot:

```bash
# Inside the chroot
curl -fsSL https://mxrepo.com/mx/repo/mxlinux.gpg.key | apt-key add -
echo 'deb http://mxrepo.com/mx/repo/ bookworm main non-free' \
  > /etc/apt/sources.list.d/mx.list
apt update
apt install -y mx-meta-core mx-meta-xfce      # or mx-meta-kde, etc.
```

## sysvinit vs systemd

MX lets users pick between systemd (default) and sysvinit at boot. To
default to sysvinit for the installed system:

```bash
apt install -y sysvinit-core
# MX's grub theme exposes both at boot; no extra config needed.
```

Agent preference: leave systemd as default unless user explicitly asks.
Most downstream QuickHatch flows (desktop recipe application, service
management) assume systemd.

## Distro-specific pitfalls

- **MX Tools are the MX identity** — a rich suite of GUI utilities
  (`mx-packageinstaller`, `mx-boot-options`, `mx-snapshot`, etc.).
  Pre-installed via `mx-meta-core`. Users often prefer them over
  distro-generic tools; don't `apt remove mx-*`.
- **Debian stable lag** — newer hardware may need HWE kernels from
  backports or the MX-packaged liquorix kernel.
- **Non-free enabled by default** — MX's repo includes non-free Debian
  packages + firmware. Don't override.
- **Snap is deliberately absent** — MX blocks snap by policy. Users who
  need snap must explicitly install `snapd`.
- **antiX base means frugal defaults** — no PulseAudio auto-start quirks,
  no systemd-resolved, etc. Works, but if agent tweaks audio/network
  stack it should verify against MX conventions first.
