#!/bin/bash
# Install Arch Linux to /dev/sda with EFI + ext4
# Run this from inside the Arch live ISO as root
set -e
set -x

# Give cowspace room just in case pacstrap needs it
mount -o remount,size=8G /run/archiso/cowspace || true

# --- Partition /dev/sda ---
# 512MB EFI + rest ext4 root
parted --script /dev/sda \
    mklabel gpt \
    mkpart ESP fat32 1MiB 513MiB \
    set 1 esp on \
    mkpart primary ext4 513MiB 100%

mkfs.fat -F32 /dev/sda1
mkfs.ext4 -F /dev/sda2

# --- Mount ---
mount /dev/sda2 /mnt
mkdir -p /mnt/boot
mount /dev/sda1 /mnt/boot

# --- Sync and pacstrap ---
pacman-key --init 2>/dev/null || true
pacman-key --populate archlinux 2>/dev/null || true
pacman -Sy --noconfirm

# Base system + common tools + bootloader + network + SSH
pacstrap -K /mnt \
    base base-devel linux linux-firmware \
    grub efibootmgr \
    networkmanager \
    openssh \
    sudo vim nano git \
    dhcpcd

# --- fstab ---
genfstab -U /mnt >> /mnt/etc/fstab

# --- Chroot config ---
arch-chroot /mnt /bin/bash <<'CHROOT'
set -e
# Locale + timezone
ln -sf /usr/share/zoneinfo/UTC /etc/localtime
hwclock --systohc
echo "en_US.UTF-8 UTF-8" > /etc/locale.gen
locale-gen
echo "LANG=en_US.UTF-8" > /etc/locale.conf

# Hostname
echo "quickhatch" > /etc/hostname
cat >> /etc/hosts <<HOSTS
127.0.0.1   localhost
::1         localhost
127.0.1.1   quickhatch.localdomain quickhatch
HOSTS

# Root password = "password" (for testing only)
echo "root:password" | chpasswd

# Bootloader (UEFI)
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB --removable
grub-mkconfig -o /boot/grub/grub.cfg

# Enable essential services
systemctl enable NetworkManager
systemctl enable sshd

# Allow root SSH login with password AND keys for testing
sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config

# Add SSH key for passwordless agent access
mkdir -p /root/.ssh
chmod 700 /root/.ssh
CHROOT

# Inject the SSH key
mkdir -p /mnt/root/.ssh
chmod 700 /mnt/root/.ssh

echo "=== INSTALL COMPLETE ==="
echo "Next: inject SSH key, unmount, reboot"
