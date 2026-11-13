#!/bin/sh
# Buildroot post-image: check the kernel, assemble disk.img and a compressed copy.
set -e

BOARD_DIR="$(dirname "$0")"

# The fragment is merged by kconfig, which silently drops options whose
# dependencies are not met. Fail the build instead of shipping a kernel
# without the drivers Mantis needs.
KCONFIG="$(ls -d "$BUILD_DIR"/linux-[0-9]*/.config | head -n 1)"
missing=""
for opt in BPF_SYSCALL BPF_JIT XDP_SOCKETS DEBUG_INFO_BTF HUGETLBFS BRIDGE NF_TABLES NF_TABLES_INET NFT_CT SOFT_WATCHDOG \
	IGB IGBVF IGC E1000 E1000E IXGBE I40E R8169 8139TOO VIRTIO_NET VMXNET3; do
	grep -qx "CONFIG_$opt=y" "$KCONFIG" || missing="$missing $opt"
done
# firmware-loading drivers (wired, and WiFi for the management NIC) are modules
for opt in ICE USB_RTL8152 TIGON3 BNX2 \
	CFG80211 MAC80211 IWLWIFI IWLMVM RTW88_8822CE RTW89_8852BE MT7921E MT7921U ATH10K_PCI RTL8XXXU; do
	grep -qE "^CONFIG_$opt=[ym]$" "$KCONFIG" || missing="$missing $opt(m)"
done
if [ -n "$missing" ]; then
	echo "ERROR: kernel config is missing (must be =y):$missing" >&2
	exit 1
fi

# A/B state for grub.cfg: committed slot a, nothing to try. GRUB rewrites this
# 1 KiB block in place, Linux too (/usr/lib/mantis/slots.sh).
rm -f "$BINARIES_DIR/efi-part/EFI/BOOT/grubenv"
"$HOST_DIR/bin/grub-editenv" "$BINARIES_DIR/efi-part/EFI/BOOT/grubenv" create
"$HOST_DIR/bin/grub-editenv" "$BINARIES_DIR/efi-part/EFI/BOOT/grubenv" set slot=a try_slot= try_count=0
# the kernel now lives in each root slot
rm -f "$BINARIES_DIR/efi-part/bzImage"

# the rootfs must fit a root slot of disk.img
slot_kib=$((448 * 1024))
root_kib=$(( $(stat -L -c %s "$BINARIES_DIR/rootfs.ext4") / 1024 ))
if [ "$root_kib" -gt "$slot_kib" ]; then
	echo "ERROR: rootfs.ext4 ($root_kib KiB) is larger than a root slot ($slot_kib KiB)" >&2
	exit 1
fi

support/scripts/genimage.sh -c "$BOARD_DIR/genimage.cfg"

# disk.img.gz: new installs. rootfs.ext4.gz: upgrades (mantis-upgrade writes it
# into the inactive slot), with its uncompressed SHA-256 for the write check.
gzip -9 -n -c "$BINARIES_DIR/disk.img" > "$BINARIES_DIR/disk.img.gz"
gzip -9 -n -c "$BINARIES_DIR/rootfs.ext4" > "$BINARIES_DIR/rootfs.ext4.gz"
sha256sum "$BINARIES_DIR/rootfs.ext4" | cut -d' ' -f1 > "$BINARIES_DIR/rootfs.ext4.sha256"
