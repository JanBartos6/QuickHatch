# Garuda Linux install primer

Arch-based, gaming-focused, pre-riced (fancy GRUB theme, bootloader splash,
themed GRUB menu, Dragonized/Dr460nized KDE as default look). Uses the
Chaotic-AUR repo for pre-built AUR packages and ships zen kernel + zfs/btrfs
snapshotting by default.

## Live environment

- Boots to KDE Plasma as `garuda` user.
- Installer: Calamares.

## Approach — pacstrap with Garuda repos

Add Chaotic-AUR + Garuda's own repo to the live env, then pacstrap:

```bash
# Add Chaotic-AUR keys + repo (used by Garuda for pre-built AUR)
pacman-key --recv-key 3056513887B78AEB --keyserver keyserver.ubuntu.com
pacman-key --lsign-key 3056513887B78AEB
pacman -U 'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst' \
        'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'

cat >> /etc/pacman.conf <<'EOF'
[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist

[garuda]
SigLevel = Required DatabaseOptional
Server = https://repos.garudalinux.org/garuda/$arch
EOF

# Partition (Garuda's calamares sets up btrfs with subvolumes for snapshots;
# for simplicity, the agent can skip subvolumes or emulate them).

DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary btrfs 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.btrfs -L garuda -f "${DISK}p2"

# Btrfs subvolumes matching Garuda's layout
mount "${DISK}p2" /mnt
btrfs su cr /mnt/@
btrfs su cr /mnt/@home
btrfs su cr /mnt/@root
btrfs su cr /mnt/@srv
btrfs su cr /mnt/@cache
btrfs su cr /mnt/@log
btrfs su cr /mnt/@tmp
umount /mnt
mount -o subvol=@,compress=zstd "${DISK}p2" /mnt
mkdir -p /mnt/{boot,home,root,srv,var/cache,var/log,var/tmp}
mount -o subvol=@home,compress=zstd "${DISK}p2" /mnt/home
mount -o subvol=@root,compress=zstd "${DISK}p2" /mnt/root
mount -o subvol=@srv,compress=zstd "${DISK}p2" /mnt/srv
mount -o subvol=@cache,compress=zstd "${DISK}p2" /mnt/var/cache
mount -o subvol=@log,compress=zstd "${DISK}p2" /mnt/var/log
mount -o subvol=@tmp,compress=zstd "${DISK}p2" /mnt/var/tmp
mount "${DISK}p1" /mnt/boot

# Pacstrap with Garuda's flavor metapackage
pacstrap -K /mnt base linux-zen linux-zen-headers linux-firmware \
  sudo networkmanager openssh grub efibootmgr sof-firmware \
  intel-ucode garuda-common-settings garuda-hooks \
  garuda-setup-assistant btrfs-progs snapper \
  chaotic-keyring chaotic-mirrorlist

# Flavor metapackage (pick one)
# Dragonized KDE (default):  pacstrap /mnt garuda-dr460nized
# Sway:                      pacstrap /mnt garuda-sway
# Hyprland:                  pacstrap /mnt garuda-hyprland
# BSPWM:                     pacstrap /mnt garuda-bspwm

genfstab -U /mnt >> /mnt/etc/fstab

# chroot — standard Arch-style setup plus Garuda's services
arch-chroot /mnt /bin/bash <<'EOF'
# ... (timezone, locale, hostname, user, ssh key — as Arch primer)

systemctl enable NetworkManager sshd
systemctl enable snapper-timeline.timer snapper-cleanup.timer   # snapshotting

grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=garuda
grub-mkconfig -o /boot/grub/grub.cfg
EOF

umount -R /mnt
reboot
```

## Common pitfalls

- **Chaotic-AUR outages** — the mirror occasionally fails. Have a backup
  mirror or skip Chaotic (user can still install AUR packages later via
  `paru`/`yay`).
- **Btrfs subvolume layout** — Garuda expects a specific one for Timeshift
  snapshots. Simplifying the layout breaks snapshots; keep it as shown.
- **Zen kernel + NVIDIA** — `nvidia-dkms` generally works but may need
  `nvidia-open` or `nvidia-lts` depending on GPU generation.
- **Fancy GRUB theme** — Garuda's GRUB has animated backgrounds. If user
  complains about boot delay, set `GRUB_TIMEOUT=0` in `/etc/default/grub`.
- **Gaming metapackage** — `garuda-gamer` pulls in Steam, Lutris, Heroic,
  Wine, etc. Install separately if user wants these.
