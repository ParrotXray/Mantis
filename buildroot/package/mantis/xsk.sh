# Shared by mantis-setup and S90mantis (sourced; POSIX sh and bash).
#
# Drivers with AF_XDP zero-copy in this kernel (6.12). igb, e1000e, r8169
# and most others only do copy mode: binding them with xsk_bind_mode =
# "zero" fails ("non-zero error code returned when creating AF_XDP socket")
# and Mantis exits. igb gains zero-copy in Linux 6.14: update this list
# with the kernel.
XSK_ZC_DRIVERS="i40e ice ixgbe igc mlx5_core"

xsk_driver() {
	ethtool -i "$1" 2>/dev/null | sed -n 's/^driver: //p' || true
}

# xsk_zc_capable NIC...: every NIC's driver has AF_XDP zero-copy
xsk_zc_capable() {
	for _nic in "$@"; do
		case " $XSK_ZC_DRIVERS " in
			*" $(xsk_driver "$_nic") "*) ;;
			*) return 1 ;;
		esac
	done
	return 0
}
