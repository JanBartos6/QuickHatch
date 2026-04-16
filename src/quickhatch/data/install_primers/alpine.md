# Alpine Linux install primer

Tiny (~130 MB), musl libc + BusyBox. Rolling or point-release. No systemd
(OpenRC). Used heavily in containers + servers; also usable as a minimal
desktop for advanced users.

## Live environment

- Boots to root shell, no password. Keyboard: US default.
- Tools: `setup-alpine` wizard, `apk`, `apk-tools`, `parted`, `busybox`.

## Approach — setup-alpine with answerfile (unattended)

Alpine's `setup-alpine` supports an **answerfile** for unattended install:

```bash
cat > /tmp/answers <<'ANS'
KEYMAPOPTS="us us"
HOSTNAMEOPTS="<HOSTNAME>"
DEVDOPTS=mdev

# Network: DHCP on eth0
INTERFACESOPTS="auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
"

DNSOPTS="-n 1.1.1.1"
TIMEZONEOPTS="<ZONE>"
PROXYOPTS=none
APKREPOSOPTS="-1"     # Use the first (community-preferred) mirror
SSHDOPTS=openssh
NTPOPTS=chrony
DISKOPTS="-m sys <DISK>"    # Full-disk sys install (not diskless or data)
LBUOPTS=none
APKCACHEOPTS=none
ANS

setup-alpine -f /tmp/answers

# setup-alpine handles: disk partition, rootfs install, bootloader, reboot
```

## Post-install SSH key handoff

`setup-alpine` doesn't do SSH keys. Before `setup-alpine` reboots:

```bash
# Chroot into /mnt (setup-alpine uses /mnt as target)
chroot /mnt /bin/sh <<'EOF'
# Create user (setup-alpine only creates root by default)
adduser -D -G wheel <USER>
echo '<USER>:<PASSWORD>' | chpasswd
echo 'root:<ROOT_PASSWORD>' | chpasswd

# Configure doas (OpenBSD-style sudo equivalent)
apk add doas
echo 'permit persist :wheel' > /etc/doas.d/doas.conf

# SSH key
mkdir -p /home/<USER>/.ssh
echo '<PUBKEY>' > /home/<USER>/.ssh/authorized_keys
chown -R <USER>:<USER> /home/<USER>/.ssh
chmod 700 /home/<USER>/.ssh
chmod 600 /home/<USER>/.ssh/authorized_keys

# Enable sshd at boot
rc-update add sshd default
EOF
```

Then `reboot`.

## Alternative — manual install (more control)

```bash
DISK=/dev/sda
parted -s "$DISK" mklabel gpt \
  mkpart ESP fat32 1MiB 513MiB set 1 esp on \
  mkpart primary ext4 513MiB 100%
mkfs.fat -F32 "${DISK}1"
mkfs.ext4 -F "${DISK}2"
mount "${DISK}2" /mnt
mkdir /mnt/boot
mount "${DISK}1" /mnt/boot

setup-disk -m sys "$DISK"
# After setup-disk:
chroot /mnt /bin/sh
# Install grub-efi, enable services, configure user as above
apk add grub-efi efibootmgr
grub-install --target=x86_64-efi --efi-directory=/boot --boot-directory=/boot
grub-mkconfig -o /boot/grub/grub.cfg
```

## Differences that matter for the agent

- **No systemd** — `rc-update add <service> default` enables a service;
  `rc-service <service> start` starts it. The agent must avoid `systemctl`
  commands after the distro check.
- **No glibc** — musl. Some precompiled binaries won't run (esp. anything
  linked statically for glibc). Flatpak works; most AUR/precompiled stuff
  doesn't.
- **`apk add <pkg>`** is the package manager. `apk update` / `apk upgrade`
  for maintenance.
- **`doas` not `sudo`** — Alpine prefers doas by convention. Both work.
- **Desktop stack** — minimal. `apk add plasma` or `apk add gnome` works
  but pulls a smaller set than Arch/Fedora equivalents. Test.

## Common pitfalls

- **Missing glibc apps** — flatpak is your escape hatch.
- **Language runtimes** — musl affects Python wheels, Node binaries, etc.
  Use `apk add py3-<pkg>` when possible, or `pip install --no-binary :all:`.
- **Firmware** — `linux-firmware` is a separate apk. Missing = no Wi-Fi.
- **Browser** — `firefox` is available from the Alpine community repo; enable it.
