# ARM64 VM Certification Pass

Date: 2026-06-02

Branch/commit before the run: `main` at `cecb594`

Profile: `.quickhatch/certification-real/profile-full-disk.json`

Backend: VirtualBox

Target disk inside each VM: `/dev/sda`

Post-reboot verification: enabled

## ISO Inputs

- Debian: `/Users/jan/Documents/distros/debian-live-13.3.0-arm64-libre-slim-autoboot.iso`
- Ubuntu: `/Users/jan/Documents/distros/ubuntu-26.04-desktop-arm64.iso`
- Fedora: `/Users/jan/Documents/distros/Fedora-Workstation-Live-44-1.7.aarch64.iso`

## Results

| Family | Status | Artifact |
| --- | --- | --- |
| Debian | passed | `.quickhatch/certification-real/live/debian/20260602-222920-293-7774a3/summary.json` |
| Fedora | passed | `.quickhatch/certification-real/live/fedora/20260602-222920-306-68f8d0/summary.json` |
| Ubuntu | passed after patch | `.quickhatch/certification-real/live-rerun/ubuntu/20260602-223735-642-5dee42/summary.json` |

Each final passing run reached:

- `script-exit:0`
- `post-reboot-ssh-up`
- `post-reboot-audit:1:0`
- `post-reboot-audit:2:0`
- `post-reboot-audit:3:0`
- `vm-torn-down`

## Bug Found

The first Ubuntu ARM64 run failed during the live install at command 10 because
the Ubuntu adapter emitted x86_64-specific install-spine pieces:

- `grub-efi-amd64`
- `debootstrap --arch=amd64`
- `http://archive.ubuntu.com/ubuntu`

On the ARM64 Ubuntu ISO, this produced:

```text
E: Package 'grub-efi-amd64' has no installation candidate
```

The VM verifier initially summarized the failure as an SSH known-hosts warning
because the SSH client warning was the only stderr while the real package error
was in stdout.

## Patch Applied

- Ubuntu install spine now selects `grub-efi-arm64`, `debootstrap --arch=arm64`,
  and `http://ports.ubuntu.com/ubuntu-ports` when `target_arch` is ARM64.
- VM verifier failure detail now strips harmless SSH known-hosts warnings before
  choosing the error text, so stdout package failures are surfaced accurately.

## Notes

Debian and Ubuntu both report GRUB warnings about EFI variables being unavailable
inside the VM, then complete the removable fallback install successfully. The
post-reboot SSH and audit commands passed.
