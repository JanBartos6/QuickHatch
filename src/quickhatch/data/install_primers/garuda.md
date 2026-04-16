# Garuda Linux card

Family: **arch** — uses `families/arch-family.md` as the base procedure.
Arch-based, gaming-focused, pre-riced (fancy GRUB theme, KDE Dr460nized,
btrfs + snapper snapshots by default, Chaotic-AUR enabled).

## Per-distro values

| Key | Value |
|---|---|
| Live env | KDE Plasma as `garuda` user |
| Extra repos | `[chaotic-aur]`, `[garuda]` |
| Pacstrap kernel | `linux-zen` + `linux-zen-headers` (not the stock `linux`) |
| Pacstrap extras | `garuda-common-settings garuda-hooks garuda-setup-assistant btrfs-progs snapper chaotic-keyring chaotic-mirrorlist` + the chosen flavor metapackage |
| Filesystem | **btrfs with subvolumes** (not ext4) — snapper-compatible layout |
| Flavor metapackages | `garuda-dr460nized` (KDE, default), `garuda-sway`, `garuda-hyprland`, `garuda-bspwm`, `garuda-gnome`, `garuda-xfce` |
| Installer alternative | Calamares; prefer pacstrap path from family primer |
| AUR helper | `paru` preinstalled |
| sudo group | `wheel` |

## Repo + key setup (add BEFORE pacstrap)

```bash
# Chaotic-AUR (used by Garuda for pre-built AUR packages)
pacman-key --recv-key 3056513887B78AEB --keyserver keyserver.ubuntu.com
pacman-key --lsign-key 3056513887B78AEB
pacman -U \
  'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst' \
  'https://cdn-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst'

cat >> /etc/pacman.conf <<'EOF'
[chaotic-aur]
Include = /etc/pacman.d/chaotic-mirrorlist

[garuda]
SigLevel = Required DatabaseOptional
Server = https://repos.garudalinux.org/garuda/$arch
EOF
```

## Partition layout (btrfs + subvolumes)

Override the family primer's ext4 partitioning with btrfs:

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary btrfs 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.btrfs -L garuda -f "${DISK}p2"

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
```

Post-install, ALSO enable snapper timers inside the chroot:

```bash
systemctl enable snapper-timeline.timer snapper-cleanup.timer
```

## Distro-specific pitfalls

- **Btrfs subvolume layout** — Garuda expects the exact layout above for
  Timeshift snapshots. Simplifying breaks snapshots; keep as shown.
- **Chaotic-AUR outages** — occasional; have a backup mirror or skip
  chaotic (`paru` can still build from AUR source).
- **linux-zen + NVIDIA** — `nvidia-dkms` generally works; `nvidia-open`
  or `nvidia-lts` may be needed for specific GPU generations.
- **Fancy GRUB theme** — boot delay annoyance. Set `GRUB_TIMEOUT=0` in
  `/etc/default/grub` to skip.
- **`garuda-gamer` metapackage** — Steam, Lutris, Heroic, Wine, etc.
  Install separately if user wants these (not auto-pulled by flavor).
