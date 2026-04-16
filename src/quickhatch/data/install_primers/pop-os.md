# Pop!_OS card

Family: **debian** — uses `families/debian-family.md` as the base.
Derived from Ubuntu LTS with System76's own installer (`distinst`) and
repos. NVIDIA and AMD ISOs ship different drivers pre-bundled.

## Per-distro values

| Key | Value |
|---|---|
| Live user | `pop` |
| Release codename | Matches Ubuntu LTS: `noble` (24.04) / `jammy` (22.04) |
| Mirror (Ubuntu base) | `http://archive.ubuntu.com/ubuntu` |
| Extra repo | `http://apt.pop-os.org/ubuntu` (Pop's own) + `http://apt.pop-os.org/proprietary` (NVIDIA etc.) |
| Extra key | `curl https://apt.pop-os.org/proprietary.key \| apt-key add -` |
| Desktop metapackage | `pop-desktop` (generic), `pop-desktop-nvidia` (NVIDIA), `pop-desktop-cosmic` (COSMIC DE in newer Pop) |
| Installer | `distinst` (Pop's own CLI installer) |
| Bootloader | **systemd-boot** (not GRUB) by default |
| sudo group | `sudo` |

## Installer-specific path (distinst)

`distinst` is Pop's CLI installer, easier to script than Calamares:

```bash
sudo distinst \
  --block /dev/<DISK_SHORT> \
  --hostname <HOSTNAME> \
  --keyboard us \
  --lang en_US.UTF-8 \
  --username <USER> \
  --realname "<REAL_NAME>" \
  --password "<PASSWORD>" \
  /cdrom/casper/filesystem.squashfs
```

After distinst, chroot to add the SSH key:

```bash
sudo mount /dev/<DISK_SHORT>3 /mnt
sudo mount /dev/<DISK_SHORT>1 /mnt/boot/efi
for f in dev dev/pts proc sys run; do sudo mount --bind /$f /mnt/$f; done

sudo chroot /mnt /bin/bash <<'EOF'
apt install -y openssh-server network-manager
systemctl enable ssh NetworkManager
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys
usermod -aG sudo <USER>
EOF

sudo umount -R /mnt
sudo reboot
```

## If using Path B (debootstrap) from the family primer

Add the Pop repo inside the chroot before installing `pop-desktop`:

```bash
curl https://apt.pop-os.org/proprietary.key | apt-key add -
echo 'deb http://apt.pop-os.org/proprietary noble main' \
  > /etc/apt/sources.list.d/pop-os-apps.list
apt update
apt install -y pop-desktop     # or pop-desktop-nvidia
```

## Distro-specific pitfalls

- **systemd-boot, not GRUB** — distinst installs systemd-boot by default.
  If chrooting manually, use `bootctl install` instead of `grub-install`.
- **NVIDIA vs AMD ISO** — Pop ships two ISOs; pick the one matching the
  user's GPU. The live env already has the matching driver loaded.
- **COSMIC DE coming** — newer Pop ships Rust-based COSMIC DE. Pick
  `pop-desktop-cosmic` on recent releases.
- **Hybrid graphics tool** — "Graphics Switching" toggles Intel/NVIDIA.
  Don't preemptively disable NVIDIA; let user decide post-install.
