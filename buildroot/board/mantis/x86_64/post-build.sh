#!/bin/sh
# Buildroot post-build: GRUB config, serial console login, login banner.
set -e

BOARD_DIR="$(dirname "$0")"

cp -f "$BOARD_DIR/grub.cfg" "$BINARIES_DIR/efi-part/EFI/BOOT/grub.cfg"

# Login prompt on the serial console as well as on tty1 (headless boxes)
if ! grep -q '^ttyS0::' "$TARGET_DIR/etc/inittab"; then
	sed -i '/# GENERIC_SERIAL$/a ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100 # MANTIS_SERIAL' \
		"$TARGET_DIR/etc/inittab"
fi

mkdir -p "$TARGET_DIR/opt/mantis"

# SSH host keys live on the data partition: the same keys after an upgrade
# (S22security creates the directory)
rm -rf "$TARGET_DIR/etc/dropbear"
ln -s /opt/mantis/ssh/dropbear "$TARGET_DIR/etc/dropbear"

# systemd mounts these on Ubuntu; BusyBox init only mounts what fstab lists.
# Mantis pins its XSK / DEVMAP maps in bpffs and fails to start without it.
add_fstab() {
	grep -q "^$1[[:space:]]" "$TARGET_DIR/etc/fstab" || printf '%s\n' "$2" >> "$TARGET_DIR/etc/fstab"
}
add_fstab bpf     "bpf		/sys/fs/bpf	bpf	mode=0700	0	0"
add_fstab cgroup2 "cgroup2		/sys/fs/cgroup	cgroup2	nsdelegate	0	0"

# Tools the init scripts rely on. A defconfig line Buildroot ignores (an
# option without a prompt, an unmet dependency) would otherwise only show up
# at run time.
missing=""
for f in usr/sbin/nft usr/bin/stunnel usr/bin/openssl usr/sbin/ntpd usr/bin/flock \
	usr/sbin/wpa_supplicant usr/sbin/ethtool usr/share/zoneinfo/Etc/UTC usr/share/zoneinfo/Asia/Taipei \
	usr/bin/curl sbin/acpid etc/acpi/PWRF/00000080 sbin/blockdev bin/lsblk boot/bzImage usr/sbin/mantis-upgrade; do
	[ -e "$TARGET_DIR/$f" ] || missing="$missing /$f"
done
if [ -n "$missing" ]; then
	echo "ERROR: missing from the rootfs:$missing" >&2
	exit 1
fi

# linux-firmware installs every API version of each Intel WiFi firmware;
# keep the one the kernel's iwlwifi loads (about 130 MiB -> 25 MiB).
python3 -I "$BOARD_DIR/../prune-iwlwifi.py" "$TARGET_DIR"

# Every shared library the release (and the Suricata / ONNX Runtime embedded
# in it) links against must be in the rootfs.
python3 -I "$BOARD_DIR/../check-needed.py" "$TARGET_DIR" "$TARGET_DIR/usr/share/mantis-dist/mantis"

# What is in this root slot: read by mantis-upgrade status (also for the
# inactive slot) and by S99upgrade.
read -r dist_sha dist_variant < "$TARGET_DIR/usr/share/mantis-dist/.dist-version"
cat > "$TARGET_DIR/etc/mantis-release" <<EOF
MANTIS_SHA=$dist_sha
MANTIS_VARIANT=$dist_variant
BUILDROOT=$BR2_VERSION_FULL
BUILT=$(date -u '+%F %T UTC')
EOF

cat > "$TARGET_DIR/etc/motd" <<'EOF'

  Mantis appliance
  ----------------
  First time here?   mantis-setup
  Service:           /etc/init.d/S90mantis {start|stop|restart|status}
  Web UI:            https://<management address>/  (self-signed certificate)
  Logs:              /opt/mantis/state/logs/console.log  (rotated by mantis-logrotate)
  Data partition:    /opt/mantis (config.toml, state/, appliance.conf, ssh/, tls/)
  Security / time:   /etc/init.d/S22security restart, /etc/init.d/S42ntp restart
  Upgrade (A/B):     mantis-upgrade status | latest | install <rootfs.ext4.gz>

EOF
