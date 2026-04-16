# Arch Linux install primer

Rolling release, minimal base, CLI install. Official ISO boots to a root shell.
The agent installs from the live ISO by running the standard Arch wiki procedure.

## Live environment

- Boots to a root shell as `root`, no password needed.
- Utilities already present: `parted`, `mkfs.*`, `pacstrap`, `arch-chroot`,
  `reflector`, `iwctl`, `openssh`.

## Network

- Ethernet auto-connects via `systemd-networkd`.
- WiFi: `iwctl station wlan0 connect <SSID>` (password prompt). Verify with
  `ping -c1 archlinux.org`.

## Preflight

```bash
timedatectl set-ntp true
[ -d /sys/firmware/efi/efivars ] && echo UEFI || echo BIOS
lsblk -b -d -n -o NAME,SIZE,TYPE   # pick target disk
```

## Partition (UEFI + GPT, EFI + root)

```bash
DISK=/dev/nvme0n1   # substitute actual
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt
mkdir /mnt/boot
mount "${DISK}p1" /mnt/boot
```

For NVMe use `${DISK}p1`, for SATA use `${DISK}1`. Adjust accordingly.

## Mirrors + base install

```bash
reflector --country 'United States' --latest 10 --sort rate \
  --save /etc/pacman.d/mirrorlist

# Select microcode: intel-ucode or amd-ucode based on /proc/cpuinfo
UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)

pacstrap -K /mnt base linux linux-firmware sudo nano networkmanager \
  openssh grub efibootmgr sof-firmware "$UCODE"

genfstab -U /mnt >> /mnt/etc/fstab
```

## Configure (chroot)

```bash
arch-chroot /mnt /bin/bash <<'EOF'
# Timezone + clock
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime
hwclock --systohc

# Locale
sed -i 's/^#\(en_US.UTF-8\)/\1/' /etc/locale.gen
locale-gen
echo 'LANG=en_US.UTF-8' > /etc/locale.conf

# Hostname + hosts
echo '<HOSTNAME>' > /etc/hostname
cat >> /etc/hosts <<HOSTS
127.0.0.1 localhost
::1       localhost
127.0.1.1 <HOSTNAME>.localdomain <HOSTNAME>
HOSTS

# User + sudo
useradd -m -G wheel -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd
sed -i 's/^# \(%wheel ALL=(ALL:ALL) ALL\)/\1/' /etc/sudoers

# SSH key for reconnect after reboot
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Services
systemctl enable NetworkManager sshd

# Bootloader (UEFI)
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB
grub-mkconfig -o /boot/grub/grub.cfg
EOF

umount -R /mnt
reboot
```

For BIOS machines: `grub-install --target=i386-pc $DISK` instead.

## Common pitfalls

- **Secure Boot enabled** → grub-install may fail or the system won't boot.
  User must disable Secure Boot in BIOS; agent can't. Error out clearly.
- **`pacstrap` download stalls** → retry, or rerun `reflector` with different
  country.
- **Wrong disk picked** → confirm with user via bridge chat before any
  `mkfs`. Show `lsblk -f` output for context.
- **Dual-boot intended** → this primer assumes single-boot (wipes disk). For
  dual-boot, different partition scheme needed; ask user to confirm first.
