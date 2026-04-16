# Pop!_OS install primer

System76's Ubuntu LTS derivative. GNOME (Pop Shell tiling extensions) or
COSMIC DE. Gamer/dev focus. Dual NVIDIA + AMD ISOs so the right driver ships
with the install.

## Live environment

- Boots to GNOME with Pop Shell as `pop` user.
- Installer: Pop!_OS's own `distinst`-based installer (not Ubiquity). Less
  automation-friendly than stock Ubuntu.

## Approach — distinst from CLI

Pop's installer exposes a CLI. Good for scripting but docs are thin.

```bash
# Wipe disk + install Pop!_OS to it
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

After distinst finishes, chroot into the target to add the SSH key:

```bash
sudo mount /dev/<DISK_SHORT>3 /mnt   # root partition distinst created
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

## Alternative — debootstrap + Pop repos

If `distinst` is too opaque:

```bash
sudo debootstrap --arch=amd64 noble /mnt http://apt.pop-os.org/ubuntu
sudo chroot /mnt /bin/bash <<'EOF'
# Add Pop's repos
curl https://apt.pop-os.org/proprietary.key | apt-key add -
echo 'deb http://apt.pop-os.org/proprietary noble main' \
  > /etc/apt/sources.list.d/pop-os-apps.list
apt update
apt install -y pop-desktop   # or pop-desktop-nvidia / pop-desktop-raspi

# ... rest matches Ubuntu primer (locale, user, grub, ssh key, etc.)
EOF
```

## NVIDIA vs AMD ISO

Pop ships two ISOs: one with NVIDIA proprietary drivers pre-bundled, one
for AMD/Intel. The live env the agent boots into is already the right
flavor — the wizard should tell the user which ISO to download based on
their scanned GPU.

## Common pitfalls

- **`pop-desktop-nvidia` vs `pop-desktop`** — pick based on GPU. Agent reads
  `lspci | grep -i vga` in the live env to confirm.
- **systemd-boot vs GRUB** — Pop!_OS uses systemd-boot (gummiboot) by
  default, not GRUB. `distinst` handles this. If chrooting manually,
  `bootctl install` instead of `grub-install`.
- **COSMIC vs GNOME** — newer Pop!_OS ships COSMIC DE. Package name is
  `pop-desktop-cosmic`. Check Pop release notes for which version the user's
  ISO has.
- **Hybrid graphics (laptops)** — Pop's "Graphics Switching" tool toggles
  Intel/NVIDIA. Agent shouldn't preemptively kill NVIDIA; let the user
  decide via GUI post-install.
