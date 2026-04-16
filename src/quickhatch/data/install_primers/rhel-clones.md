# Rocky Linux / AlmaLinux card

Family: **fedora** — uses `families/fedora-family.md` as the base procedure.
Both are RHEL-compatible rebuilds: same package set, same install procedure
(Anaconda + kickstart), just different rebuild organizations. Covered by
one primer because the only per-distro difference is the repo URL + OS
name string.

## Per-distro values

| Key | Rocky Linux | AlmaLinux |
|---|---|---|
| Live env | Boots directly into Anaconda installer | Same |
| Releasever | `9`, `10` — reads `/etc/rocky-release` to confirm | `9`, `10` — reads `/etc/almalinux-release` |
| Mirror | `http://download.rockylinux.org/pub/rocky/<ver>/BaseOS/x86_64/os/` | `http://repo.almalinux.org/almalinux/<ver>/BaseOS/x86_64/os/` |
| AppStream | `http://download.rockylinux.org/pub/rocky/<ver>/AppStream/x86_64/os/` | `http://repo.almalinux.org/almalinux/<ver>/AppStream/x86_64/os/` |
| Package group | `@minimal-environment` (server) / `@graphical-server-environment` / `@workstation-product-environment` (desktop, GNOME) |
| Installer | Anaconda + kickstart |
| sudo group | `wheel` |

## Kickstart: swap `%packages` for the right groups

```
%packages
@core
@minimal-environment
# For a desktop: add @workstation-product-environment
openssh-server
sudo
NetworkManager
# For user desktops: add firefox, flatpak, etc.
%end
```

Point kickstart at the distro-specific URL:

Rocky:
```
url --url="http://download.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/"
repo --name="AppStream" --baseurl="http://download.rockylinux.org/pub/rocky/9/AppStream/x86_64/os/"
```

AlmaLinux:
```
url --url="http://repo.almalinux.org/almalinux/9/BaseOS/x86_64/os/"
repo --name="AppStream" --baseurl="http://repo.almalinux.org/almalinux/9/AppStream/x86_64/os/"
```

## When to pick these

Server-leaning by design. Desktop users who want RHEL-compatibility
(running same packages as their work RHEL box, predictable long-term
support) are the target. If the user just wants a point-release desktop,
**Fedora Workstation** is usually a better fit.

## Distro-specific pitfalls

- **No non-free by default** — Rocky/Alma follow the RHEL upstream
  philosophy. For multimedia codecs + NVIDIA:
  ```
  dnf install -y https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm \
    https://mirrors.rpmfusion.org/nonfree/el/rpmfusion-nonfree-release-$(rpm -E %rhel).noarch.rpm
  ```
- **Conservative package versions** — if user needs newer tools, enable
  EPEL (`dnf install epel-release`) + possibly CRB/PowerTools repo.
- **No Workstation default** — you have to request `@workstation-product-
  environment` explicitly in kickstart. The default minimal install has
  no GUI.
- **RHEL certification quirks** — packages whose availability depends on
  Red Hat subscriptions (e.g. some Insights/Satellite tools) are omitted
  from Rocky/Alma. Works fine for normal use.
- **Rocky vs Alma** — functionally interchangeable for most use cases.
  Rocky has CloudLinux-derived tooling; Alma is sponsored by CloudLinux
  itself. User preference is mostly tribal; agent picks whichever the
  user asked for and doesn't editorialize.
- **Secure Boot** — signed shim works for most hardware.
- **SELinux** — enforcing by default (RHEL-standard). Leave it unless
  user explicitly asks otherwise.
