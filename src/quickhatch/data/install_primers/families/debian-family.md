# Debian-family install procedure (shared)

This is the universal install procedure shared across Debian, Ubuntu, and
every Ubuntu derivative (Mint, Pop!_OS, Zorin, elementary, KDE Neon, Kali).
Each specific distro's card below tells you which **release codename**,
**mirror URL**, **extra repo/PPA**, and **metapackage** to substitute in.

The LLM should use this procedure unless the distro card says otherwise.

## Live environment

- Most Debian-family live ISOs boot to a desktop session or a `boot:` prompt
  for the installer.
- The live user varies (`ubuntu`, `live`, `mint`, `kali`, `pop`) — distro
  card will say. Sudo works without a password.

## Network

- Ethernet auto-connects via NetworkManager on desktop ISOs, via netplan or
  systemd-networkd on server ISOs.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>`.

## Three install paths — pick what works

### Path A: Official unattended installer (fastest when available)

- **Debian**: d-i with preseed.cfg — see the per-distro card.
- **Ubuntu**: subiquity (server) / ubiquity (desktop) with cloud-init
  `user-data`.
- **Mint / Zorin / elementary**: forked ubiquity, same `user-data` format.
- **Pop!_OS**: `distinst` CLI.
- **KDE Neon**: Calamares (limited automation — fall back to Path B).
- **Kali**: d-i with preseed.cfg (Debian-compatible, Kali mirror).

Per-distro card has the installer config template.

### Path B: debootstrap (universal, QuickHatch-friendly)

This path works on any Debian-family live env and is the preferred QuickHatch
path for Ubuntu Desktop live installs over SSH. Use it when the distro's
graphical installer is awkward to automate or would need an interactive GUI.

QuickHatch command items run as separate SSH executions, so do not use heredocs
or standalone shell variables. Keep each command self-contained, and put every
disk/base/chroot/bootloader command under `install_or_skip`.

```bash
DISK=/dev/nvme0n1
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}p1"
mkfs.ext4 -F "${DISK}p2"
mount "${DISK}p2" /mnt
mkdir -p /mnt/boot/efi
mount "${DISK}p1" /mnt/boot/efi

Keep `/boot` on the Linux root filesystem for Debian/Ubuntu-family installs.
Do not mount the FAT32 ESP directly at `/boot`; Ubuntu kernel postinst scripts
create `/boot/vmlinuz*` symlinks, and FAT32 cannot support those symlinks.

# Substitute <RELEASE> and <MIRROR> from the per-distro card:
#   Debian:       bookworm   http://deb.debian.org/debian
#   Ubuntu:       noble      http://archive.ubuntu.com/ubuntu
#   Mint (base):  noble      http://archive.ubuntu.com/ubuntu
#   Pop!_OS:      noble      http://apt.pop-os.org/ubuntu
#   Zorin (base): noble      http://archive.ubuntu.com/ubuntu
#   elementary:   noble      http://archive.ubuntu.com/ubuntu
#   KDE Neon:     jammy      http://archive.ubuntu.com/ubuntu
#   Kali:         kali-rolling  http://http.kali.org/kali
sudo debootstrap --arch=amd64 <RELEASE> /mnt <MIRROR>

