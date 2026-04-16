# Linux Mint install primer

Ubuntu LTS base + Cinnamon (default) / MATE / XFCE. Friendly out-of-box
experience: proprietary codecs, NVIDIA drivers, GUI everywhere.

## Live environment

- Boots to chosen DE as `mint` user, no password.
- Installer icon on the desktop: "Install Linux Mint" (Ubiquity-based).

## Approach — autoinstall (same as Ubuntu)

Mint uses Ubiquity, which consumes the same `user-data` format as Ubuntu:

```yaml
# /tmp/user-data
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
  shutdown: reboot
```

```bash
sudo ubiquity --automation-file=/tmp/user-data
```

## Alternative — chroot install from live (debootstrap)

Same pattern as Ubuntu primer, with Mint's package repo:

```bash
sudo debootstrap --arch=amd64 jammy /mnt \
  http://archive.ubuntu.com/ubuntu
# Mint 22 Wilma = Ubuntu 24.04 noble
# Mint 21.x Vanessa/Victoria/etc. = Ubuntu 22.04 jammy

sudo chroot /mnt /bin/bash <<'EOF'
# Add Mint's repo
echo 'deb http://packages.linuxmint.com wilma main upstream import backport' \
  > /etc/apt/sources.list.d/mint.list
apt-key adv --keyserver keyserver.ubuntu.com --recv A6616109451BBBF2
apt update
apt install -y mint-meta-cinnamon   # or mint-meta-xfce / mint-meta-mate
# ... rest matches Ubuntu primer (user, ssh key, grub, etc.)
EOF
```

## Key differences from Ubuntu

- **No snaps** — Mint deliberately blocks snapd. Firefox is a deb via the
  `mozillateam` PPA.
- **Cinnamon default** — `mint-meta-cinnamon` metapackage pulls it in.
- **MintTools** — Update Manager, Driver Manager, Software Manager. If agent
  pre-installs NVIDIA drivers, these will still offer them; harmless.
- **Codecs preinstalled** — no separate "ubuntu-restricted-extras" step.

## Common pitfalls

- **Mint uses an older Ubuntu base** — packages available in Ubuntu's current
  release may not be in Mint's base. Check package versions before assuming.
- **PPA compatibility** — use the matching Ubuntu release name in PPAs (e.g.
  `jammy` for Mint 21.x), not `wilma`.
- **Secure Boot** — Mint is signed; works out of the box on most hardware.
