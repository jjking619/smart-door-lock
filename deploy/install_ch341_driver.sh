#!/bin/bash
# Build and install the CH340/CH341 USB-serial driver for kernels shipped without
# CONFIG_USB_SERIAL_CH341 (e.g. Quectel Pi M2 RK3576 image). Re-run after kernel updates.
set -euo pipefail

KVER="$(uname -r)"
HEADERS="${KERNEL_HEADERS:-/usr/src/linux-headers-6.1-rockchip}"
WORK="${CH341_BUILD_DIR:-${TMPDIR:-/tmp}/ch341-build}"

if [ -e /sys/module/ch341 ]; then
    echo "ch341 driver already loaded"
    exit 0
fi

if ! grep -q "\"$KVER\"" "$HEADERS/include/generated/utsrelease.h" 2>/dev/null; then
    echo "Kernel headers in $HEADERS do not match running kernel $KVER" >&2
    exit 1
fi

KMAJOR_MINOR_PATCH="${KVER%%-*}"
mkdir -p "$WORK"
if [ ! -s "$WORK/ch341.c" ]; then
    curl -fsSL -o "$WORK/ch341.c" \
        "https://raw.githubusercontent.com/gregkh/linux/v${KMAJOR_MINOR_PATCH}/drivers/usb/serial/ch341.c"
fi
echo "obj-m += ch341.o" > "$WORK/Makefile"
make -C "$HEADERS" M="$WORK" modules

sudo install -D -m 0644 "$WORK/ch341.ko" "/lib/modules/$KVER/extra/ch341.ko"
sudo depmod -a "$KVER"
echo ch341 | sudo tee /etc/modules-load.d/ch341.conf >/dev/null
sudo modprobe ch341

sleep 1
ls -l /dev/ttyUSB* /dev/serial/by-id/ 2>/dev/null || echo "Driver loaded; replug the CH340 adapter if no ttyUSB appears"
