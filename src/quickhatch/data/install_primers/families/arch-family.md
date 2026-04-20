# Arch-family install procedure (shared)

Shared install procedure for Arch Linux, EndeavourOS, CachyOS, and Garuda
Linux. The Arch installer is all-CLI (pacstrap + arch-chroot); derivatives
ship Calamares as an alternative but all of them can be installed with the
Arch-style procedure below by adding their repos to the live env before
pacstrap.

## Live environment

- Arch ISO: boots to `root` shell, no password.
- EndeavourOS / CachyOS / Garuda: boot to a DE with a Calamares installer
  on the desktop. Drop to TTY (Ctrl+Alt+F3) or terminal, sudo to root.

## Network

- Ethernet: auto-connects via `systemd-networkd`.
- WiFi: `iwctl station wlan0 connect <SSID>` (prompts for password).

## Preflight

```bash
timedatectl set-ntp true
[ -d /sys/firmware/efi/efivars ] && echo UEFI || echo BIOS
lsblk -b -d -n -o NAME,SIZE,TYPE   # pick target disk
```

## Partition (UEFI + GPT, EFI + root)

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt
mkdir /mnt/boot
mount "${DISK}p1" /mnt/boot
```

NVMe disks use `${DISK}p1` / `${DISK}p2`; SATA use `${DISK}1` / `${DISK}2`.

For Garuda specifically: swap ext4 for btrfs + the subvolume layout in the
Garuda card (snapshot-friendly). For all others, plain ext4 is fine.

## Add distro-specific repos BEFORE pacstrap

This is the one stage where derivative distros differ. Each distro card
has its own block:

- **Arch**: nothing to add.
- **EndeavourOS**: add `[endeavouros]` repo + `endeavouros-mirrorlist`.
- **CachyOS**: run `cachyos-repo.sh` to add `[cachyos]` + signing keys.
- **Garuda**: add `[chaotic-aur]` + `[garuda]` + their keyrings.

The per-distro card has the exact commands.

## Mirrors

```bash
reflector --country 'United States' --latest 10 --sort rate \
  --save /etc/pacman.d/mirrorlist
```

## Pacstrap the base system

```bash
# Pick CPU microcode based on vendor
UCODE=$(grep -qi intel /proc/cpuinfo && echo intel-ucode || echo amd-ucode)

# Base packages shared by all:
BASE="base linux linux-firmware sudo nano networkmanager openssh grub \
  efibootmgr sof-firmware $UCODE"

# Per-distro additions (from card):
#   Arch:         (nothing extra)
#   EndeavourOS:  endeavouros-mirrorlist endeavouros-keyring eos-bash-shared eos-welcome
#   CachyOS:      linux-cachyos linux-cachyos-headers cachyos-settings cachyos-keyring
#   Garuda:       linux-zen linux-zen-headers garuda-common-settings garuda-hooks \
#                 garuda-setup-assistant btrfs-progs snapper chaotic-keyring

pacstrap -K /mnt $BASE <PER_DISTRO_EXTRAS>

genfstab -U /mnt >> /mnt/etc/fstab
```

For CachyOS: skip `linux` (CachyOS kernel replaces it). On pre-Haswell CPUs
(no AVX2) use `linux` instead of `linux-cachyos`.

## Configure inside chroot

```bash
arch-chroot /mnt /bin/bash <<'EOF'
# --- 1. Enable distro-specific repos IN the installed system too ---
# (Copy or re-run whatever was used on the live env; see per-distro card)

# --- 2. Timezone + clock ---
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime
hwclock --systohc

# --- 3. Locale ---
sed -i 's/^#\(en_US.UTF-8\)/\1/' /etc/locale.gen
locale-gen
echo 'LANG=en_US.UTF-8' > /etc/locale.conf

# --- 4. Hostname + hosts ---
echo '<HOSTNAME>' > /etc/hostname
cat >> /etc/hosts <<HOSTS
127.0.0.1 localhost
::1       localhost
127.0.1.1 <HOSTNAME>.localdomain <HOSTNAME>
HOSTS

# --- 5. User + sudo ---
useradd -m -G wheel -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd
sed -i 's/^# \(%wheel ALL=(ALL:ALL) ALL\)/\1/' /etc/sudoers

# --- 6. SSH key (for agent reconnect after reboot) ---
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# --- 7. Services ---
systemctl enable NetworkManager sshd
# Garuda: also `systemctl enable snapper-timeline.timer snapper-cleanup.timer`

# --- 8. Bootloader ---
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=GRUB
grub-mkconfig -o /boot/grub/grub.cfg
# For BIOS: grub-install --target=i386-pc $DISK
EOF