# Bind-mount system dirs before chroot. The harness can inject this before
# chroot commands when it sees a Debian-family live debootstrap plan, but it is
# safe to include explicitly.
sudo sh -c 'target=/mnt; test -d "$target/etc" && cp -L /etc/resolv.conf "$target/etc/resolv.conf" || true; for d in dev dev/pts proc sys run; do mkdir -p "$target/$d"; mountpoint -q "$target/$d" || mount --bind "/$d" "$target/$d"; done'
```

Now chroot and configure. Use `chroot /mnt sh -c '...'` batches, not heredocs.
The base packages needed are the same everywhere:

```bash
sudo chroot /mnt sh -c "apt update"
sudo chroot /mnt sh -c "apt install -y linux-generic systemd-sysv sudo openssh-server network-manager locales curl grub-efi-amd64 efibootmgr"
sudo chroot /mnt sh -c "apt install -y <METAPACKAGE>"
sudo chroot /mnt sh -c "locale-gen en_US.UTF-8 && echo LANG=en_US.UTF-8 > /etc/default/locale"
sudo chroot /mnt sh -c "echo '<HOSTNAME>' > /etc/hostname && printf '127.0.0.1 localhost\n127.0.1.1 <HOSTNAME>.localdomain <HOSTNAME>\n' > /etc/hosts"
sudo chroot /mnt sh -c "ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime"
sudo chroot /mnt sh -c "id <USER> >/dev/null 2>&1 || useradd -m -G sudo -s /bin/bash <USER>; echo '<USER>:<PASSWORD>' | chpasswd; echo 'root:<ROOT_PASSWORD>' | chpasswd"
sudo sh -c "install -d -m 700 /mnt/home/<USER>/.ssh && printf '%s\n' '<PUBKEY>' > /mnt/home/<USER>/.ssh/authorized_keys && chown -R <USER>:<USER> /mnt/home/<USER>/.ssh && chmod 600 /mnt/home/<USER>/.ssh/authorized_keys"
sudo chroot /mnt sh -c "systemctl enable ssh NetworkManager"
sudo sh -c 'ROOT_UUID=$(blkid -s UUID -o value /dev/<ROOT_PARTITION>); BOOT_UUID=$(blkid -s UUID -o value /dev/<BOOT_PARTITION>); printf "UUID=%s / ext4 defaults 0 1\nUUID=%s /boot/efi vfat defaults 0 2\n" "$ROOT_UUID" "$BOOT_UUID" > /mnt/etc/fstab'
sudo chroot /mnt sh -c "grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu && update-initramfs -u -k all && update-grub"
```

For BIOS machines swap `grub-efi-amd64 efibootmgr` for `grub-pc`, and use the
matching `grub-install /dev/<DISK>` command. Do not include the final
`umount -R /mnt` or `reboot` in the plan; QuickHatch runs guarded boot checks,
copies/verifies the SSH key, unmounts, reboots, and reconnects.

### Path C: cloud-init autoinstall YAML (Ubuntu + derivatives only)

```yaml
#cloud-config
autoinstall:
  version: 1
  identity:
    realname: "<REAL_NAME>"
    username: <USER>
    password: "<HASHED_PASSWORD>"   # mkpasswd -m sha-512
    hostname: <HOSTNAME>
  ssh:
    install-server: true
    authorized-keys:
      - "<PUBKEY>"
  packages:
    - sudo
    - openssh-server
    - network-manager
  storage:
    layout:
      name: direct
  late-commands:
    - curtin in-target --target=/target -- systemctl enable ssh NetworkManager
    - curtin in-target --target=/target -- usermod -aG sudo <USER>
  shutdown: reboot
```

Boot the installer with `autoinstall ds=nocloud;s=file:///<path>/`.

## Universal pitfalls

- **`systemd-sysv` missing after debootstrap** — always include it in the
  base install, or the system has no init.
- **Secure Boot** — Debian-family distros ship signed shims; works on most
  hardware. If `grub-install` fails with SB enabled, disable it in BIOS.
- **non-free-firmware** — needed for most WiFi + NVIDIA proprietary drivers.
  Not in Debian main; add `contrib non-free non-free-firmware` components.
  Ubuntu ships it by default; Kali requires it explicitly.
- **Snap vs apt** — Ubuntu + Pop ship snap; Mint + Kali block it; Zorin +
  elementary + Neon don't install it by default. Agent checks `prefs` for
  snap preference and installs or skips accordingly.
- **HWE kernel** — LTS-based distros (Mint, Zorin, elementary) ship older
  kernels. Add `linux-generic-hwe-<VER>` for newer hardware support.
- **NetworkManager vs netplan/networkd** — server ISOs use netplan. If the
  target is a server flavor, enable netplan's NetworkManager renderer or
  write netplan YAML instead.
