# elementary OS card

Family: **debian** — uses `families/debian-family.md` as the base.
Ubuntu LTS derivative with the custom Pantheon desktop. macOS-inspired.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `elementary` |
| Release codename | Matches Ubuntu LTS: 8.x → `noble` (24.04), 7.x → `jammy` (22.04) |
| Mirror (Ubuntu base) | `http://archive.ubuntu.com/ubuntu` |
| Extra PPA | `ppa:elementary-os/stable` |
| Desktop metapackages | `elementary-desktop pantheon-session pantheon-wallpapers appcenter lightdm lightdm-pantheon-greeter switchboard` |
| Installer | elementary's forked Ubiquity (subset of Ubuntu autoinstall) |
| sudo group | `sudo` |

## Installer-specific path

Use a minimal Ubuntu-style `user-data` — elementary's installer rejects
some advanced keys. If it fails, fall back to debootstrap (Path B) and
install the Pantheon metapackages manually.

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
  storage:
    layout:
      name: direct
  shutdown: reboot
```

If Path B, inside the chroot:

```bash
add-apt-repository -y ppa:elementary-os/stable
apt update
apt install -y elementary-desktop pantheon-session pantheon-wallpapers \
  appcenter lightdm lightdm-pantheon-greeter switchboard
```

## Distro-specific pitfalls

- **GTK theming locked** — elementary hard-codes some app styles; themes
  from gnome-look.org often only half-apply. Agent should not try to force
  a non-Pantheon theme.
- **Dock is Plank** — customize via `plank --preferences`, not a GNOME
  extension.
- **No snap by default** — deliberate. Users wanting snap must
  `apt install snapd` themselves.
- **Flatpak-first AppCenter** — elementary's app store only shows flatpaks.
  The agent should prefer flatpak when installing user apps here.
- **Pay-what-you-want nag** — AppCenter asks for donations; harmless but
  users should know it's optional.
- **PPAs can conflict** — Ubuntu PPAs installing upstream GNOME packages
  can break elementary's modified GNOME stack. Prefer flatpak for user
  apps.
