# Void Linux install primer

Independent rolling distro. Uses runit (not systemd). XBPS package manager.
Available in glibc and musl variants. Minimalist, pragmatic.

## Live environment

- Boots to root shell (or XFCE live variant).
- Tools: `void-installer`, `xbps-install`, `parted`, `cfdisk`.

## Approach — void-installer (TUI)

Void ships an installer that's ncurses-based and not autoinstall-friendly.
For unattended install, use **chroot + xbps** directly:

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

# Copy /var/db/xbps keys into target
mkdir -p /mnt/var/db/xbps/keys
cp /var/db/xbps/keys/*.plist /mnt/var/db/xbps/keys/

# Install base system
XBPS_ARCH=x86_64 xbps-install -S -y -R \
  https://repo-default.voidlinux.org/current -r /mnt \
  base-system grub-x86_64-efi efibootmgr openssh chrony

# For musl variant: use -R https://repo-default.voidlinux.org/current/musl and
#                   XBPS_ARCH=x86_64-musl

# Chroot prep
for f in dev proc sys run; do mount --rbind /$f /mnt/$f; mount --make-rslave /mnt/$f; done

chroot /mnt /bin/bash <<'EOF'
# Timezone + locale
ln -sf /usr/share/zoneinfo/<ZONE> /etc/localtime

# For glibc variant:
sed -i 's/^#\(en_US.UTF-8\)/\1/' /etc/default/libc-locales
xbps-reconfigure -f glibc-locales
echo 'LANG=en_US.UTF-8' >> /etc/locale.conf

# Hostname
echo '<HOSTNAME>' > /etc/hostname

# rc.conf (runit)
sed -i 's|^#HOSTNAME=.*|HOSTNAME="<HOSTNAME>"|' /etc/rc.conf
sed -i 's|^#TIMEZONE=.*|TIMEZONE="<ZONE>"|' /etc/rc.conf

# User + wheel group
useradd -m -G wheel,users,audio,video,storage,input,network -s /bin/bash <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd

# sudo (installed via xbps-install sudo) or doas; both work on Void
xbps-install -Sy sudo
sed -i 's/^# \(%wheel ALL=(ALL) ALL\)/\1/' /etc/sudoers

# SSH key
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Enable services via runit (symlink to /var/service)
ln -sf /etc/sv/sshd /var/service/
ln -sf /etc/sv/chronyd /var/service/

# Network (install NetworkManager, or use dhcpcd directly)
xbps-install -Sy NetworkManager
ln -sf /etc/sv/NetworkManager /var/service/

# Bootloader
grub-install --target=x86_64-efi --efi-directory=/boot --bootloader-id=void
grub-mkconfig -o /boot/grub/grub.cfg

# fstab
genfstab -U / >> /etc/fstab 2>/dev/null || \
  printf 'UUID=%s / ext4 defaults 0 1\nUUID=%s /boot vfat defaults 0 2\n' \
    "$(blkid -s UUID -o value ${DISK}p2)" \
    "$(blkid -s UUID -o value ${DISK}p1)" >> /etc/fstab
EOF

umount -R /mnt
reboot
```

## Key Void concepts

- **runit** — service supervisor. Enable service = symlink from
  `/etc/sv/<service>` to `/var/service/`. Disable = remove the symlink.
- **xbps** — package manager. `xbps-install -S` = sync + install.
  `xbps-query -Rs <name>` = search. `xbps-remove` = remove.
- **XBPS_ARCH** — needed when installing to a chroot, tells xbps which
  architecture to target.
- **No systemd** — no `systemctl`, no `journalctl`. Logs are in
  `/var/log/socklog/` (install `socklog-void` for this).

## Common pitfalls

- **Forgetting to symlink services** — no service starts on boot until
  symlinked to `/var/service/`.
- **xbps-src for AUR-like packages** — user packages come from `xbps-src`
  (void-packages repo). Agent shouldn't build these for the user
  unattended.
- **musl vs glibc** — musl is faster + smaller, but breaks proprietary
  binaries (NVIDIA's, Steam, Zoom). Pick glibc for desktop users.
- **Firmware not auto-installed** — `xbps-install -Sy linux-firmware`
  separately. Wi-Fi breaks without this.
- **Locale missing** — glibc variant needs `/etc/default/libc-locales`
  editing + `xbps-reconfigure glibc-locales` or apps complain about
  missing locale files.
