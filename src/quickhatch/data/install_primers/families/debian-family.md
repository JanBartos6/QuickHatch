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

### Path B: debootstrap (universal, agent-friendly)

This path works on any Debian-family live env. Agent uses this when the
distro's installer is awkward to automate:

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

# Bind-mount system dirs before chroot
for f in dev dev/pts proc sys run; do sudo mount --bind /$f /mnt/$f; done
```

Now chroot and configure. The base packages needed are the same everywhere:

```bash
sudo chroot /mnt /bin/bash <<'EOF'
# --- 1. Add distro-specific repo/PPA (from card) if needed ---
# Example (Mint):
#   echo 'deb http://packages.linuxmint.com wilma main upstream import backport' \
#     > /etc/apt/sources.list.d/mint.list
#   apt-key adv --keyserver keyserver.ubuntu.com --recv A6616109451BBBF2
# Example (Zorin):
#   add-apt-repository -y ppa:zorinos/stable
# Example (Kali, non-free-firmware):
#   sed -i 's/main$/main contrib non-free non-free-firmware/' /etc/apt/sources.list

apt update

# --- 2. Core system packages ---
apt install -y \
  linux-generic systemd-sysv sudo \
  grub-efi-amd64 efibootmgr \
  openssh-server network-manager \
  locales curl

# For BIOS machines swap grub-efi-amd64 + efibootmgr for:
#   apt install -y grub-pc

# --- 3. Distro metapackage (from card) ---
# Example substitutions:
#   Ubuntu:      apt install -y ubuntu-desktop
#   Mint:        apt install -y mint-meta-cinnamon
#   Pop!_OS:     apt install -y pop-desktop
#   Zorin:       apt install -y zorin-os-desktop
#   elementary:  apt install -y elementary-desktop pantheon-session
#   KDE Neon:    apt install -y neon-desktop
#   Kali:        apt install -y kali-linux-default
#   Debian min:  apt install -y task-desktop
apt install -y <METAPACKAGE>

# --- 4. Locale ---
locale-gen en_US.UTF-8
echo 'LANG=en_US.UTF-8' > /etc/default/locale

# --- 5. Hostname ---
echo '<HOSTNAME>' > /etc/hostname
cat >> /etc/hosts <<HOSTS
127.0.0.1 localhost
127.0.1.1 <HOSTNAME>.localdomain <HOSTNAME>
HOSTS

# --- 6. Timezone ---
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime

# --- 7. User + sudo ---
useradd -m -G sudo -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd

# --- 8. SSH key for reconnect after reboot ---
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# --- 9. Enable services ---
systemctl enable ssh NetworkManager

# --- 10. fstab ---
ROOT_UUID=$(blkid -s UUID -o value ${DISK}p2)
BOOT_UUID=$(blkid -s UUID -o value ${DISK}p1)
echo "UUID=$ROOT_UUID / ext4 defaults 0 1" >> /etc/fstab
echo "UUID=$BOOT_UUID /boot vfat defaults 0 2" >> /etc/fstab

# --- 11. Bootloader ---
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=ubuntu
update-grub
EOF

sudo umount -R /mnt
sudo reboot
```

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
