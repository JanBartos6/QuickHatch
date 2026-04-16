# Fedora Silverblue / Kinoite install primer

Immutable Fedora (same base as Bazzite/uBlue, but vanilla). Silverblue ships
GNOME; Kinoite ships KDE Plasma. Root is read-only, managed via `rpm-ostree`.

## Live environment

- Fedora-derived live ISO; boots to chosen DE as `liveuser`.
- Installer: Anaconda.

## Approach — Anaconda + kickstart (same as Fedora) with ostree twist

```bash
cat > /tmp/qh.ks <<'KICKSTART'
text
lang en_US.UTF-8
keyboard us
timezone <ZONE> --utc

rootpw --iscrypted <ROOT_PASSWORD_HASH>
user --name=<USER> --groups=wheel --iscrypted --password=<PASSWORD_HASH>

clearpart --all --drives=<DISK_SHORT>
autopart --type=plain
bootloader --location=mbr --boot-drive=<DISK_SHORT>

network --bootproto=dhcp --device=link --activate --hostname=<HOSTNAME>

services --enabled=sshd
sshkey --username=<USER> "<PUBKEY>"

# Ostree source — Silverblue
ostreesetup --osname="fedora" --remote="fedora" \
  --url="https://ostree.fedoraproject.org" \
  --ref="fedora/41/x86_64/silverblue" \
  --nogpg

# For Kinoite: --ref="fedora/41/x86_64/kinoite"

%post
mkdir -p /home/<USER>/.ssh
echo "<PUBKEY>" > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys
%end

reboot
KICKSTART

sudo anaconda --kickstart=/tmp/qh.ks
```

## Post-install — the immutable model

Same as Bazzite: the root is read-only. Install apps via:

1. **Flatpak** (apps): `flatpak install flathub org.mozilla.firefox`
2. **Toolbox** (CLI dev tools): `toolbox create -r 41` → `toolbox enter`
3. **Distrobox** (richer containers): `distrobox create -n dev -i fedora:41`
4. **rpm-ostree layer** (system-level, reboot required):
   `rpm-ostree install <pkg>` then `systemctl reboot`
5. **Homebrew** (macOS-style userspace): available via the standard
   curl installer.

Agent preference order: **flatpak > toolbox/distrobox > homebrew > rpm-ostree**.

## Silverblue vs Kinoite

| Feature | Silverblue  | Kinoite |
|---------|-------------|---------|
| DE      | GNOME       | KDE Plasma |
| Theming | Adwaita     | Breeze |
| Best for| GNOME lovers, GNOME OS feel | Plasma lovers, customizable immutable |

## Common pitfalls

- **"Cannot install package"** — root is immutable; use flatpak/toolbox
  instead. Tell the user this is normal.
- **Kernel param changes** — use `rpm-ostree kargs --append=...`; direct
  edits to `/etc/default/grub` don't stick.
- **GPU drivers** — NVIDIA proprietary driver via rpm-ostree layering:
  `rpm-ostree install akmod-nvidia xorg-x11-drv-nvidia`. Requires
  RPMFusion repo enabled first.
- **User-level theming** — works fine (gtk-4.0 configs, KDE themes in
  `~/.config`); system theme changes don't.
- **Rollback is cheap** — `rpm-ostree rollback` reverts to the previous
  deployment. Good safety net for experiments.
