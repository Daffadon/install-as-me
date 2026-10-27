#!/usr/bin/env python3
"""Archinstall-driven KDE Plasma Wayland + Cinnamon X11 installer.

Run from the Arch ISO as root. This WIPES the selected disk.

The hostname is prompted for unless HOSTNAME is exported. Post-install extras
(yay, the WhiteSur "Big Sur" theme for Plasma/GTK/Cinnamon/SDDM) are applied in
the chroot and never abort the installation.
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

DISK = Path(os.environ["DISK"]) if os.environ.get("DISK") else None
HOSTNAME = os.environ.get("HOSTNAME") or None  # None -> prompt
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Jakarta")
USERNAME = os.environ.get("USERNAME", "daffaputra")
KB_LAYOUT = os.environ.get("KB_LAYOUT", "us")
SYS_LANG = os.environ.get("SYS_LANG", "en_US.UTF-8")
SYS_ENC = os.environ.get("SYS_ENC", "UTF-8")
KERNELS = os.environ.get("KERNELS", "linux").split()
SNAPPER = os.environ.get("SNAPPER", "0") == "1"
LUKS = os.environ.get("LUKS", "0") == "1"
NVIDIA_OPEN = os.environ.get("NVIDIA_OPEN", "auto")
INSTALL_YAY = os.environ.get("INSTALL_YAY", "1") == "1"
INSTALL_THEME = os.environ.get("INSTALL_THEME", "1") == "1"
THEME_VARIANT = os.environ.get("THEME_VARIANT", "dark").strip().lower()
# Space separated additions; EXTRA_PACKAGES goes through pacman, EXTRA_AUR
# through yay (AUR only, or repo names as a fallback).
EXTRA_PACKAGES = os.environ.get("EXTRA_PACKAGES", "").split()
EXTRA_AUR = os.environ.get("EXTRA_AUR", "").split()
# Zen Browser ships as a binary AUR package; everything else below is in extra.
AUR_PACKAGES = ["zen-browser-bin", "onlyoffice-bin", *EXTRA_AUR]
WORKDIR = Path("/root/archinstall-daffaputra")

# WhiteSur is the most complete macOS Big Sur port for Linux: it ships a GTK
# theme, icons, cursors, a Plasma look-and-feel/desktop theme/aurorae/Kvantum
# theme, wallpapers and (unlike most porting projects) an SDDM greeter theme.
# Names below are the identifiers those packages actually install.
THEMES = {
    "dark": {
        "gtk": "WhiteSur-Dark",
        "icons": "WhiteSur-dark",
        "lnf": "com.github.vinceliuice.WhiteSur-dark",
        "plasma": "WhiteSur-dark",
        "colors": "WhiteSurDark",
        "aurorae": "WhiteSur-dark",
        "widget_style": "kvantum-dark",
        "sddm": "WhiteSur-dark",
        "wallpaper": "WhiteSur-dark",
        "prefer_dark": "1",
    },
    "light": {
        "gtk": "WhiteSur-Light",
        "icons": "WhiteSur-light",
        "lnf": "com.github.vinceliuice.WhiteSur",
        "plasma": "WhiteSur",
        "colors": "WhiteSur",
        "aurorae": "WhiteSur",
        "widget_style": "kvantum",
        "sddm": "WhiteSur-light",
        "wallpaper": "WhiteSur-light",
        "prefer_dark": "0",
    },
}


def run(*cmd):
    return subprocess.check_output(cmd, text=True).strip()


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def secure_boot_enabled():
    efivars = Path("/sys/firmware/efi/efivars")
    if not efivars.exists():
        return False
    for p in efivars.glob("SecureBoot-*"):
        try:
            data = p.read_bytes()
            return len(data) > 4 and data[4] == 1
        except OSError:
            return False
    return False


def pick_disk():
    from archinstall.lib.disk.disk_menu import select_devices
    from archinstall.tui.components import tui

    selected = tui.run(lambda: select_devices())
    if not selected:
        die("no disk selected")
    if len(selected) != 1:
        die("select exactly one disk")
    return Path(selected[0].device_info.path)


def validate_packages(packages):
    """Fail before the disk is touched if a package is unknown to the repos.

    archinstall only reports "target not found" after partitioning and wiping,
    which is far too late; the ISO ships a pacman with the same repos.
    """
    try:
        run("pacman", "-Sy")
    except subprocess.CalledProcessError as err:
        print(f"WARNING: pacman -Sy failed ({err}); skipping package validation")
        return
    missing = [
        name
        for name in packages
        if subprocess.run(
            ["pacman", "-Si", name], capture_output=True, text=True
        ).returncode
        != 0
    ]
    if missing:
        die(
            "unknown package(s) for this ISO's repositories: "
            + ", ".join(missing)
            + " (check with: pacman -Si <name>)"
        )


def ask_hostname():
    default = HOSTNAME or ""
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"Hostname{suffix}: ").strip()
        except EOFError:
            die("no hostname given")
        value = answer or default
        if not value:
            print("Hostname cannot be empty.")
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
            print(f"Invalid hostname: {value!r} (lowercase, digits, '-')")
            continue
        return value


EXTRAS_SCRIPT = r"""# Post-install extras, executed inside the target chroot as root.
# archinstall runs custom commands *before* genfstab, so this script must never
# exit non-zero: every optional step is reported and skipped on failure.
set -u

