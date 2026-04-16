# openSUSE Tumbleweed install primer

Rolling release with top-tier QA (openQA integration tests every snapshot).
Uses YaST2 for system management. Supports AutoYaST for unattended install.

## Live environment

- DVD/Offline installer: boots to YaST.
- Tumbleweed boots to graphical installer by default; CLI available via TTY.

## Network

- Ethernet auto via wicked or NetworkManager.
- WiFi: `nmcli device wifi connect <SSID> password <PSK>` on live env.

## Approach — AutoYaST (unattended)

Generate an AutoYaST profile XML, feed to installer.

```xml
<?xml version="1.0"?>
<!DOCTYPE profile>
<profile xmlns="http://www.suse.com/1.0/yast2ns"
         xmlns:config="http://www.suse.com/1.0/configns">

  <suse_register>
    <do_registration config:type="boolean">false</do_registration>
  </suse_register>

  <keyboard>
    <keymap>english-us</keymap>
  </keyboard>
  <language>
    <language>en_US</language>
  </language>
  <timezone>
    <timezone><ZONE></timezone>
    <hwclock>UTC</hwclock>
  </timezone>

  <networking>
    <hostname>
      <hostname><HOSTNAME></hostname>
      <domain>localdomain</domain>
    </hostname>
    <dns>
      <hostname><HOSTNAME></hostname>
    </dns>
  </networking>

  <partitioning config:type="list">
    <drive>
      <device>/dev/<DISK_SHORT></device>
      <initialize config:type="boolean">true</initialize>
      <use>all</use>
    </drive>
  </partitioning>

  <software>
    <patterns config:type="list">
      <pattern>base</pattern>
      <pattern>enhanced_base</pattern>
      <pattern>gnome</pattern>
      <pattern>x11</pattern>
    </patterns>
    <packages config:type="list">
      <package>openssh</package>
      <package>sudo</package>
      <package>NetworkManager</package>
    </packages>
  </software>

  <users config:type="list">
    <user>
      <username>root</username>
      <user_password><ROOT_PASSWORD></user_password>
      <encrypted config:type="boolean">false</encrypted>
    </user>
    <user>
      <username><USER></username>
      <fullname><REAL_NAME></fullname>
      <user_password><PASSWORD></user_password>
      <encrypted config:type="boolean">false</encrypted>
      <authorized_keys config:type="list">
        <listentry><PUBKEY></listentry>
      </authorized_keys>
    </user>
  </users>

  <services-manager>
    <services>
      <enable config:type="list">
        <service>sshd</service>
        <service>NetworkManager</service>
      </enable>
    </services>
  </services-manager>

  <scripts>
    <post-scripts config:type="list">
      <script>
        <filename>wheel.sh</filename>
        <interpreter>shell</interpreter>
        <source><![CDATA[
usermod -aG wheel <USER>
sed -i 's/^# \(%wheel ALL=(ALL:ALL) ALL\)/\1/' /etc/sudoers
        ]]></source>
      </script>
    </post-scripts>
  </scripts>
</profile>
```

Boot the installer with `autoyast=file:///tmp/autoinst.xml`.

## Alternative — zypper install from live

```bash
DISK=/dev/nvme0n1
# Partition (same as other primers)
# ...

zypper --root /mnt addrepo \
  http://download.opensuse.org/tumbleweed/repo/oss/ oss
zypper --root /mnt refresh
zypper --root /mnt install -y --no-recommends \
  patterns-base-base patterns-base-enhanced_base \
  kernel-default systemd openssh sudo NetworkManager \
  grub2 grub2-x86_64-efi

# Chroot, configure (locale, user, ssh key, grub), reboot
```

## Common pitfalls

- **YaST registration nag** — if the user has no SUSE account, the
  `<do_registration>false</do_registration>` line is critical.
- **Patterns vs packages** — openSUSE prefers patterns (metapackages) over
  raw package lists. `patterns-gnome` pulls everything for GNOME.
- **zypper is slower than pacman/apt** — package operations can be noticeably
  slower; don't time-out the agent's commands too aggressively.
- **Snapshots (Snapper + btrfs)** — default. Installer creates /home on xfs,
  / on btrfs. Good for rollbacks but the agent should know the layout.
