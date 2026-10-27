#!/usr/bin/env python3
"""Archinstall-driven KDE Plasma Wayland + Cinnamon X11 installer.

Run from the Arch ISO as root. This WIPES the selected disk.

The hostname and username are prompted for (the hostname defaults to "archy")
unless HOSTNAME/USERNAME are exported. Post-install extras (yay, the WhiteSur
"Big Sur" theme for Plasma/GTK/Cinnamon/SDDM) are applied in the chroot and
never abort the installation.
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
# None -> prompt; the hostname prompt falls back to DEFAULT_HOSTNAME.
HOSTNAME = os.environ.get("HOSTNAME") or None
USERNAME = os.environ.get("USERNAME") or None
DEFAULT_HOSTNAME = "archy"
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Jakarta")
KB_LAYOUT = os.environ.get("KB_LAYOUT", "us")
SYS_LANG = os.environ.get("SYS_LANG", "en_US.UTF-8")
SYS_ENC = os.environ.get("SYS_ENC", "UTF-8")
KERNELS = os.environ.get("KERNELS", "linux-zen").split()
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
# Desktop session SDDM preselects (file name in /usr/share/wayland-sessions).
SESSION = os.environ.get("SESSION", "plasma")
# Dual boot: use GRUB (the only bootloader that can list another OS) and
# os-prober, so an existing Windows/other Linux appears in the menu.
DUAL_BOOT = os.environ.get("DUAL_BOOT", "0") == "1"
# AUR-only packages (everything else below is in extra).
AUR_PACKAGES = [
    "zen-browser-bin",
    "onlyoffice-bin",
    "cloudflare-warp-bin",
    "zsh-theme-powerlevel10k-git",
    "ttf-meslo-nerd-font-powerlevel10k",
    *EXTRA_AUR,
]
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
    default = HOSTNAME or DEFAULT_HOSTNAME
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


def ask_username():
    default = USERNAME or ""
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"Username{suffix}: ").strip()
        except EOFError:
            die("no username given")
        value = answer or default
        if not value:
            print("Username cannot be empty.")
            continue
        if value == "root":
            print("'root' is the superuser account; pick another name.")
            continue
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}\$?", value):
            print(f"Invalid username: {value!r} (lowercase, digits, '-', '_')")
            continue
        return value


EXTRAS_SCRIPT = r"""# Post-install extras, executed inside the target chroot as root.
# archinstall runs custom commands *before* genfstab, so this script must never
# exit non-zero: every optional step is reported and skipped on failure.
set -u

user='@@USER@@'
home='/home/@@USER@@'

# Some AUR install scripts run `clear` unconditionally and die with a plain
# "TERM environment variable not set" in a non-tty chroot (whitesur-gtk-theme
# is one of them, and it hides the error).
export TERM=xterm-256color

warn() { printf 'WARNING: %s\n' "$*" >&2; }

aur_log=/var/log/postinstall-aur.log

aur_fail() {
  warn "$*"
  [ -s "$aur_log" ] && tail -n 12 "$aur_log" >&2
  return 0
}

have_space() {
  # zen-browser-bin alone downloads ~110 MiB and unpacks more; running out
  # midway leaves half-built trees behind, so bail out early instead.
  local avail
  avail=$(df -Pm "$home" 2>/dev/null | awk 'NR == 2 {print $4}')
  if [ -z "$avail" ] || [ "$avail" -lt 2048 ]; then
    warn "only ${avail:-0} MiB free in $home; skipping the AUR builds"
    return 1
  fi
}

# sshd is enabled by default, but archinstall's ufw setup denies inbound traffic.
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null 2>&1 || warn 'could not open the SSH port in ufw'
fi

