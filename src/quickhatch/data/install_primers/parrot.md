# Parrot OS card

Family: **debian** — uses `families/debian-family.md` as the base.
Debian testing-based security distro. Smaller user base than Kali but
dedicated — popular in privacy-conscious security work + AnonSurf
(Tor-everywhere) workflows.

## Per-distro values

| Key | Value |
|---|---|
| Live env | `parrot` user (password `parrot`) or auto-login to MATE/KDE |
| Release codename | `parrot` (rolling, tracks Debian testing) |
| Mirror | `https://deb.parrot.sh/parrot` |
| Extra components | `main contrib non-free` |
| Signing key | `wget -qO- https://deb.parrot.sh/parrot/misc/parrotsec.gpg \| apt-key add -` |
| Desktop metapackage | `parrot-core parrot-interface parrot-meta-desktop` (MATE default) / `parrot-meta-kde-full` / `parrot-meta-gnome` |
| Installer | Calamares + Debian d-i fallback |
| sudo group | `sudo` |

## Parrot editions (pick metapackage)

| Edition | Metapackage | Contents |
|---|---|---|
| Home | `parrot-meta-home` | Daily-driver, no security tools by default |
| Security | `parrot-meta-security` | Full pentesting arsenal (Kali-equivalent) |
| Architect | `parrot-meta-architect` | Minimal base, user picks tools |

QuickHatch default: **Home** unless the user's stated use case is
security/pentesting, in which case **Security**.

## Installer-specific path

Calamares has limited automation — prefer Path B (debootstrap from family
primer) with Parrot's own mirror. Inside the chroot:

```bash
wget -qO- https://deb.parrot.sh/parrot/misc/parrotsec.gpg | apt-key add -
echo 'deb https://deb.parrot.sh/parrot parrot main contrib non-free' \
  > /etc/apt/sources.list.d/parrot.list
apt update

# Pick the metapackage:
apt install -y parrot-core parrot-interface parrot-meta-desktop
# or: parrot-meta-security, parrot-meta-architect
```

## AnonSurf (Parrot's signature tool)

`anonsurf start` routes all traffic through Tor. Comes with the Security
edition. Agent shouldn't enable it during install — user controls when
it's on.

## Distro-specific pitfalls

- **AppArmor enforcing** — Parrot defaults to AppArmor active (Kali does
  too but less strictly). Some tools require profile adjustment; agent
  leaves enforcement on unless user asks.
- **Sandbox-style defaults** — browsers run in a firejail-style sandbox
  by default. If the agent tries to launch GUIs over SSH, firejail
  profiles may block some X11/Wayland forwarding. Usually harmless.
- **Rolling nature** — version pinning breaks quickly. Keep the system
  fresh with `apt full-upgrade` periodically; agent should do one such
  upgrade after first boot before layering anything else.
- **Community < Kali** — if Parrot's repo is temporarily slow/down, the
  fallback is to switch to Kali mirrors (sibling distro), though that
  loses Parrot-specific packages.
- **AnonSurf + clearnet confusion** — if the user enabled AnonSurf during
  setup and then the agent tries to SSH out, connectivity may behave
  oddly. Test AnonSurf only AFTER setup completes.
