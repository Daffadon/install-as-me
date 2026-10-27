# install-as-me

An opinionated Arch Linux installer driven by [archinstall](https://github.com/archlinux/archinstall):
KDE Plasma (Wayland) + Cinnamon, a macOS Big Sur look via WhiteSur, and a
post-install pass that installs the AUR helper, desktop apps, the shell setup and
the services this machine needs.

**It wipes the disk you select.** Run it from the Arch ISO as root.

## Requirements

- Booted from a current Arch ISO with network access
- `archinstall` 4.5+ on the ISO (invoked as `archinstall --config … --creds …`)
- UEFI or BIOS; the target disk must be unmounted
- Secure Boot must be off (NVIDIA modules are unsigned) unless `ALLOW_SECURE_BOOT=1`

## Usage

```bash
git clone https://github.com/Daffadon/install-as-me
cd install-as-me
python install-as-me.py
```

Prompts, in order:

1. `Hostname [archy]:` — empty answer accepts the default
2. `Username:` — no default, `root` is rejected
3. Target disk (TUI picker) when `DISK` is not set
4. GPU driver question when an NVIDIA card is detected
5. `Type YES to continue`
6. User password, optional root password, LUKS passphrase (when `LUKS=1`)

Set `CONFIRM=1` to skip the `YES` prompt for unattended runs.

## Configuration

All settings are environment variables; nothing is edited in the script.

| Variable | Default | Meaning |
|---|---|---|
| `DISK` | picker | Block device to erase, e.g. `/dev/nvme0n1` |
| `HOSTNAME` | prompt, `archy` | Hostname; exported value becomes the prompt default |
| `USERNAME` | prompt | Account to create (wheel, sudo) |
| `TIMEZONE` | `Asia/Jakarta` | Validated against `/usr/share/zoneinfo` |
| `KB_LAYOUT` | `us` | Validated against `localectl list-keymaps` |
| `SYS_LANG` / `SYS_ENC` | `en_US.UTF-8` / `UTF-8` | Locale |
| `KERNELS` | `linux-zen` | Space separated kernel packages, e.g. `linux linux-zen` |
| `SNAPPER` | `0` | `1` enables btrfs snapshots via Snapper |
| `LUKS` | `0` | `1` encrypts the root partition |
| `NVIDIA_OPEN` | `auto` | `1`/`0` to skip the Turing+ question |
| `ALLOW_SECURE_BOOT` | `0` | `1` proceeds with Secure Boot enabled |
| `CONFIRM` | `0` | `1` skips the destructive confirmation |
| `SESSION` | `plasma` | Wayland session SDDM preselects |
| `DUAL_BOOT` | `0` | `1` uses GRUB + os-prober instead of systemd-boot + UKI |
| `INSTALL_YAY` | `1` | `0` skips the AUR helper (AUR packages then build with makepkg) |
| `INSTALL_THEME` | `1` | `0` skips WhiteSur |
| `THEME_VARIANT` | `dark` | `dark` or `light` |
| `EXTRA_PACKAGES` | empty | Repo packages, space separated |
| `EXTRA_AUR` | empty | AUR packages, space separated |

Example:

```bash
DISK=/dev/nvme0n1 USERNAME=daffa DUAL_BOOT=1 THEME_VARIANT=light \
  EXTRA_PACKAGES="btop fastfetch" python install-as-me.py
```

## What gets installed

**Layout:** 1 GiB FAT32 `/boot`, remaining space Btrfs with `@`, `@home`,
`@log` (`/var/log`) and `@pkg` (`/var/cache/pacman/pkg`), `compress=zstd,noatime`,
zstd swap. Bootloader: systemd-boot with UKIs on UEFI, GRUB on BIOS.

**Desktop:** KDE Plasma (Wayland) + Cinnamon, SDDM greeter, PipeWire, Bluetooth,
ufw.

| Group | Packages |
|---|---|
| Desktop apps | `vlc`, `libreoffice-fresh`, `obsidian`, `zed`, `okular`, `thunderbird`, `flameshot`, `copyq`, `onlyoffice-bin` (AUR), `zen-browser-bin` (AUR) |
| Terminal | `ghostty`, `neovim`, `tmux`, `fzf`, `ripgrep`, `fd`, `jq`, `eza`, `bat`, `zoxide`, `bind` (`dig`, `host`, `nslookup`) |
| Integration | `gst-plugins-good`/`bad`/`ugly`, `gst-libav`, `7zip`, `unrar`, `gvfs-mtp`, `gvfs-gphoto2`, `noto-fonts-cjk`, `lm_sensors`, `wl-clipboard` |
| Toolchains | `go`, `uv` (Python), `bun` (JS/TS), `nvm` with Node LTS, `dbeaver` (SQL client) |
| Shell | `zsh` + oh-my-zsh (git clone), powerlevel10k (AUR), zsh completions/autosuggestions/syntax-highlighting/history-substring-search, MesloLGS NF + JetBrains Mono Nerd fonts |
| Containers / VMs | `docker`, `qemu-full`, `virt-manager` (`libvirtd` enabled, user in the `libvirt` group, NAT network autostart) |
| Remote / network | `openssh`, `tailscale`, `cloudflare-warp-bin` (AUR) |
| Theme | `whitesur-gtk-theme`, `whitesur-icon-theme`, `whitesur-cursor-theme-git`, `whitesur-kde-theme`, `kvantum`, `milou` (search applet used by the WhiteSur panel layout) |

**Services enabled:** `sshd`, `docker`, `libvirtd`, `tailscaled`, plus `bluetooth` via
archinstall's Bluetooth option; `ufw allow 22/tcp`.

**Baseline:** `git`, `sudo`, `openssh`, `mesa`, `mesa-utils`. GPU extras follow
the detected hardware: `nvidia-settings` + `nvidia-prime` with the open kernel
modules, plus `vulkan-intel`/`intel-media-driver`/`libva-intel-driver` or
`vulkan-radeon`/`xf86-video-amdgpu` for a hybrid iGPU.

**Kernel:** `linux-zen` by default. archinstall builds NVIDIA modules as
`nvidia-open-dkms` plus `<kernel>-headers` whenever the selected kernel name
contains a dash, so a non-mainline kernel still gets working modules.

## Post-install pass

Everything below runs as one guarded script inside the target chroot. archinstall
runs custom commands **before** `genfstab`, so the script always exits 0 and
reports problems as `WARNING:` lines instead of aborting a finished install.

archinstall pipes custom-command output and never prints it, so the script writes
progress straight to `/dev/tty`: a banner, nine numbered phases and the live
output of every long-running command (pacman, git, makepkg, yay, grub-mkconfig).
The same lines go to `/var/log/postinstall-aur.log`, which also holds output when
no terminal is attached.

- **yay** — `yay-bin` built as the user (makepkg refuses root), installed with
  `pacman -U`. If that fails, AUR packages are built directly with
  `makepkg -si` through a temporary `NOPASSWD` sudoers drop-in that is removed
  afterwards.
- **WhiteSur** — GTK/Kvantum/Plasma look-and-feel, icons, cursors, SDDM greeter,
  Cinnamon settings via a dconf system database (`/etc/dconf/db/local.d`),
  `/etc/skel` mirror, and a one-shot autostart hook that applies the
  look-and-feel layout + wallpaper on the first Plasma login.
- **Terminal** — Ctrl+Alt+T bound to Ghostty in `kglobalshortcutsrc`,
  `TerminalApplication=ghostty` in `kdeglobals`.
- **Shell** — oh-my-zsh, `.zshrc`, `~/.p10k.zsh` (lean preset), zsh as login
  shell; files are only written when absent.
- **Groups** — the user joins `docker` and `libvirt`; the libvirt NAT network is
  marked autostart.
- **Dual boot** — with GRUB installed: `GRUB_DISABLE_OS_PROBER=false`, timeout
  raised to 5 s when another OS is found, `grub-mkconfig` regenerated.

## Dual boot

`DUAL_BOOT=1` switches to GRUB and disables UKIs, because GRUB is the only
supported bootloader that can list another installed OS:

- Arch's GRUB menu will contain the other OS after `os-prober` runs.
- The firmware boot order decides which bootloader you see first. Select Arch's
  entry once (firmware menu or `efibootmgr -o`) to get Arch's GRUB.
- To make an *existing* other distro's GRUB list Arch instead, boot that distro
  and run `sudo os-prober && sudo grub-mkconfig -o /boot/grub/grub.cfg`.

## Troubleshooting

Logs written inside the installed system:

| File | Contents |
|---|---|
| `/var/log/postinstall-aur.log` | every yay/makepkg/grub/dconf action, including failures |
| `/var/log/pacman.log` | package transactions |

Common `WARNING:` lines and what they mean:

- `yay is unavailable` — AUR helper bootstrap failed; AUR packages fall back to
  `makepkg` (see the log for the real error).
- `only N MiB free in /home` — AUR builds are skipped below 2 GiB free.
- `group docker does not exist` — the `docker` package did not install.
- `GTK theme WhiteSur-Dark is missing` — only part of the theme built; Plasma is
  still themed.

Known caveats:

- The WhiteSur SDDM theme is written for SDDM ≤ 6.7 and instantiates
  `VirtualKeyboardLoader`, which Plasma/SDDM 6.8 dropped. The installer drops a
  compatibility shim next to `Main.qml`; the greeter's on-screen keyboard toggle
  is inert as a result.
- The WhiteSur panel layout asks for `org.kde.plasma.splitdigitalclock`, which no
  repository packages; the layout is rewritten to `org.kde.plasma.digitalclock`.
- `steam` is not installed: it needs the `multilib` repository enabled.
- Cloudflare WARP is installed and `warp-svc` is enabled, but the client still
  needs one manual registration: `warp-cli registration new`.

## Verified / not verified

Checked on real installs: package names against the Arch and AUR APIs, yay and
AUR builds, the WhiteSur theme and SDDM greeter (`sddm-greeter-qt6 --test-mode`
under Xvfb), nvm, service enablement, group membership.

Not exercised: LUKS, Snapper, BIOS/GRUB installs and the dual-boot menu on real
hardware, NVIDIA driver paths.
