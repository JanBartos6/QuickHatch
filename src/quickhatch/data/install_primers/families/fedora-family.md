# Fedora-family install procedure (shared)

Shared install procedure for Fedora Workstation, Fedora Silverblue/Kinoite,
and Bazzite. All three use Anaconda (Red Hat's installer) driven by a
kickstart file. The kickstart body is ~85% identical across them; only the
**ostree source** (for atomic/immutable variants) and the pre-installed
package list differ.

## Live environment

- Fedora-derived live ISOs boot to a desktop (GNOME for Workstation/
  Silverblue, KDE for Kinoite, GNOME+gaming tweaks for Bazzite).
- Live user: `liveuser`, auto-logged-in, no password.
- Drop to TTY: Ctrl+Alt+F3, log in as `liveuser`.

## Network

- Ethernet auto via NetworkManager.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>`.

## Approach — Anaconda + kickstart (universal)

Kickstart is a declarative file Anaconda consumes. Same format across all
Fedora-family distros; only two things change per distro:

1. **Package-based vs ostree** — classic Fedora uses `%packages`; Silverblue
   / Kinoite / Bazzite use `ostreesetup`.
2. **Metapackage / DE** — Workstation ships GNOME, Kinoite ships KDE,
   Bazzite ships Fedora-Kinoite-based with gaming tweaks on top.

### Kickstart template

```
text
lang en_US.UTF-8
keyboard us
timezone <ZONE> --utc

# Passwords are crypt(3) SHA-512 hashes; generate with:
#   openssl passwd -6 '<plaintext>'
rootpw --iscrypted <ROOT_PASSWORD_HASH>
user --name=<USER> --groups=wheel --iscrypted --password=<PASSWORD_HASH>

# Disk
clearpart --all --drives=<DISK_SHORT>
autopart --type=plain
bootloader --location=mbr --boot-drive=<DISK_SHORT>

# Network
network --bootproto=dhcp --device=link --activate --hostname=<HOSTNAME>

# Services + SSH key
services --enabled=sshd,NetworkManager
sshkey --username=<USER> "<PUBKEY>"

# --- PACKAGE SOURCE: ONE of the following per distro card ---

# A. Classic Fedora (Workstation):
%packages
@core
@standard
@workstation-product
openssh-server
sudo
NetworkManager
%end

# B. Silverblue (immutable, stock Fedora ostree):
# ostreesetup --osname="fedora" --remote="fedora" \
#   --url="https://ostree.fedoraproject.org" \
#   --ref="fedora/41/x86_64/silverblue" \
#   --nogpg

# C. Kinoite (KDE variant of Silverblue):
# ostreesetup --osname="fedora" --remote="fedora" \
#   --url="https://ostree.fedoraproject.org" \
#   --ref="fedora/41/x86_64/kinoite" \
#   --nogpg

# D. Bazzite (OCI image from Universal Blue):
# ostreesetup --osname="bazzite" --remote="bazzite" \
#   --url="ostree-unverified-registry:ghcr.io/ublue-os/bazzite:latest" \
#   --ref="bazzite:latest" \
#   --nogpg

# --- POST-INSTALL (mostly shared) ---
%post
mkdir -p /home/<USER>/.ssh
echo "<PUBKEY>" > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Ensure wheel group has sudo (usually default, but verify)
echo '%wheel ALL=(ALL) ALL' > /etc/sudoers.d/wheel
%end

reboot
```

### Run it

```bash
sudo anaconda --kickstart=/tmp/qh.ks
```

Anaconda handles partitioning, package install (or ostree pull),
bootloader, user creation, then reboots.

## Alternative — chroot install via dnf --installroot (classic Fedora only)

If Anaconda fails, install directly via dnf:

```bash
DISK=/dev/nvme0n1
# Partition as above
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt
mkdir /mnt/boot
mount "${DISK}p1" /mnt/boot

sudo dnf --installroot=/mnt --releasever=<VERSION> install -y \
  @core @standard kernel dnf sudo NetworkManager openssh-server \
  grub2 grub2-efi-x64 efibootmgr shim-x64

# Chroot and configure (locale, user, ssh key, grub) as in the Debian
# primer — the commands are nearly identical, just s/apt install/dnf
# install/.
```

Does NOT apply to Silverblue/Kinoite/Bazzite — those are ostree-only, not
dnf. Use the kickstart path for immutable variants.

## Post-install for atomic variants (Silverblue/Kinoite/Bazzite)

Agent reconnects after reboot. Root is read-only. Install user apps via:

1. **Flatpak** (preferred): `flatpak install -y flathub <app>`
2. **Toolbox** (dev tools): `toolbox create -r 41; toolbox enter`
3. **Distrobox** (richer containers): `distrobox create -n dev -i fedora:41`
4. **rpm-ostree layer** (system, reboot required): `rpm-ostree install <pkg>`

Preference order: **flatpak > toolbox/distrobox > rpm-ostree**.

## Universal pitfalls

- **`--iscrypted` requires crypt(3) SHA-512 hashes, not plaintext.** Agent
  generates with `openssl passwd -6 '<password>'`.
- **Version pinning** — `--releasever=<N>` must match the Fedora release in
  use (41, 42, ...). Agent reads `/etc/fedora-release` on the live env.
- **Secure Boot** — Fedora's shim is signed; works out of the box on most
  hardware.
- **NVIDIA drivers** — Classic Fedora: add RPMFusion, `dnf install
  akmod-nvidia xorg-x11-drv-nvidia`. Silverblue/Kinoite: `rpm-ostree
  install akmod-nvidia xorg-x11-drv-nvidia` then reboot. Bazzite already
  ships NVIDIA builds.
- **rpm-ostree changes need reboot** — every layered install on immutable
  variants is staged into the next deployment. Stack multiple installs
  into one reboot.
- **SELinux** — enforcing by default. Leave it enforcing unless user
  explicitly asks otherwise; silently disabling bites later.