umount -R /mnt
reboot
```

## Universal pitfalls

- **Secure Boot enabled** — grub-install may fail or the system won't boot.
  User must disable Secure Boot in BIOS; agent can't.
- **Wrong disk picked** — confirm with user via bridge chat before any
  `mkfs`. Show `lsblk -f` output.
- **`pacstrap` download stalls** — rerun `reflector` with a different
  country, or swap mirrors in `/etc/pacman.d/mirrorlist`.
- **Multiple kernels** — Garuda uses `linux-zen`, CachyOS uses
  `linux-cachyos`. Don't also install `linux` unless user wants multi-kernel
  grub entries.
- **AUR** — Arch itself doesn't ship an AUR helper. EndeavourOS has `yay`
  pre-installed. CachyOS has `paru`. Garuda has `paru`. Agent installs
  `yay` from AUR (build from source) if needed.

## Post-install gotchas seen in real runs

These aren't theoretical — each one bit a previous run until we documented it.

### Pacman DB signatures (`core: missing required signature`)

Arch deprecated DB signing years ago; modern mirrors often don't serve
`core.db.sig` / `extra.db.sig`. A fresh install whose `pacman.conf` has
`SigLevel = Required DatabaseRequired` will then fail every sync with
"missing required signature" and 404s on `*.db.sig`. Fix:

```
sudo sed -i 's/^SigLevel.*DatabaseRequired.*/SigLevel = Required DatabaseOptional/' /etc/pacman.conf
# OR, if no global SigLevel is set, explicitly add DatabaseNever in [options]:
sudo sed -i '/^\[options\]/a SigLevel = Required DatabaseNever' /etc/pacman.conf
# Then clear stale cached sync DBs and resync:
sudo rm -f /var/lib/pacman/sync/*.db*
sudo pacman -Syy --noconfirm
```

After this works, you can refresh the archlinux-keyring cleanly:
`sudo pacman -S --noconfirm archlinux-keyring`.

### Locally-built AUR packages failing to install (`missing required signature`)

When global `SigLevel = Required`, pacman rejects unsigned locally-built
AUR packages even though they just compiled successfully. This is the
same bit but in the opposite direction. Quick workaround for one-off
AUR installs:

```
sudo sed -i 's/^SigLevel = Required.*/SigLevel = Optional TrustAll/' /etc/pacman.conf
yay -S --noconfirm <pkgs>    # or pacman -U on the built .pkg.tar.zst
sudo sed -i 's/^SigLevel = Optional TrustAll/SigLevel = Required DatabaseNever/' /etc/pacman.conf
```

A better long-term choice is to sign the local repo, but that's overkill
for a one-shot migration.

### `$arch not defined` after editing `pacman.conf`

Symptom: `error: '$arch' is used in the mirrorlist but no 'Architecture'
is defined`. Fix: ensure `[options]` contains `Architecture = auto` (or
an explicit arch like `x86_64`). The Arch default `pacman.conf` has
`#Architecture = auto` commented out; a stray `sed` that removes the
line entirely causes this.

```
sudo sed -i '/^\[options\]/a Architecture = x86_64' /etc/pacman.conf
```

### Stuck pacman / yay / makepkg lock (`already running`, `file exists`)

Typical culprits:
- `/var/lib/pacman/db.lck` — left behind by a killed pacman. Remove if
  no pacman is actually running.
- `/tmp/*.lock` or `/tmp/<name>.lock/` — AUR packages (WhiteSur, some
  proton builds) use per-script lockfiles.
- `~/.cache/yay/<pkg>/` — partial build. `rm -rf` and retry.

### Non-fatal pacman warnings

`warning: directive 'Server' in section 'options' not recognized` /
`warning: config file ... Server not recognized` usually mean a mirror
list line ended up inside `[options]` instead of `[core]`. Annoying but
pacman proceeds successfully. **Don't chase this during install; the
migration still completes. Fix it at the end if you care.**

## Desktop gotchas

### Plasma 6 ships Wayland-only by default on Arch

`plasma-workspace` no longer provides `/usr/share/xsessions/plasma*.desktop`
in Plasma 6. If the user's hardware doesn't play nice with Wayland (common
on NVIDIA proprietary + older kernels, and on VirtualBox with `vboxvideo`)
the Wayland session black-screens on login. Fix:

```
sudo pacman -S --noconfirm plasma-x11-session
```

Restart SDDM; the Session dropdown will now include "Plasma (X11)".

### X11 session black screen — no video driver loaded

Xorg log (`/var/log/Xorg.0.log`) shows:

