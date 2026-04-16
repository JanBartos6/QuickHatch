# Gentoo Linux install primer

Source-based, maximally customizable. Portage package manager. Users choose
OpenRC or systemd, glibc or musl, stable or ~amd64 (testing). Install
is the most hands-on of any mainstream distro.

## Live environment

- Boots to root shell from the official Minimal Install or Admin Live ISO.
- Tools: `links`, `gpg`, `tar`, `chroot`, `cfdisk`, `parted`.
- Clock sync: `chronyd -q`.

## Approach — the classic Gentoo handbook install (condensed)

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt/gentoo
mkdir /mnt/gentoo/boot
mount "${DISK}p1" /mnt/gentoo/boot

cd /mnt/gentoo

# Pick stage3 tarball: openrc/systemd, desktop/nomultilib/...
# Agent preference for desktop users: stage3-amd64-desktop-systemd or
#                                     stage3-amd64-desktop-openrc
STAGE3_URL="https://distfiles.gentoo.org/releases/amd64/autobuilds/\
current-stage3-amd64-desktop-systemd/\
stage3-amd64-desktop-systemd-$(date +%Y%m%d)T.tar.xz"

wget "$STAGE3_URL" || \
  wget "$(wget -qO- https://distfiles.gentoo.org/releases/amd64/autobuilds/latest-stage3-amd64-desktop-systemd.txt | grep -v '^#' | awk '{print $1}' | head -1 | xargs -I{} echo https://distfiles.gentoo.org/releases/amd64/autobuilds/{})"

tar xpvf stage3-*.tar.xz --xattrs-include='*.*' --numeric-owner

# make.conf: set CPU flags, parallelism
cat > /mnt/gentoo/etc/portage/make.conf <<'EOF'
COMMON_FLAGS="-march=native -O2 -pipe"
CFLAGS="${COMMON_FLAGS}"
CXXFLAGS="${COMMON_FLAGS}"
FCFLAGS="${COMMON_FLAGS}"
FFLAGS="${COMMON_FLAGS}"
MAKEOPTS="-j$(nproc)"
EMERGE_DEFAULT_OPTS="--jobs=$(nproc) --load-average=$(nproc)"
ACCEPT_KEYWORDS="amd64"
ACCEPT_LICENSE="*"
VIDEO_CARDS="<intel|nvidia|amdgpu>"
INPUT_DEVICES="libinput"
USE="-bindist X pulseaudio networkmanager systemd dbus"
EOF

# Mirrors
mkdir -p /mnt/gentoo/etc/portage/repos.conf
cp /mnt/gentoo/usr/share/portage/config/repos.conf \
   /mnt/gentoo/etc/portage/repos.conf/gentoo.conf
cp /etc/resolv.conf /mnt/gentoo/etc/

# Bind mounts for chroot
mount --types proc /proc /mnt/gentoo/proc
mount --rbind /sys /mnt/gentoo/sys
mount --make-rslave /mnt/gentoo/sys
mount --rbind /dev /mnt/gentoo/dev
mount --make-rslave /mnt/gentoo/dev
mount --bind /run /mnt/gentoo/run
mount --make-slave /mnt/gentoo/run

chroot /mnt/gentoo /bin/bash <<'EOF'
source /etc/profile
export PS1="(chroot) $PS1"

# Sync portage tree
emerge-webrsync

# Profile selection (agent picks one based on use cases)
# Example: desktop systemd
eselect profile set default/linux/amd64/23.0/desktop/systemd

# Timezone
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime

# Locale
echo 'en_US.UTF-8 UTF-8' > /etc/locale.gen
locale-gen
eselect locale set en_US.utf8
env-update && source /etc/profile

# Hostname
echo '<HOSTNAME>' > /etc/hostname

# Kernel: use distribution kernel to skip manual config
emerge --quiet sys-kernel/gentoo-kernel-bin sys-kernel/linux-firmware

# fstab — write UUIDs for root + EFI
echo "UUID=$(blkid -s UUID -o value /dev/nvme0n1p2) / ext4 defaults,noatime 0 1" >> /etc/fstab
echo "UUID=$(blkid -s UUID -o value /dev/nvme0n1p1) /boot vfat defaults 0 2" >> /etc/fstab

# Networking + SSH
emerge --quiet net-misc/networkmanager net-misc/openssh
systemctl enable NetworkManager sshd

# Bootloader
emerge --quiet sys-boot/grub sys-boot/efibootmgr
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB
grub-mkconfig -o /boot/grub/grub.cfg

# User + sudo
emerge --quiet app-admin/sudo
useradd -m -G users,wheel,audio,video,input -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd
sed -i 's/^# \(%wheel ALL=(ALL:ALL) ALL\)/\1/' /etc/sudoers

# SSH key
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys
EOF

umount -R /mnt/gentoo
reboot
```

## Expected install time

**Hours.** Gentoo compiles packages from source. A full desktop install (DE,
kernel, Firefox, etc.) on a modern 16-thread CPU is typically 4–12 hours.

For faster install: use the **systemd desktop stage3 tarball** + the
distribution kernel (gentoo-kernel-bin is precompiled) + binhost (binary
package host, if enabled) to skip most compilation.

```
# In make.conf, enable binhost for precompiled packages
FEATURES="${FEATURES} getbinpkg"
```

With binhost enabled, install time drops to ~45 min.

## Common pitfalls

- **`-march=native` on a slower older CPU** — take-it-or-leave-it; agent
  should ask. Generic `-march=x86-64-v3` is safer if migrating.
- **Forgetting to emerge `sys-apps/systemd-utils`** — systemd-less stage3
  lacks tools that systemd-stage3 has.
- **`grub-install` EFI failure** — verify `/sys/firmware/efi/efivars` exists.
  If not, user's machine is BIOS; switch to `grub-install --target=i386-pc $DISK`.
- **Kernel config mismatch** — `gentoo-kernel-bin` is precompiled, so this
  avoids manual config. Don't pick `gentoo-sources` unless the user wants
  custom kernel.
- **USE flag conflicts** — emerges will block if global USE contradicts
  per-package USE. Agent handles: `--autounmask-write --autounmask=y`.
- **Extremely slow if no binhost + slow CPU** — in these cases, recommend
  a different distro ("You're installing Gentoo on an Intel Atom — this
  will take 2 days. Consider Debian or Arch instead").
