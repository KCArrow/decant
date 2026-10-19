#!/usr/bin/env bash
# Build the portable single-file binary (dist/decant-x86_64) with PyInstaller.
#
# CI runs this inside quay.io/pypa/manylinux_2_28_x86_64 (AlmaLinux 8, glibc 2.28),
# so the binary runs on RHEL/Rocky 8 and newer, Fedora, Arch, Debian 11+, Ubuntu 20.04+.
# Building on a newer distro works too, but the result only runs on distros at
# least that new.
#
#   PYTHON=python3.12 packaging/build-portable.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}

# Inside the manylinux image: install the X11/xcb libraries Qt's xcb plugin
# links against, so PyInstaller can find and bundle them.
if [ -f /etc/almalinux-release ] && command -v dnf >/dev/null; then
    dnf install -y -q epel-release
    dnf install -y -q libxkbcommon libxkbcommon-x11 xcb-util-cursor xcb-util-wm xcb-util-image \
        xcb-util-keysyms xcb-util-renderutil libX11 libxcb mesa-libEGL mesa-libGL fontconfig \
        freetype dbus-libs glib2 libwayland-client libwayland-cursor libwayland-egl
fi

"$PY" -m venv .build-venv
. .build-venv/bin/activate
pip install -q --upgrade pip
pip install -q "PySide6-Essentials>=6.5" "pyinstaller>=6.3"
pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/decant.spec
ls -l dist/decant-x86_64
dist/decant-x86_64 --version