install_yay() {
  command -v yay >/dev/null 2>&1 && return 0

  if ! getent hosts aur.archlinux.org >/dev/null 2>&1; then
    aur_fail 'cannot resolve aur.archlinux.org (DNS/network in the chroot)'
    return 1
  fi

  # Build inside the user's home: /tmp is a small, not necessarily user-writable
  # tmpfs while the ISO's chroot is active (it filled up mid-build otherwise).
  local tmp="$home/.cache/yay-bin"
  printf '=== %s: bootstrap yay-bin\n' "$(date -Is)" >> "$aur_log"
  have_space || return 1
  pacman -S --needed --noconfirm base-devel git >> "$aur_log" 2>&1 \
    || { aur_fail 'base-devel/git install failed'; return 1; }
  install -d -o "$user" -g "$user" "$home/.cache"
  rm -rf "$tmp"
  runuser -u "$user" -- env HOME="$home" git clone --depth 1 \
    https://aur.archlinux.org/yay-bin.git "$tmp" >> "$aur_log" 2>&1 \
    || { aur_fail 'yay-bin clone failed'; rm -rf "$tmp"; return 1; }
  runuser -u "$user" -- env HOME="$home" bash -c "cd $tmp && makepkg -f --noconfirm" \
    >> "$aur_log" 2>&1 \
    || { aur_fail 'yay-bin build failed'; rm -rf "$tmp"; return 1; }
  pacman -U --noconfirm "$tmp"/yay-bin-*.pkg.tar.zst >> "$aur_log" 2>&1 \
    || { aur_fail 'yay-bin install failed'; rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
}

sudoers_dropin=/etc/sudoers.d/99-postinstall-nopasswd

enable_nopasswd() {
  # Both yay and the makepkg fallback install through sudo, and the chroot
  # cannot answer a password prompt.
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > "$sudoers_dropin" \
    || { warn 'cannot write sudoers drop-in'; return 1; }
  chmod 440 "$sudoers_dropin"
}

install_aur() {
  enable_nopasswd || return 1
  printf '=== %s: yay -S --needed %s\n' "$(date -Is)" "$*" >> "$aur_log"
  runuser -u "$user" -- env HOME="$home" yay -S --noconfirm --needed "$@" >> "$aur_log" 2>&1
  local rc=$?
  rm -f "$sudoers_dropin"
  [ "$rc" -ne 0 ] && aur_fail "yay -S $* failed (rc=$rc)"
  return "$rc"
}

aur_build() {
  # Fallback without a helper: clone each package, build and install with makepkg.
  local pkg tmp rc=0
  enable_nopasswd || return 1
  have_space || return 1
  # Same reason as install_yay: keep sources off the chroot's tmpfs.
  install -d -o "$user" -g "$user" "$home/.cache/aur-build"
  for pkg in "$@"; do
    tmp="$home/.cache/aur-build/$pkg"
    printf '=== %s: makepkg %s\n' "$(date -Is)" "$pkg" >> "$aur_log"
    rm -rf "$tmp"
    if ! runuser -u "$user" -- env HOME="$home" git clone --depth 1 \
        "https://aur.archlinux.org/$pkg.git" "$tmp" >> "$aur_log" 2>&1; then
      aur_fail "clone failed for $pkg"
      rc=1
      continue
    fi
    if ! runuser -u "$user" -- env HOME="$home" bash -c \
        "cd $tmp && makepkg -si --noconfirm" >> "$aur_log" 2>&1; then
      aur_fail "build failed for $pkg"
      rc=1
    fi
    rm -rf "$tmp"
  done
  rmdir "$home/.cache/aur-build" 2>/dev/null
  rm -f "$sudoers_dropin"
  return "$rc"
}

configure_session() {
  # SDDM preselects the first session it finds (Cinnamon here); point it at the
  # Plasma Wayland session so KDE, with the WhiteSur look, is the default.
  local session="/usr/share/wayland-sessions/@@SESSION@@.desktop"
  [ -f "$session" ] || return 0
  install -d /var/lib/sddm
  printf '[Last]\nUser=%s\nSession=%s\n' "$user" "$session" > /var/lib/sddm/state.conf
  chown sddm:sddm /var/lib/sddm/state.conf 2>/dev/null || true
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

enable_optional_services() {
  # Cloudflare WARP only exists once its AUR package is installed, and a
  # missing unit in the archinstall services list aborts the whole install, so
  # enable it here instead.
  if [ -f /usr/lib/systemd/system/warp-svc.service ] \
      || [ -f /usr/lib/systemd/system/warp-svc.service.masked ]; then
    systemctl enable warp-svc >> "$aur_log" 2>&1 \
      || warn 'could not enable warp-svc'
  fi
}

configure_bootloader() {
  # archinstall writes /etc/default/grub but never enables os-prober, so with
  # GRUB installed a second operating system is simply missing from the menu.
  local def=/etc/default/grub found
  [ -f "$def" ] || return 0

  if command -v os-prober >/dev/null 2>&1; then
    if grep -q '^#\?GRUB_DISABLE_OS_PROBER' "$def"; then
      sed -i 's/^#\?GRUB_DISABLE_OS_PROBER=.*/GRUB_DISABLE_OS_PROBER=false/' "$def"
    else
      printf 'GRUB_DISABLE_OS_PROBER=false\n' >> "$def"
    fi
  else
    warn 'os-prober is missing; GRUB will not list other operating systems'
  fi

  found=$(os-prober 2>/dev/null | cut -d: -f1,2 | tr '\n' ' ')
  if [ -n "$found" ]; then
    printf '=== %s: other OS detected: %s\n' "$(date -Is)" "$found" >> "$aur_log"
    # A zero timeout hides the menu entirely.
    if grep -q '^GRUB_TIMEOUT=0$' "$def"; then
      sed -i 's/^GRUB_TIMEOUT=0$/GRUB_TIMEOUT=5/' "$def"
    fi
  fi

  if [ -d /boot/grub ]; then
    grub-mkconfig -o /boot/grub/grub.cfg >> "$aur_log" 2>&1 \
      || { aur_fail 'grub-mkconfig failed'; return 1; }
  fi
}

configure_shell() {
  local zshrc="$home/.zshrc" p10k="$home/.p10k.zsh"
  local ghostty="$home/.config/ghostty/config" ksrc="$home/.config/kglobalshortcutsrc"

  # oh-my-zsh from git so `omz update` keeps working (no pacman package).
  if [ ! -d "$home/.oh-my-zsh" ]; then
    runuser -u "$user" -- env HOME="$home" git clone --depth 1 \
      https://github.com/ohmyzsh/ohmyzsh.git "$home/.oh-my-zsh" >> "$aur_log" 2>&1 \
      || warn 'oh-my-zsh clone failed; zsh starts without it'
  fi

  if ! grep -q 'Managed by install-as-me' "$zshrc" 2>/dev/null; then
    cat > "$zshrc" <<'ZSHRC'
# Managed by install-as-me.py
export ZSH="$HOME/.oh-my-zsh"
ZSH_THEME=""
plugins=(
  git
  zsh-completions
  zsh-autosuggestions
  history-substring-search
  zsh-syntax-highlighting
)

[ -f "$ZSH/oh-my-zsh.sh" ] && source "$ZSH/oh-my-zsh.sh"

# powerlevel10k from the packaged theme (see zsh-theme-powerlevel10k-git)
[ -f /usr/share/zsh-theme-powerlevel10k/powerlevel10k.zsh-theme ] \
  && source /usr/share/zsh-theme-powerlevel10k/powerlevel10k.zsh-theme
[[ -f ~/.p10k.zsh ]] && source ~/.p10k.zsh

# nvm
[ -s /usr/share/nvm/init-nvm.sh ] && source /usr/share/nvm/init-nvm.sh

# go install / bun add -g put binaries here
export PATH="$HOME/.bun/bin:$HOME/go/bin:$PATH"

alias ls='eza --icons --group-directories-first'
alias ll='eza -l --icons --group-directories-first'
alias la='eza -la --icons --group-directories-first'
alias cat='bat --paging=never'
command -v zoxide >/dev/null && eval "$(zoxide init zsh)"
ZSHRC
  fi

  # p10k ships ready-made configs; the lean preset is the least noisy.
  if [ ! -f "$p10k" ] && [ -f /usr/share/zsh-theme-powerlevel10k/config/p10k-lean.zsh ]; then
    cp /usr/share/zsh-theme-powerlevel10k/config/p10k-lean.zsh "$p10k"
  fi

  install -d "$home/.config/ghostty"
  if [ ! -f "$ghostty" ]; then
    cat > "$ghostty" <<'GHOSTTY'
font-family = JetBrainsMono Nerd Font
font-size = 11
cursor-style = block
window-padding-x = 8
window-padding-y = 8
mouse-hide-while-typing = true
confirm-close-surface = false
GHOSTTY
  fi

  # CopyQ is a tray clipboard manager; it needs to be started with the session.
  install -d "$home/.config/autostart"
  if [ ! -f "$home/.config/autostart/copyq.desktop" ]; then
    cat > "$home/.config/autostart/copyq.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=CopyQ
Exec=copyq --start-server
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
DESKTOP
  fi

  # Ctrl+Alt+T for the terminal. Plasma stores app launch shortcuts under
  # [services][<desktop file>] in kglobalshortcutsrc; complement it with KDE's
  # default terminal so "Open Terminal" in Dolphin/Kate uses Ghostty too.
  if ! grep -q 'com.mitchellh.ghostty.desktop' "$ksrc" 2>/dev/null; then
    printf '\n[services][com.mitchellh.ghostty.desktop]\n_launch=Ctrl+Alt+T,none,Ghostty\n' >> "$ksrc"
  fi
  if command -v kwriteconfig6 >/dev/null 2>&1; then
    runuser -u "$user" -- env HOME="$home" kwriteconfig6 \
      --file kdeglobals --group General --key TerminalApplication ghostty 2>/dev/null \
      || warn 'could not set kdeglobals TerminalApplication'
  fi

  grep -qx /usr/bin/zsh /etc/shells 2>/dev/null \
    || printf '/usr/bin/zsh\n' >> /etc/shells
  usermod -s /usr/bin/zsh "$user" || warn 'could not set zsh as the login shell'

  chown -R "$user:$user" "$home/.config" "$home/.zshrc" "$home/.p10k.zsh" 2>/dev/null
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

patch_sddm_theme() {
  # Plasma/SDDM >= 6.8 dropped the VirtualKeyboardLoader type from
  # org.kde.breeze.components, but the packaged WhiteSur greeter still
  # instantiates it, so the entire login screen falls back with
  # "VirtualKeyboardLoader is not a type". Provide the type from the theme's own
  # directory (implicit import) and keep the on-screen keyboard disabled.
  local dir
  for dir in /usr/share/sddm/themes/WhiteSur*; do
    [ -f "$dir/Main.qml" ] || continue
    grep -q 'VirtualKeyboardLoader' "$dir/Main.qml" || continue
    if [ -f "$dir/VirtualKeyboardLoader.qml" ] \
        && ! grep -q 'Compatibility shim' "$dir/VirtualKeyboardLoader.qml"; then
      continue
    fi
    cat > "$dir/VirtualKeyboardLoader.qml" <<'QML'
// Compatibility shim for SDDM/Plasma >= 6.8, which removed the
// VirtualKeyboardLoader type from org.kde.breeze.components while the WhiteSur
// greeter still instantiates it. Declares the surface Main.qml uses and keeps
// the on-screen keyboard disabled instead of failing to load the login screen.
import QtQuick

Item {
    property var screenRoot
    property var mainStack
    property var mainBlock
    property var passwordField
    property bool keyboardActive: false
    function showHide() {}
    visible: false
}
QML
    printf '=== %s: patched SDDM theme %s\n' "$(date -Is)" "$dir" >> "$aur_log"
  done
}

patch_lnf_layout() {
  # The WhiteSur layout instantiates org.kde.plasma.splitdigitalclock, a KDE
  # Store applet that no repository packages, so applying the look-and-feel
  # fails with "could not find requested component". The stock digital clock
  # accepts the same configuration keys (showDate, use24hFormat, fontFamily).
  local js src="$home/.config/plasma-org.kde.plasma.desktop-appletsrc"
  for js in /usr/share/plasma/look-and-feel/com.github.vinceliuice.WhiteSur*/contents/layouts/*.js; do
    [ -f "$js" ] || continue
    grep -q 'splitdigitalclock' "$js" || continue
    sed -i 's/org\.kde\.plasma\.splitdigitalclock/org.kde.plasma.digitalclock/g' "$js" \
      || { warn "could not patch $js"; continue; }
    printf '=== %s: patched layout %s\n' "$(date -Is)" "$js" >> "$aur_log"
  done
  # Repair a panel that was already built from the broken layout.
  if [ -f "$src" ] && grep -q 'splitdigitalclock' "$src"; then
    sed -i 's/plugin=org\.kde\.plasma\.splitdigitalclock/plugin=org.kde.plasma.digitalclock/' "$src" \
      || warn 'could not patch the existing desktop layout'
  fi
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
  # GTK falls back to Adwaita on its own if the theme package failed to build.
  [ -d "/usr/share/themes/@@GTK@@" ] \
    || warn 'GTK theme @@GTK@@ is missing; GTK apps keep the default theme'

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

  # Cinnamon reads dconf. Use a system database instead of a per-user write:
  # dbus-run-session never returns while gvfs daemons linger, which hung the
  # installer inside the chroot.
  install -d /etc/dconf/db/local.d /etc/dconf/profile
  cat > /etc/dconf/db/local.d/00-whitesur <<'CFG'
[org/cinnamon/desktop/interface]
gtk-theme='@@GTK@@'
icon-theme='@@ICONS@@'
cursor-theme='WhiteSur-cursors'

[org/cinnamon/theme]
name='@@GTK@@'

[org/cinnamon/desktop/background]
picture-uri='file:///usr/share/wallpapers/@@WALLPAPER@@/contents/images/3840x2160.jpg'
CFG
  if [ -f /etc/dconf/profile/user ]; then
    grep -q '^system-db:local$' /etc/dconf/profile/user \
      || printf 'system-db:local\n' >> /etc/dconf/profile/user
  else
    printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
  fi
  dconf update 2>/dev/null || warn 'dconf update failed; Cinnamon keeps the default look'

  chown -R "$user:$user" "$home/.config" "$home/.local"
  # Mirror only what was written here; copying the whole .config would leak
  # whatever the live session had already created.
  install -d /etc/skel/.config /etc/skel/.local/bin
  for item in kdeglobals plasmarc kwinrc kcminputrc Kvantum gtk-3.0 gtk-4.0 autostart; do
    cp -a "$home/.config/$item" /etc/skel/.config/ || warn "could not copy $item to /etc/skel"
  done
  cp -a "$home/.local/bin/whitesur-first-login.sh" /etc/skel/.local/bin/
}

if @@WANT_YAY@@; then
  install_yay || warn 'yay is unavailable; AUR packages fall back to makepkg'
fi

if @@WANT_APPS@@; then
  if ! command -v yay >/dev/null 2>&1 || ! install_aur @@AUR_PACKAGES@@; then
    warn 'installing @@AUR_PACKAGES@@ directly with makepkg'
    aur_build @@AUR_PACKAGES@@
  fi
  for pkg in @@AUR_PACKAGES@@; do
    pacman -Q "$pkg" >/dev/null 2>&1 || warn "AUR package $pkg is still missing"
  done
fi

install_node
enable_optional_services
configure_groups
configure_session
configure_shell
configure_bootloader

if @@WANT_THEME@@; then
  if [ ! -d "/usr/share/plasma/desktoptheme/@@PLASMA@@" ] || [ ! -d "/usr/share/themes/@@GTK@@" ]; then
    if ! command -v yay >/dev/null 2>&1 || ! install_aur \
        whitesur-gtk-theme whitesur-icon-theme whitesur-cursor-theme-git whitesur-kde-theme; then
      warn 'installing WhiteSur directly with makepkg'
      aur_build whitesur-gtk-theme whitesur-icon-theme whitesur-cursor-theme-git whitesur-kde-theme
    fi
  fi
  if [ -d "/usr/share/plasma/desktoptheme/@@PLASMA@@" ] || [ -d "/usr/share/themes/@@GTK@@" ]; then
    apply_theme
    patch_sddm_theme
    patch_lnf_layout
  else
    warn 'WhiteSur theme is not installed; desktop keeps its default look'
  fi
fi

exit 0
"""


def extras_script():
    """Build the chroot script that installs yay, AUR apps and the theme."""
    if not USERNAME:
        die("extras_script() needs a resolved USERNAME (prompt runs in main)")
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
        "WANT_THEME": "true" if INSTALL_THEME else "false",
        "WANT_APPS": "true" if AUR_PACKAGES else "false",
        "SESSION": SESSION,
        "AUR_PACKAGES": " ".join(AUR_PACKAGES),
    }.items():
        script = script.replace(f"@@{token}@@", value)
    if "@@" in script:
        die("internal error: unsubstituted placeholder in extras script")
    return script


def main():
    global DISK, HOSTNAME, USERNAME

    if os.geteuid() != 0:
        die("run as root from the Arch ISO")
    if not shutil.which("archinstall"):
        die("archinstall not found")
    if not KERNELS:
        die("KERNELS is empty")
    if THEME_VARIANT not in THEMES:
        die(f"THEME_VARIANT must be one of {', '.join(THEMES)}, got {THEME_VARIANT!r}")
    HOSTNAME = ask_hostname()
    USERNAME = ask_username()
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
        "sudo",
        "kvantum",
        # archinstall's bluetooth_config already pulls these in; listed
        # explicitly so bluetoothctl is guaranteed
        "bluez",
        "bluez-utils",
        # desktop apps
        "vlc",
        "libreoffice-fresh",
        "obsidian",
        "zed",
        "nvm",
        "okular",
        "thunderbird",
        "flameshot",
        "copyq",
        # plumbing: media codecs for GTK apps, archive formats, device mounting,
        # CJK fonts, sensors and the Wayland clipboard
        "gst-plugins-good",
        "gst-plugins-bad",
        "gst-plugins-ugly",
        "gst-libav",
        "7zip",
        "unrar",
        "gvfs-mtp",
        "gvfs-gphoto2",
        "noto-fonts-cjk",
        "lm_sensors",
        "wl-clipboard",
        # terminal baseline
        "neovim",
        "tmux",
        "fzf",
        "ripgrep",
        "fd",
        "jq",
        "eza",
        "bat",
        "zoxide",
        # dig/host/nslookup (no standalone package on Arch)
        "bind",
        # shell
        "zsh",
        "zsh-completions",
        "zsh-autosuggestions",
        "zsh-syntax-highlighting",
        "zsh-history-substring-search",
        "ghostty",
        "ttf-jetbrains-mono-nerd",
        # developer toolchains
        "go",
        "uv",
        "bun",
        # containers, VMs, networking
        "docker",
        "qemu-full",
        "virt-manager",
        "tailscale",
        # org.kde.milou applet used by the WhiteSur desktop layout
        "milou",
        *EXTRA_PACKAGES,
    ]
    if DUAL_BOOT:
        # lets GRUB list an existing Windows/other Linux
        packages.append("os-prober")
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
            # GRUB is the only option that can list another installed OS
            # (via os-prober); DUAL_BOOT forces it and disables UKI.
            "bootloader": "Grub" if (DUAL_BOOT or not uefi) else "Systemd-boot",
            "uki": uefi and not DUAL_BOOT,
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
