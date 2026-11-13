# Mantis fail-open bridge, sourced by /etc/init.d/S21bypass and S90mantis.
#
# Same design as deploy.sh on Ubuntu: with BYPASS=open in
# /opt/mantis/appliance.conf (the default) ingress and egress are permanent
# ports of a Linux bridge, br-mantis (STP off, no address).
#   - Mantis running: its XDP program sees every frame before the bridge and
#     forwards it itself (DEVMAP, AF_XDP); the bridge carries nothing.
#   - Mantis not running (boot, stopped, crashed, restarting): no XDP
#     program, so the bridge forwards, uninspected.
# A crash needs no action: the kernel detaches the XDP program when the
# process exits. A hung Mantis keeps its program attached and would
# black-hole traffic; S90mantis kills it and detaches XDP (xdp_detach).
# BYPASS=closed: no bridge, nothing crosses while Mantis is not running.

MANTIS_BRIDGE="br-mantis"
MANTIS_BYPASS_LOG="/opt/mantis/state/logs/console.log"

bypass_log() {
	echo "=== $(date '+%F %T') $*" >> "$MANTIS_BYPASS_LOG" 2>/dev/null
}

bypass_enabled() {
	[ "${BYPASS:-open}" = open ] && [ -n "$NIC_INGRESS" ] && [ -n "$NIC_EGRESS" ]
}

bypass_active() {
	[ -e "/sys/class/net/$MANTIS_BRIDGE" ]
}

# Create the bridge with both NICs (idempotent), or remove it with
# BYPASS=closed.
bridge_apply() {
	if ! bypass_enabled; then
		bridge_remove
		return 0
	fi
	if ! bypass_active; then
		ip link add "$MANTIS_BRIDGE" type bridge stp_state 0 forward_delay 0 || return 1
		bypass_log "fail-open bridge $MANTIS_BRIDGE created over $NIC_INGRESS and $NIC_EGRESS"
	fi
	# Forward link-local frames too (LLDP, 802.1X ...), like a cable would;
	# bits 0-2 (STP, pause, LACP) cannot be set and STP passes with STP off.
	ip link set "$MANTIS_BRIDGE" type bridge group_fwd_mask 0xfff8 2>/dev/null
	for nic in "$NIC_INGRESS" "$NIC_EGRESS"; do
		if [ "$(basename "$(readlink "/sys/class/net/$nic/master" 2>/dev/null)")" != "$MANTIS_BRIDGE" ]; then
			ip addr flush dev "$nic"
			ip link set dev "$nic" master "$MANTIS_BRIDGE"
		fi
		ip link set dev "$nic" up
	done
	ip link set "$MANTIS_BRIDGE" up
}

bridge_remove() {
	bypass_active || return 0
	for nic in "$NIC_INGRESS" "$NIC_EGRESS"; do
		[ -n "$nic" ] && ip link set dev "$nic" nomaster 2>/dev/null
	done
	ip link del "$MANTIS_BRIDGE"
	bypass_log "fail-open bridge $MANTIS_BRIDGE removed (BYPASS=closed)"
}

# Drop an XDP program a killed Mantis may have left (normally the kernel
# does this when the process exits): without it the bridge gets the frames.
xdp_detach() {
	for nic in "$NIC_INGRESS" "$NIC_EGRESS"; do
		[ -n "$nic" ] && ip link set dev "$nic" xdp off 2>/dev/null
	done
}

xdp_attached() {
	ip link show dev "$1" 2>/dev/null | grep -q ' xdp'
}
