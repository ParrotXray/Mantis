# A/B root slots, sourced by mantis-upgrade, S05trialdog and S99upgrade.
#
# Disk: p1 EFI (GRUB, grubenv), p2 root slot a, p3 root slot b, p4 data.
# The booted slot and its partition come from the kernel command line that
# grub.cfg builds (mantis.slot=, root=PARTUUID=); the other partitions are
# found on the same disk by number, so a second disk with the same image
# (e.g. a USB stick) is never touched.

MANTIS_EFI_MNT="/run/mantis-efi"
MANTIS_GRUBENV="$MANTIS_EFI_MNT/EFI/BOOT/grubenv"
MANTIS_UPGRADE_LOG="/opt/mantis/state/logs/upgrade.log"

upgrade_log() {
	echo "=== $(date '+%F %T') $*" | tee -a "$MANTIS_UPGRADE_LOG" >> /opt/mantis/state/logs/console.log 2>/dev/null
	logger -t mantis-upgrade "$*" 2>/dev/null
}

cmdline_get() {
	tr ' ' '\n' < /proc/cmdline | sed -n "s/^$1=//p" | tail -n 1
}

slot_booted() { cmdline_get mantis.slot; }
slot_other() { [ "$1" = a ] && echo b || echo a; }
slot_partn() { [ "$1" = b ] && echo 3 || echo 2; }
is_trial_boot() { [ "$(cmdline_get mantis.trial)" = 1 ]; }
# mantis.trial=1 stays on the command line until the next boot; once
# S99upgrade has confirmed the slot this is an ordinary boot
MANTIS_TRIAL_CONFIRMED="/run/mantis-trial-confirmed"
trial_unconfirmed() { is_trial_boot && [ ! -e "$MANTIS_TRIAL_CONFIRMED" ]; }

# /dev/sda, /dev/nvme0n1, /dev/vda: the disk holding the booted root
boot_disk() {
	uuid=$(cmdline_get root | sed -n 's/^PARTUUID=//p')
	dev=$(findfs "PARTUUID=$uuid" 2>/dev/null) || return 1
	echo "/dev/$(lsblk -no PKNAME "$dev" | head -n 1)"
}

# partition device number $1 of the boot disk (sda2, nvme0n1p2, vda2)
part_dev() {
	disk=$(boot_disk) || return 1
	for p in /sys/class/block/"$(basename "$disk")"/*/partition; do
		[ "$(cat "$p")" = "$1" ] && { echo "/dev/$(basename "$(dirname "$p")")"; return 0; }
	done
	return 1
}

slot_dev() { part_dev "$(slot_partn "$1")"; }

efi_mount() {
	grep -q " $MANTIS_EFI_MNT " /proc/mounts && return 0
	mkdir -p "$MANTIS_EFI_MNT"
	mount -t vfat "$(part_dev 1)" "$MANTIS_EFI_MNT"
}

efi_umount() {
	sync
	umount "$MANTIS_EFI_MNT" 2>/dev/null
}

# grubenv: "# GRUB Environment Block\n" + key=value lines, padded with '#' to
# exactly 1024 bytes. GRUB rewrites it in place, so it is rewritten in place
# here too (same size, conv=notrunc), never replaced by a new file.
env_get() {
	sed -n "s/^$1=//p" "$MANTIS_GRUBENV" 2>/dev/null | tail -n 1
}

# env_set key=value ... (keeps the other keys)
env_set() {
	tmp=/run/grubenv.new
	{
		echo "# GRUB Environment Block"
		grep -v '^#' "$MANTIS_GRUBENV" | while IFS= read -r line; do
			key=${line%%=*}
			for kv in "$@"; do
				[ "${kv%%=*}" = "$key" ] && continue 2
			done
			echo "$line"
		done
		for kv in "$@"; do
			echo "$kv"
		done
	} > "$tmp"
	size=$(wc -c < "$tmp")
	if [ "$size" -gt 1024 ]; then
		echo "grubenv would exceed 1024 bytes" >&2
		return 1
	fi
	head -c $((1024 - size)) /dev/zero | tr '\0' '#' >> "$tmp"
	dd if="$tmp" of="$MANTIS_GRUBENV" bs=1024 count=1 conv=notrunc 2>/dev/null
	sync
	rm -f "$tmp"
}

# release info of a slot: /etc/mantis-release, mounted read-only for the
# inactive slot
slot_release() {
	if [ "$1" = "$(slot_booted)" ]; then
		cat /etc/mantis-release 2>/dev/null
		return
	fi
	dev=$(slot_dev "$1") || return 1
	mnt=/run/mantis-slot-$1
	mkdir -p "$mnt"
	if mount -t ext4 -o ro "$dev" "$mnt" 2>/dev/null; then
		cat "$mnt/etc/mantis-release" 2>/dev/null
		umount "$mnt"
	fi
	rmdir "$mnt" 2>/dev/null
}

release_field() {
	echo "$1" | sed -n "s/^$2=//p"
}