```
(EE) Failed to load module "vboxvideo"
(EE) Failed to load module "fbdev"
(EE) Failed to load module "vesa"
(EE) modeset(0): failed to set mode: Invalid argument
```

All three fallback drivers are missing AND Xorg couldn't settle on the
generic `modesetting` driver either. Fix:

```
sudo pacman -S --noconfirm xf86-video-vesa xf86-video-fbdev
```

For VirtualBox specifically, also install `virtualbox-guest-utils` and
enable `vboxservice`. To force the modesetting driver (works with any
KMS-capable GPU including the `vboxvideo` kernel module):

```
sudo mkdir -p /etc/X11/xorg.conf.d
sudo tee /etc/X11/xorg.conf.d/20-modesetting.conf >/dev/null <<EOF_X
Section "Device"
    Identifier "Default Modesetting"
    Driver     "modesetting"
EndSection
EOF_X
```

### Applying a Plasma theme from an SSH agent session

Writing to `~/.config/kdeglobals` with `kwriteconfig6` while the user is
NOT logged in works for some settings (Icons theme, cursor) but NOT for
the global Plasma look-and-feel, color scheme, or wallpaper — those
require a running plasmashell and DBUS. To apply live:

```
# As the logged-in user:
export DISPLAY=:0
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export XAUTHORITY=$(find /tmp -name 'xauth*' 2>/dev/null | head -1)
plasma-apply-lookandfeel -a <theme-id>
plasma-apply-colorscheme <scheme-name>
plasma-apply-desktoptheme <theme-name>
plasma-apply-wallpaperimage /path/to/image.jpg
qdbus6 org.kde.KWin /KWin reconfigure
```

If the user isn't logged in yet, write the config files and they'll be
picked up on next login. Icons + cursor + window decorations apply via
`kwriteconfig6` cleanly.

## Recovering an install that boots into emergency mode

Symptom on the target: kernel boots, then a wall of `[ TIME ]` and
`[DEPEND] failed` lines, ending with `You are in emergency mode` and
`Cannot open access to console, the root account is locked`.

Almost always one of three causes, all recoverable from the live USB:

### Cause 1 — `encrypt` hook missing from initramfs (most common)

Symptom: `Timed out waiting for device /dev/mapper/cryptroot` AND no
password prompt before the timeout.

Fix from chroot:
```
# Boot the live USB, then:
cryptsetup luksOpen /dev/sda2 cryptroot
mount /dev/mapper/cryptroot /mnt
mount /dev/sda1 /mnt/boot     # EFI partition
arch-chroot /mnt

# Verify HOOKS contains `encrypt` BETWEEN `block` and `filesystems`
grep ^HOOKS /etc/mkinitcpio.conf
# If missing, add it:
sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block encrypt filesystems fsck)/' /etc/mkinitcpio.conf

# Regenerate initramfs (THIS IS WHAT WAS MISSING)
mkinitcpio -P
```

### Cause 2 — Kernel cmdline missing `cryptdevice=`

Symptom: same as above (no password prompt, just timeout).

Fix from chroot:
```
# Get the LUKS container UUID
blkid /dev/sda2 -s UUID -o value
# → e.g. aa224704-7f05-45f5-8487-c7fddc8cd902

# Edit /etc/default/grub — GRUB_CMDLINE_LINUX_DEFAULT must include:
#   cryptdevice=UUID=<container-uuid>:cryptroot root=/dev/mapper/cryptroot
nano /etc/default/grub

# Then regenerate grub.cfg (NOT just /etc/default/grub on its own)
grub-mkconfig -o /boot/grub/grub.cfg
```

### Cause 3 — Root account locked AND no sudo-capable user

Symptom: any other failure becomes unrecoverable because sulogin
refuses to drop you into a shell.

Fix from chroot — set a root password:
```
passwd
```

### Prevention checklist (run BEFORE rebooting from chroot)

```
# All of these must be true:
grep ^HOOKS /etc/mkinitcpio.conf | grep -q encrypt        # encrypt hook present
grep ^GRUB_CMDLINE /etc/default/grub | grep -q cryptdevice # cmdline has it
[ /boot/initramfs-linux.img -nt /etc/mkinitcpio.conf ]    # initramfs is fresh
[ /boot/grub/grub.cfg -nt /etc/default/grub ]             # grub.cfg is fresh
awk -F: '$1=="root" && $2!~/^[!*]/' /etc/shadow            # root has a password
```

The QuickHatch harness now blocks the `reboot` command on the live
ISO if any of these checks fail, surfacing the specific failures back
to the agent. Pre-reboot is the only place to catch this — once you
reboot into a broken install, the only fix is re-mounting from the
live USB.
