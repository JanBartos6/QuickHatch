# Bazzite install primer

Atomic Fedora derivative built for gaming + Steam Deck-like console mode.
Immutable root filesystem (rpm-ostree), flatpak-first for apps. Based on
Fedora Silverblue + Universal Blue.

## Live environment

- Fedora-derived live ISO: boots to GNOME (desktop variant) or similar.
- Installer: Anaconda, same as Fedora.

## Approach — Anaconda with kickstart (like Fedora)

Bazzite installs via Anaconda with the immutable-variant repos baked in.
The kickstart is near-identical to Fedora's with one key swap:

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

# Key difference: ostreesetup pulls from Bazzite's OCI image
ostreesetup --osname="bazzite" --remote="bazzite" \
  --url="ostree-unverified-registry:ghcr.io/ublue-os/bazzite:latest" \
  --ref="bazzite:latest" \
  --nogpg

%post
# Pre-install SSH key into ostree home (layered above base image)
mkdir -p /home/<USER>/.ssh
echo "<PUBKEY>" > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Enable sudo for wheel (should be default but verify)
echo '%wheel ALL=(ALL) ALL' > /etc/sudoers.d/wheel
%end

reboot
KICKSTART

sudo anaconda --kickstart=/tmp/qh.ks
```

## After reboot — the different layer model

Bazzite is **immutable**. The agent can't just `dnf install vim`. Options:

1. **Flatpak (preferred for user apps):**
   ```bash
   flatpak install -y flathub org.mozilla.firefox
   ```

2. **rpm-ostree (system-level, requires reboot to activate):**
   ```bash
   rpm-ostree install <package>
   systemctl reboot
   ```

3. **Toolbox / distrobox (dev tools in a container):**
   ```bash
   distrobox create -n dev -i fedora:41
   distrobox enter dev
   # install anything normally inside
   ```

4. **Homebrew (macOS-style user-space):**
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

The agent should prefer flatpak > homebrew > distrobox > rpm-ostree, in that
order, for user-facing apps. `rpm-ostree` reboots are expensive.

## Common pitfalls

- **`dnf install` fails** — the root is read-only. Tell the user this is
  normal; use flatpak/distrobox instead.
- **Gaming defaults are already present** — Steam, Lutris, Heroic, MangoHud,
  gamemode, proton-ge are pre-installed. Agent doesn't need to add these.
- **Kernel parameters** — change via `rpm-ostree kargs --append=...`.
- **Desktop theming** — limited by the immutable model. Stylix-style whole-
  system theming doesn't work. Use per-app Flatpak themes + KDE/GNOME
  settings.
- **HTPC / Steam Deck modes** — Bazzite has a "Gaming Mode" session that
  boots into Steam Big Picture. Enable via greeter settings.