user='@@USER@@'
home='/home/@@USER@@'

warn() { printf 'WARNING: %s\n' "$*" >&2; }

# sshd is enabled by default, but archinstall's ufw setup denies inbound traffic.
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null 2>&1 || warn 'could not open the SSH port in ufw'
fi

install_yay() {
  command -v yay >/dev/null 2>&1 && return 0

  # makepkg refuses to run as root: build as the user, install as root.
  local tmp=/tmp/yay-bin
  pacman -S --needed --noconfirm base-devel git || { warn 'base-devel install failed'; return 1; }
  rm -rf "$tmp"
  runuser -u "$user" -- env HOME="$home" git clone --depth 1 https://aur.archlinux.org/yay-bin.git "$tmp" \
    || { warn 'yay-bin clone failed'; rm -rf "$tmp"; return 1; }
  runuser -u "$user" -- env HOME="$home" bash -c "cd $tmp && makepkg -f --noconfirm" \
    || { warn 'yay-bin build failed'; rm -rf "$tmp"; return 1; }
  pacman -U --noconfirm "$tmp"/yay-bin-*.pkg.tar.zst \
    || { warn 'yay-bin install failed'; rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
}

install_aur() {
  # yay installs through sudo, and the chroot cannot answer a password prompt.
  local dropin=/etc/sudoers.d/99-postinstall-nopasswd
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > "$dropin" \
    || { warn 'cannot write sudoers drop-in'; return 1; }
  chmod 440 "$dropin"
  runuser -u "$user" -- env HOME="$home" yay -S --noconfirm --needed "$@"
  local rc=$?
  rm -f "$dropin"
  return "$rc"
}

install_node() {
  local init=/usr/share/nvm/init-nvm.sh
  if [ ! -f "$init" ]; then
    warn 'nvm is not installed; skipping the Node.js LTS setup'
    return 0
  fi
  # nvm is a shell function, so it has to be wired into the interactive shell.
  if [ -f "$home/.bashrc" ] && ! grep -q 'init-nvm.sh' "$home/.bashrc"; then
    printf '\n# nvm\n[ -s %s ] && source %s\n' "$init" "$init" >> "$home/.bashrc"
  fi
  runuser -u "$user" -- env HOME="$home" bash -lc \
    "source $init && nvm install --lts && nvm alias default node" \
    || warn 'nvm: Node.js LTS install failed (run: nvm install --lts)'
}

configure_groups() {
  # docker/libvirt restrict access to group members, and virt-manager expects
  # the libvirt NAT network to be marked autostart or it reports "no network".
  local group
  for group in docker libvirt; do
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$user" || warn "could not add $user to the $group group"
    else
      warn "group $group does not exist; skipping membership"
    fi
  done
  if [ -f /etc/libvirt/qemu/networks/default.xml ]; then
    install -d /etc/libvirt/qemu/networks/autostart
    ln -sf ../default.xml /etc/libvirt/qemu/networks/autostart/default.xml
  fi
}

dconf_write() {
  runuser -u "$user" -- env HOME="$home" dbus-run-session -- dconf write "$1" "$2" \
    || warn "dconf write $1 failed"
}

apply_theme() {
  local cfg="$home/.config"
  install -d "$cfg/gtk-3.0" "$cfg/gtk-4.0" "$cfg/Kvantum" "$cfg/autostart" "$home/.local/bin"

  cat > "$cfg/kdeglobals" <<'CFG'
[General]
ColorScheme=@@COLORS@@

[Icons]
Theme=@@ICONS@@

[KDE]
LookAndFeelPackage=@@LNF@@
widgetStyle=@@WIDGETSTYLE@@
CFG
  cat > "$cfg/plasmarc" <<'CFG'
[Theme]
name=@@PLASMA@@
CFG
  cat > "$cfg/kwinrc" <<'CFG'
[org.kde.kdecoration2]
ButtonsOnLeft=XAI
ButtonsOnRight=
library=org.kde.kwin.aurorae
theme=__aurorae__svg__@@AURORAE@@
CFG
  cat > "$cfg/kcminputrc" <<'CFG'
[Mouse]
cursorTheme=WhiteSur-cursors
CFG
  cat > "$cfg/Kvantum/kvantum.kvconfig" <<'CFG'
[General]
theme=WhiteSur
CFG

  local gtk
  for gtk in gtk-3.0 gtk-4.0; do
    cat > "$cfg/$gtk/settings.ini" <<'CFG'
[Settings]
gtk-theme-name=@@GTK@@
gtk-icon-theme-name=@@ICONS@@
gtk-cursor-theme-name=WhiteSur-cursors
gtk-cursor-theme-size=24
gtk-font-name=Noto Sans 10
gtk-application-prefer-dark-theme=@@PREFER_DARK@@
CFG
  done

  # Applying the look-and-feel (panels/layout) and the wallpaper both need a
  # running session, so defer them to the first login and drop the autostart
  # entry once they succeed.
  cat > "$home/.local/bin/whitesur-first-login.sh" <<'SH'
#!/bin/sh
# WhiteSur look-and-feel needs a running plasmashell; retry briefly.
for _ in 1 2 3 4 5; do
  plasma-apply-lookandfeel -a '@@LNF@@' >/dev/null 2>&1 && break
  sleep 4
done
wallpaper='/usr/share/wallpapers/@@WALLPAPER@@/contents/images/3840x2160.jpg'
for _ in 1 2 3 4 5; do
  if plasma-apply-wallpaperimage "$wallpaper" >/dev/null 2>&1; then
    rm -f "$HOME/.config/autostart/whitesur-first-login.desktop"
    exit 0
  fi
  sleep 4
done
exit 0
SH
  chmod 755 "$home/.local/bin/whitesur-first-login.sh"

  cat > "$cfg/autostart/whitesur-first-login.desktop" <<'SH'
[Desktop Entry]
Type=Application
Name=WhiteSur first login
Exec=/home/@@USER@@/.local/bin/whitesur-first-login.sh
Terminal=false
NoDisplay=true
OnlyShowIn=KDE;
X-GNOME-Autostart-enabled=true
SH

  install -d /etc/sddm.conf.d
  cat > /etc/sddm.conf.d/10-whitesur-theme.conf <<'CFG'
[Theme]
Current=@@SDDM@@
CFG

  # Cinnamon keeps its appearance in dconf, which needs a session bus.
  if command -v dconf >/dev/null 2>&1 && command -v dbus-run-session >/dev/null 2>&1; then
    dconf_write /org/cinnamon/desktop/interface/gtk-theme "'@@GTK@@'"
    dconf_write /org/cinnamon/desktop/interface/icon-theme "'@@ICONS@@'"
    dconf_write /org/cinnamon/desktop/interface/cursor-theme "'WhiteSur-cursors'"
    dconf_write /org/cinnamon/theme/name "'@@GTK@@'"
    dconf_write /org/cinnamon/desktop/background/picture-uri \
      "'file:///usr/share/wallpapers/@@WALLPAPER@@/contents/images/3840x2160.jpg'"
  else
    warn 'dconf/dbus-run-session missing; Cinnamon keeps the default look'
  fi

  chown -R "$user:$user" "$home/.config" "$home/.local"
  install -d /etc/skel/.config /etc/skel/.local/bin
  cp -a "$home/.config/." /etc/skel/.config/
  cp -a "$home/.local/bin/." /etc/skel/.local/bin/
}

if @@WANT_YAY@@; then
  install_yay || warn 'yay is unavailable; skipping AUR packages'
fi

if @@WANT_APPS@@; then
  if command -v yay >/dev/null 2>&1; then
    install_aur @@AUR_PACKAGES@@ || warn 'some AUR packages failed to install'
  fi
fi

install_node
configure_groups

if @@WANT_THEME@@; then
  if command -v yay >/dev/null 2>&1; then
    [ -d /usr/share/themes/@@GTK@@ ] || install_aur \
      whitesur-gtk-theme whitesur-icon-theme whitesur-cursor-theme-git whitesur-kde-theme \
      || warn 'WhiteSur packages failed to install'
    if [ -d /usr/share/themes/@@GTK@@ ]; then
      apply_theme
    else
      warn 'WhiteSur theme is not installed; desktop keeps its default look'
    fi
  fi
fi

exit 0
"""


def extras_script():
    """Build the chroot script that installs yay, AUR apps and the theme."""
    theme = THEMES[THEME_VARIANT]
    script = EXTRAS_SCRIPT
    for token, value in {
        "USER": USERNAME,
        "GTK": theme["gtk"],
        "ICONS": theme["icons"],
        "LNF": theme["lnf"],
        "PLASMA": theme["plasma"],
        "COLORS": theme["colors"],
        "AURORAE": theme["aurorae"],
        "WIDGETSTYLE": theme["widget_style"],
        "SDDM": theme["sddm"],
        "WALLPAPER": theme["wallpaper"],
        "PREFER_DARK": theme["prefer_dark"],
        "WANT_YAY": "true" if INSTALL_YAY else "false",
        "WANT_THEME": "true" if INSTALL_YAY and INSTALL_THEME else "false",
        "WANT_APPS": "true" if INSTALL_YAY and AUR_PACKAGES else "false",
        "AUR_PACKAGES": " ".join(AUR_PACKAGES),
    }.items():
        script = script.replace(f"@@{token}@@", value)
    if "@@" in script:
        die("internal error: unsubstituted placeholder in extras script")
    return script


def main():
    global DISK, HOSTNAME

    if os.geteuid() != 0:
        die("run as root from the Arch ISO")
    if not shutil.which("archinstall"):
        die("archinstall not found")
    if not KERNELS:
        die("KERNELS is empty")
    if THEME_VARIANT not in THEMES:
        die(f"THEME_VARIANT must be one of {', '.join(THEMES)}, got {THEME_VARIANT!r}")
    HOSTNAME = ask_hostname()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", USERNAME):
        die(f"invalid username: {USERNAME!r}")
    if not (Path("/usr/share/zoneinfo") / TIMEZONE).exists():
        die(f"invalid timezone: {TIMEZONE!r}")
    if KB_LAYOUT not in run("localectl", "--no-pager", "list-keymaps").splitlines():
        die(f"invalid keyboard layout: {KB_LAYOUT!r}")

    if DISK is None:
        DISK = pick_disk()
    if not DISK.is_block_device():
        die(f"{DISK} is not a block device")

    mounted = run("lsblk", "-no", "MOUNTPOINTS", str(DISK))
    if mounted:
        die(f"{DISK} has mounted partitions:\n{mounted}")

    if secure_boot_enabled() and os.environ.get("ALLOW_SECURE_BOOT") != "1":
        die(
            "Secure Boot is enabled. NVIDIA modules are unsigned and will not load. "
            "Disable Secure Boot or sign the modules. If you accept the risk, "
            "rerun with ALLOW_SECURE_BOOT=1."
        )

    uefi = Path("/sys/firmware/efi").exists()
    pci = run("lspci", "-nn")
    gpu_lines = [
        line for line in pci.splitlines()
        if any(x in line for x in (
            "VGA compatible controller",
            "3D controller",
            "Display controller",
        ))
    ]
    has_nvidia = any("10de:" in line.lower() for line in gpu_lines)
    has_intel = any("8086:" in line.lower() for line in gpu_lines)
    has_amd = any("1002:" in line.lower() for line in gpu_lines)

    if has_nvidia:
        print("NVIDIA device(s):")
        for line in gpu_lines:
            if "10de:" in line.lower():
                print(f"  {line}")

    if NVIDIA_OPEN == "auto":
        if not has_nvidia:
            nvidia_open = False
        else:
            answer = input(
                "Is this NVIDIA GPU Turing or newer (GTX 16xx / RTX 20xx+)? [y/N] "
            ).strip().lower()
            nvidia_open = answer.startswith("y")
    else:
        nvidia_open = NVIDIA_OPEN == "1"

    if nvidia_open and not has_nvidia:
        die("NVIDIA_OPEN=1 but no NVIDIA GPU was detected")
    if has_nvidia and not nvidia_open:
        print(
            "WARNING: using nouveau/open-source. Legacy NVIDIA proprietary drivers "
            "are not available through archinstall."
        )

    size_bytes = int(run("blockdev", "--getsize64", str(DISK)))
    sector_size = int(run("blockdev", "--getss", str(DISK)))
    total_mib = size_bytes // (1024 * 1024)
    boot_mib = 1024
    start_mib = 1
    root_mib = total_mib - start_mib - boot_mib - 1
    if total_mib < 8 * 1024 or root_mib < 4 * 1024:
        die(f"{DISK} is too small ({total_mib} MiB)")

    model = run("lsblk", "-dno", "MODEL", str(DISK)) or "unknown"
    print(f"Target: {DISK} ({model}), {total_mib} MiB, sector size {sector_size}")
    print(f"Root: {root_mib} MiB Btrfs, boot: {boot_mib} MiB FAT32")
    print("This will ERASE the disk.")
    if os.environ.get("CONFIRM") != "1":
        if input("Type YES to continue: ").strip() != "YES":
            sys.exit(0)

    user_pw = getpass.getpass(f"Password for {USERNAME}: ")
    if not user_pw:
        die("user password cannot be empty")
    root_pw = getpass.getpass("Root password (blank = leave unset): ")
    luks_pw = ""
    if LUKS:
        luks_pw = getpass.getpass("LUKS passphrase: ")
        if not luks_pw:
            die("LUKS passphrase cannot be empty")

    from archinstall.lib.models.users import Password

    def yescrypt(password):
        return Password(plaintext=password).enc_password

    boot_id = str(uuid.uuid4())
    root_id = str(uuid.uuid4())
    boot_flags = ["boot", "esp"] if uefi else ["boot"]

    disk_config = {
        "config_type": "default_layout",
        "device_modifications": [
            {
                "device": str(DISK),
                "wipe": True,
                "partitions": [
                    {
                        "obj_id": boot_id,
                        "status": "create",
                        "type": "primary",
                        "start": {
                            "value": 1,
                            "unit": "MiB",
                            "sector_size": {"value": sector_size, "unit": "B"},
                        },
                        "size": {
                            "value": boot_mib,
                            "unit": "MiB",
                            "sector_size": {"value": sector_size, "unit": "B"},
                        },
                        "fs_type": "fat32",
                        "mountpoint": "/boot",
                        "mount_options": [],
                        "flags": boot_flags,
                        "dev_path": None,
                        "btrfs": [],
                    },
                    {
                        "obj_id": root_id,
                        "status": "create",
                        "type": "primary",
                        "start": {
                            "value": start_mib + boot_mib,
                            "unit": "MiB",
                            "sector_size": {"value": sector_size, "unit": "B"},
                        },
                        "size": {
                            "value": root_mib,
                            "unit": "MiB",
                            "sector_size": {"value": sector_size, "unit": "B"},
                        },
                        "fs_type": "btrfs",
                        "mountpoint": None,
                        "mount_options": ["compress=zstd", "noatime"],
                        "flags": [],
                        "dev_path": None,
                        "btrfs": [
                            {"name": "@", "mountpoint": "/"},
                            {"name": "@home", "mountpoint": "/home"},
                            {"name": "@log", "mountpoint": "/var/log"},
                            {"name": "@pkg", "mountpoint": "/var/cache/pacman/pkg"},
                        ],
                    },
                ],
            }
        ],
    }

    if SNAPPER:
        disk_config["btrfs_options"] = {
            "snapshot_config": {"type": "Snapper"}
        }
    if LUKS:
        disk_config["disk_encryption"] = {
            "encryption_type": "luks",
            "partitions": [root_id],
        }

    if nvidia_open:
        gfx_driver = "Nvidia (open kernel module for newer GPUs, Turing+)"
    else:
        gfx_driver = "All open-source"

    packages = [
        "git",
        "mesa",
        "mesa-utils",
        "openssh",
        "kvantum",
        # desktop apps
        "vlc",
        "libreoffice-fresh",
        "obsidian",
        "zed",
        "nvm",
        "okular",
        "thunderbird",
        "flameshot",
        # terminal baseline
        "neovim",
        "tmux",
        "fzf",
        "ripgrep",
        "fd",
        "jq",
        # containers, VMs, networking
        "docker",
        "qemu-full",
        "virt-manager",
        "tailscale",
        *EXTRA_PACKAGES,
    ]
    if nvidia_open:
        packages += ["nvidia-settings", "nvidia-prime"]
        if has_intel:
            packages += [
                "vulkan-intel",
                "intel-media-driver",
                "libva-intel-driver",
            ]
        if has_amd:
            packages += ["vulkan-radeon", "xf86-video-amdgpu"]

    validate_packages(packages)

    config = {
        "hostname": HOSTNAME,
        "timezone": TIMEZONE,
        "ntp": True,
        "locale_config": {
            "kb_layout": KB_LAYOUT,
            "sys_lang": SYS_LANG,
            "sys_enc": SYS_ENC,
            "console_font": "default8x16",
        },
        "kernels": KERNELS,
        "firmware_optdeps": [],
        "swap": {"enabled": True, "algorithm": "zstd"},
        "bootloader_config": {
            "bootloader": "Systemd-boot" if uefi else "Grub",
            "uki": uefi,
            "removable": False,
        },
        "disk_config": disk_config,
        "profile_config": {
            "profile": {
                "main": "Desktop",
                "details": ["KDE Plasma", "Cinnamon"],
            },
            "gfx_driver": gfx_driver,
            "greeter": "sddm",
        },
        "app_config": {
            "audio_config": {"audio": "pipewire"},
            "bluetooth_config": {"enabled": True},
            "firewall_config": {"firewall": "ufw"},
            "fonts_config": {"fonts": ["noto-fonts", "noto-fonts-emoji"]},
            "print_service_config": {"enabled": False},
        },
        "network_config": {"type": "nm"},
        "pacman_config": {"parallel_downloads": 5, "color": True},
        "packages": packages,
        "services": ["sshd", "docker", "libvirtd", "tailscaled"],
        "custom_commands": [
            f"grep -q '{HOSTNAME}' /etc/hosts || echo '127.0.1.1 {HOSTNAME}' >> /etc/hosts",
            extras_script(),
        ],
        "script": "guided",
    }

    creds = {
        "users": [
            {
                "username": USERNAME,
                "sudo": True,
                "enc_password": yescrypt(user_pw),
            }
        ]
    }
    if root_pw:
        creds["root_enc_password"] = yescrypt(root_pw)
    if LUKS:
        creds["encryption_password"] = luks_pw

    WORKDIR.mkdir(parents=True, exist_ok=True)
    cfg_path = WORKDIR / "user_configuration.json"
    creds_path = WORKDIR / "user_credentials.json"
    cfg_path.write_text(json.dumps(config, indent=2) + "\n")
    creds_path.write_text(json.dumps(creds, indent=2) + "\n")
    cfg_path.chmod(0o600)
    creds_path.chmod(0o600)

    print(f"Config: {cfg_path}")
    print(f"Credentials: {creds_path}")
    print(f"GPU driver: {gfx_driver}")
    print(f"Kernels: {', '.join(KERNELS)}")
    print("Validating config (no changes)...")
    subprocess.run(
        [
            "archinstall",
            "--config",
            str(cfg_path),
            "--creds",
            str(creds_path),
            "--silent",
            "--dry-run",
        ],
        check=True,
    )
    print("Starting installation...")
    subprocess.run(
        [
            "archinstall",
            "--config",
            str(cfg_path),
            "--creds",
            str(creds_path),
            "--silent",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
