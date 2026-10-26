#!/usr/bin/env python3
"""Archinstall-driven KDE Plasma Wayland + Cinnamon X11 installer.

Run from the Arch ISO as root. This WIPES the selected disk.
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
HOSTNAME = os.environ.get("HOSTNAME", "daffaputra")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Jakarta")
USERNAME = os.environ.get("USERNAME", "daffaputra")
KB_LAYOUT = os.environ.get("KB_LAYOUT", "us")
SYS_LANG = os.environ.get("SYS_LANG", "en_US.UTF-8")
SYS_ENC = os.environ.get("SYS_ENC", "UTF-8")
KERNELS = os.environ.get("KERNELS", "linux").split()
SNAPPER = os.environ.get("SNAPPER", "0") == "1"
LUKS = os.environ.get("LUKS", "0") == "1"
NVIDIA_OPEN = os.environ.get("NVIDIA_OPEN", "auto")
WORKDIR = Path("/root/archinstall-daffaputra")


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


def main():
    global DISK

    if os.geteuid() != 0:
        die("run as root from the Arch ISO")
    if not shutil.which("archinstall"):
        die("archinstall not found")
    if not KERNELS:
        die("KERNELS is empty")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", HOSTNAME):
        die(f"invalid hostname: {HOSTNAME!r}")
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

    packages = ["git", "mesa", "mesa-utils"]
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
        "services": [],
        "custom_commands": [
            f"grep -q '{HOSTNAME}' /etc/hosts || echo '127.0.1.1 {HOSTNAME}' >> /etc/hosts"
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
