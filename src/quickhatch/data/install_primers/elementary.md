# Elementary OS install primer

Ubuntu LTS base + Pantheon desktop (elementary's custom DE). macOS-inspired
aesthetic: dock, top bar, crisp typography, curated AppCenter.

## Live environment

- Boots to Pantheon as `elementary` user.
- Installer: elementary OS Installer (forked Ubiquity, simpler UX).

## Approach — autoinstall (Ubuntu-compatible, limited)

elementary's installer accepts a subset of the Ubuntu autoinstall format.
For best results, stick to the basics:

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

If the installer rejects autoinstall, fall back to debootstrap.

## Alternative — debootstrap + elementary PPA

```bash
sudo debootstrap --arch=amd64 jammy /mnt \
  http://archive.ubuntu.com/ubuntu
# elementary 7.x = Ubuntu 22.04 jammy
# elementary 8.x = Ubuntu 24.04 noble

sudo chroot /mnt /bin/bash <<'EOF'
add-apt-repository -y ppa:elementary-os/stable
apt update
apt install -y elementary-desktop pantheon-session pantheon-wallpapers \
  appcenter lightdm lightdm-pantheon-greeter

# ... standard Ubuntu-like post-install: user, ssh key, locale, grub
EOF
```

## Key packages

- `elementary-desktop` — the full Pantheon stack.
- `pantheon-session` — session manager.
- `appcenter` — elementary's curated app store (flatpak-first).
- `lightdm-pantheon-greeter` — the login screen.
- `switchboard` — elementary's system settings (not GNOME Control Center).

## What makes it elementary

- **Flatpak-first** — AppCenter shows only flatpaks (from flathub +
  elementary's own). The agent should prefer flatpak when installing user
  apps on elementary.
- **Pay-what-you-want apps** — AppCenter asks for donations; harmless but
  users should know it's optional.
- **Strict HIG** — icon + spacing consistency. Many apps reject patches that
  break it.
- **Multitasking view + Picture-in-Picture** — built-in, not extensions.

## Common pitfalls

- **GTK theming limited** — elementary hard-codes some app styles. Themes
  from gnome-look.org often half-apply.
- **`dock` is Plank** — customize via Plank preferences (`plank --preferences`).
- **No snap by default** — deliberate. Users wanting snap must install
  `snapd`.
- **Some Ubuntu PPAs conflict** — elementary's modified GNOME packages can
  break when PPAs install upstream GNOME versions. Use flatpak instead.
