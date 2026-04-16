# Ubuntu LTS install primer

Canonical's LTS release, 5-year support, apt + snap ecosystem. GNOME default.
Live ISO ships Subiquity (server) or Ubiquity (desktop); both support
autoinstall (cloud-init).

## Live environment

- Desktop ISO: boots to GNOME as `ubuntu` user.
- Server ISO: boots to `installer@ubuntu-server` prompt.
- Open terminal; `sudo` needs no password on live env.

## Network

- Ethernet: auto via NetworkManager (desktop) or netplan (server).
- WiFi: `nmcli device wifi connect <SSID> password <PSK>` on desktop. Server
  ISO has no WiFi support — use ethernet.

## Approach — autoinstall (cloud-init)

Ubuntu supports unattended install via cloud-init. Write a `user-data` file,
point the installer at it, it runs without user interaction.

```yaml
# /tmp/user-data
#cloud-config
autoinstall:
  version: 1
  locale: en_US.UTF-8
  keyboard:
    layout: us
  identity:
    realname: "<REAL_NAME>"
    username: <USER>
    password: "<HASHED_PASSWORD>"   # mkpasswd -m sha-512 '<PASSWORD>'
    hostname: <HOSTNAME>
  ssh:
    install-server: true
    allow-pw: false
    authorized-keys:
      - "<PUBKEY>"
  packages:
    - sudo
    - network-manager
    - curl
  storage:
    layout:
      name: direct   # wipes target disk, single root partition
  user-data:
    disable_root: false
  late-commands:
    - curtin in-target --target=/target -- systemctl enable ssh NetworkManager
    - curtin in-target --target=/target -- usermod -aG sudo <USER>
  shutdown: reboot
```

```bash
# Pass it to the installer
sudo subiquity --autoinstall-url=file:///tmp/user-data
# or on desktop ISO:
sudo ubiquity --automation-file=/tmp/user-data
```

## Alternative — debootstrap from live

If the installer misbehaves, install directly via debootstrap (works on any
Debian-family live env):

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

sudo debootstrap --arch=amd64 noble /mnt http://archive.ubuntu.com/ubuntu
# noble = Ubuntu 24.04. Use jammy for 22.04.

for f in dev dev/pts proc sys run; do sudo mount --bind /$f /mnt/$f; done
```

Then chroot and:

```bash
sudo chroot /mnt /bin/bash <<'EOF'
apt update
apt install -y linux-generic systemd-sysv sudo grub-efi-amd64 efibootmgr \
  openssh-server network-manager locales

# Locale
locale-gen en_US.UTF-8
echo 'LANG=en_US.UTF-8' > /etc/default/locale

# Hostname
echo <HOSTNAME> > /etc/hostname

# Timezone
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime

# User
useradd -m -G sudo -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd

# SSH key
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Services
systemctl enable ssh NetworkManager

# fstab
echo "UUID=$(blkid -s UUID -o value ${DISK}p2) / ext4 defaults 0 1" >> /etc/fstab
echo "UUID=$(blkid -s UUID -o value ${DISK}p1) /boot vfat defaults 0 2" >> /etc/fstab

# Bootloader
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=ubuntu
update-grub
EOF

sudo umount -R /mnt
sudo reboot
```

## Common pitfalls

- **Snap vs apt confusion** — desktop ISO installs Firefox/Chromium/etc. as
  snaps. Agent should note this if user wants pure apt.
- **Ubuntu Pro prompt** — some releases nag about Pro. Harmless but noisy;
  agent can disable via `pro config set apt_news=false`.
- **GRUB to EFI mismatch** — on BIOS-only systems, use `grub-pc` instead of
  `grub-efi-amd64`, install to the raw device not a partition.
- **Kernel name** — `linux-generic` works everywhere; `linux-hwe-*` is for
  newer hardware on LTS releases.
