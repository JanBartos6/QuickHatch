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

## Approach A — QuickHatch supported path: install first, then configure

For this tranche, QuickHatch should not attempt a full Fedora Workstation OS
install from a running live desktop over SSH. Running Anaconda/Kickstart as a
normal SSH command from a live Workstation ISO is not reliable: local
`--kickstart` is not supported for live installations and text/cmdline runs can
block on installer spokes with no stdin. A direct `dnf --installroot` install is
also too fragile across BIOS/UEFI, bootloader, repo, and live-root storage
state to be the default automation path.

Supported flow:

1. User boots the Fedora Workstation ISO.
2. User runs the graphical installer manually and reboots into the installed
   system.
3. User runs `bash quickstart.sh` on the installed Fedora system.
4. QuickHatch reconnects and performs drivers, desktop, apps, display, and
   final audit work in configure-only mode.

If the model is connected to a Fedora-family live ISO, it should ask for this
manual installer handoff instead of emitting disk partitioning, Anaconda, or
`dnf --installroot` command groups.

## Experimental — classic Fedora via `dnf --installroot`

This is not the QuickHatch default. Keep it for future implementation notes and
expert debugging only.

```bash
DISK=/dev/sda
REL="$(. /etc/os-release; echo "${VERSION_ID%%.*}")"

sudo swapoff -a || true
sudo umount -R /mnt 2>/dev/null || true
sudo wipefs -a "$DISK"
sudo parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
sudo mkfs.fat -F32 "${DISK}1"
sudo mkfs.ext4 -F "${DISK}2"
sudo mount "${DISK}2" /mnt
sudo mkdir -p /mnt/boot/efi
sudo mount "${DISK}1" /mnt/boot/efi

sudo dnf --installroot=/mnt --releasever="$REL" --setopt=install_weak_deps=False \
  install -y @core @standard @workstation-product @gnome-desktop \
  kernel-core kernel-modules kernel-modules-core linux-firmware \
  grub2-efi-x64 shim-x64 efibootmgr sudo passwd NetworkManager openssh-server

for f in dev proc sys run; do sudo mount --rbind /$f /mnt/$f; sudo mount --make-rslave /mnt/$f; done
sudo chroot /mnt /bin/bash -c "
  ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime
  echo '<HOSTNAME>' > /etc/hostname
  useradd -m -G wheel -s /bin/bash <USER>
  echo '<USER>:<PASSWORD>' | chpasswd
  echo 'root:<ROOT_PASSWORD>' | chpasswd
  echo '%wheel ALL=(ALL) ALL' > /etc/sudoers.d/wheel
  mkdir -p /home/<USER>/.ssh
  echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
  chown -R <USER>:<USER> /home/<USER>/.ssh
  chmod 700 /home/<USER>/.ssh
  chmod 600 /home/<USER>/.ssh/authorized_keys
  systemctl enable NetworkManager sshd gdm
  grub2-mkconfig -o /boot/grub2/grub.cfg
  mkdir -p /boot/efi/EFI/BOOT
  cp -f /boot/efi/EFI/fedora/shimx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI 2>/dev/null || true
  cp -f /boot/efi/EFI/fedora/grubx64.efi /boot/efi/EFI/BOOT/grubx64.efi 2>/dev/null || true
"
```

Do not use this path in generated plans unless QuickHatch grows a typed Fedora
install executor that owns the partition layout, bootloader mode, chroot bind
mounts, repo cache location, SSH continuity, bootability guard, unmount, reboot,
and reconnect as first-class code.

## Approach B — Anaconda + kickstart (boot-time or manual installer)

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
#   --ref="fedora/<VERSION>/x86_64/silverblue" \
#   --nogpg

# C. Kinoite (KDE variant of Silverblue):
# ostreesetup --osname="fedora" --remote="fedora" \
#   --url="https://ostree.fedoraproject.org" \
#   --ref="fedora/<VERSION>/x86_64/kinoite" \
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
# Use this at boot time or through the graphical installer workflow, not as a
# normal QuickHatch SSH command from a live Workstation session.
inst.ks=file:/path/to/qh.ks
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
- **Version pinning** — `--releasever=<N>` and ostree refs must match the
  Fedora release in use. Read `/etc/os-release`, `/etc/fedora-release`, or
  `rpm -E %fedora`; never hardcode a "current" Fedora number in a plan.
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
