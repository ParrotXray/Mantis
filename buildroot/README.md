# Mantis Buildroot appliance

A minimal, self-contained x86_64 disk image that boots straight into Mantis:
no apt, no systemd, no netplan. It is built with [Buildroot](https://buildroot.org)
from a Mantis CI release, the same tarball `deploy.sh` installs on Ubuntu.

## What is inside

| Part | Details |
|---|---|
| Toolchain | Bootlin x86-64 glibc stable (gcc 14, glibc 2.42; the release needs glibc ≥ 2.39) |
| Kernel | 6.12 LTS, `x86_64_defconfig` + [`linux.fragment`](board/mantis/x86_64/linux.fragment): BPF JIT, AF_XDP, BTF, hugepages. NICs built in: Intel igb (i350), igbvf, igc, e1000, e1000e, ixgbe(vf), i40e, iavf; Realtek r8169 (RTL8111/8168, 8125 2.5G, 8126 5G), 8139; Marvell sky2, Atheros alx/atl1c, nVidia forcedeth, Aquantia, mlx5; virtio, vmxnet3, Hyper-V, AWS ENA; USB CDC/AX88179. As modules (firmware read at probe): Intel ice, Realtek USB r8152, Broadcom tg3/bnx2/bnxt. The build fails if igb, r8169 or the eBPF options are missing |
| Firmware | linux-firmware, only rtl_nic (r8169, r8152), tg3, bnx2, ice DDP, and the WiFi firmware below |
| WiFi (management NIC only) | cfg80211/mac80211 + wireless-regdb, wpa_supplicant (WPA2/WPA3), iw, rfkill. Drivers (modules) and firmware: Intel iwlwifi (7260/7265/3168/8265, 9xxx, AX200/201/210/211, BE200), Realtek rtw88/rtw89 (RTL8821CE/8822BE/8822CE/8852AE/BE/CE, USB 8811CU/8821CU/8822BU/CU) and rtl8xxxu USB, MediaTek MT7921/7922/7925 (PCIe/USB), MT7601U/MT76x2U, Qualcomm ath10k QCA6174/QCA9377, ath9k, AR9271 USB, Ralink RT2800 USB |
| Init | BusyBox init, eudev (same predictable NIC names as Ubuntu, e.g. `enp4s0f0`) |
| Mantis | Release tarball unpacked to `/usr/share/mantis-dist` ([`package/mantis`](package/mantis)) |
| Suricata runtime libs | libpcap, libnet, libevent, hiredis, libmaxminddb, libmagic, libunwind, libcap-ng, libnetfilter_queue, jansson, libyaml, pcre2, lz4, elfutils, zlib; Debian sonames `libpcap.so.0.8` / `libnet.so.1` are symlinked |
| Hyperscan | [`package/vectorscan`](package/vectorscan): Vectorscan 5.4.11 (`libhs.so.5`), fat runtime: core2, corei7, AVX2, AVX-512, AVX-512 VBMI picked at run time |
| Security | nftables (management firewall), stunnel + openssl (HTTPS in front of the web UI), dropbear (SSH) |
| Extras | BusyBox ntpd, crond, nc; bash, ethtool, iproute2, e2fsprogs, util-linux |

### Disk layout (`disk.img`, GPT, UEFI boot)

| # | Partition | Size | Mounted | Contents |
|---|---|---|---|---|
| 1 | `efi` (vfat) | 32 MiB | – | GRUB, `grub.cfg`, `grubenv` (A/B state) |
| 2 | `root-a` (ext4) | 448 MiB | `/` when slot a runs | OS, kernel (`/boot/bzImage`), Mantis release |
| 3 | `root-b` (ext4) | 448 MiB | `/` when slot b runs | the other slot: empty until the first upgrade |
| 4 | `mantis-data` (ext4) | 64 MiB → whole disk | `/opt/mantis` | `config.toml`, `state/` (app.db, records, logs, rules), `appliance.conf`, `ssh/`, `tls/` |

On first boot `S20data` grows partition 4 to the end of the disk. `/opt/mantis`
holds the release (bind mounts of `mantis` and `share/` from the running slot) plus
the data that must survive an upgrade, so it looks the same as a `deploy.sh` install.

## Build

On an Ubuntu/Debian build host (about 15 GB of disk; 1 to 2 hours on 4 cores the first time):

```bash
sudo apt-get install -y build-essential git rsync cpio bc unzip file wget python3
buildroot/build.sh                       # latest release (.last-built-version), avx2 variant
MANTIS_VARIANT=baseline buildroot/build.sh   # avx512 | avx2 | avx | baseline
```

Output: `buildroot/output/images/disk.img` and `disk.img.gz`.

`build.sh` clones Buildroot (LTS, pinned in the script) into `buildroot/.br-src`,
applies [`configs/mantis_x86_64_defconfig`](configs/mantis_x86_64_defconfig), sets
the release sha / variant, and runs `make`. Extra arguments go to `make`,
e.g. `buildroot/build.sh menuconfig` or `buildroot/build.sh linux-menuconfig`.

## Install

```bash
gunzip -c disk.img.gz | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
```

Boot it (UEFI). Log in as `root` on the console (tty1 or serial ttyS0 115200).
The image has no root password: the first login makes you choose one (it is
kept on the data partition, across upgrades). Then run:

```bash
mantis-setup
```

It asks the same questions as `deploy.sh` (ingress/egress NIC, queues, XDP mode,
GRO/VLAN, UMEM/hugepages, CPU sets, port, home_net), plus the management NIC
(DHCP, static or WiFi), the fail mode, time zone and NTP servers, an SSH key,
whether SSH accepts passwords and which networks may manage the box. It writes
`/opt/mantis/config.toml` and `/opt/mantis/appliance.conf` and starts Mantis.
Until then every wired NIC tries DHCP, so the box can also be reached over SSH.

The web UI is then at **`https://<management address>/`** (self-signed
certificate, see below); the plain HTTP port is only reachable from the box
itself.

The management NIC can be **WiFi**: `mantis-setup` lists the nearby networks,
asks for the SSID, password (WPA2 and WPA3-SAE) and country code, and writes
`/opt/mantis/wpa_supplicant.conf` (mode 600, kept across upgrades). At boot
`S41mgmt-net` waits for the interface, starts `wpa_supplicant` and then runs
DHCP or the static address. Ingress and egress must stay wired. Check the link
with `wpa_cli -i <nic> status`.

| Task | Command |
|---|---|
| Service | `/etc/init.d/S90mantis {start\|stop\|restart\|status}` |
| Log | `tail -f /opt/mantis/state/logs/console.log` |
| Re-configure | `mantis-setup` |

`S90mantis` applies the NIC settings (`ethtool -L/-K`, hugepages) at every boot,
like `mantis-nic.service`, and supervises Mantis: it restarts it after a crash
(5 s, backing off to 5 min while it keeps failing to come up) and kills it when
its web UI stops answering for 30 s (hung).

### Fail-open

Same design as `deploy.sh` on Ubuntu. With **fail mode `open`** (the default,
asked by `mantis-setup`, `BYPASS=` in `appliance.conf`) the ingress and egress
NICs are permanent ports of a Linux bridge, `br-mantis` (STP off, no address),
created from early boot (`S21bypass`).

- **Mantis running:** its XDP program runs in the NIC driver, before the bridge,
  and forwards every frame itself (DEVMAP, AF_XDP); the bridge carries nothing.
- **Mantis not running** (boot, stopped, restarting, crashed): no XDP program,
  so the bridge forwards, **uninspected**. A crash needs no action: the kernel
  detaches XDP when the process exits.
- **Mantis hung:** its XDP program stays attached. ICMP and ARP still cross
  (DEVMAP runs in the kernel) but TCP/UDP go to the frozen process over AF_XDP
  and stall. The supervisor kills it after 30 s without a web UI answer and
  detaches XDP, so the bridge forwards until the restarted Mantis is up.
- **Fail mode `closed`:** no bridge, nothing crosses while Mantis is down.

The bridge forwards link-local frames too (`group_fwd_mask 0xfff8`: LLDP,
802.1X), like a cable. The kernel has no `br_netfilter`, so no iptables rule is
needed (deploy.sh adds one on Ubuntu). `/etc/init.d/S90mantis status` shows
whether traffic goes through Mantis or the bridge.
A power loss or kernel crash still cuts the link: only a NIC with a hardware
bypass relay (e.g. Silicom i350 bypass cards) covers that.

### Differences from an Ubuntu install

- bpffs (`/sys/fs/bpf`, where Mantis pins its XSK maps) and cgroup2 are mounted
  from `/etc/fstab`; systemd does this on Ubuntu.
- `/opt/mantis/mantis` and `share/` are read-only bind mounts of the release in
  the rootfs, so Mantis (which looks for `config.toml` next to its executable)
  sees the same layout as a `deploy.sh` install.
- The release links against Ubuntu sonames; `libpcap.so.0.8`, `libnet.so.1` and
  `libhiredis.so.1.1.0` are symlinks to the upstream builds.

### Security

`S22security` applies, at every boot, what `mantis-setup` stored in `appliance.conf`:

| | |
|---|---|
| Console | no default password: the first login must set one |
| Firewall (`FIREWALL=on`) | nftables, input policy drop: only SSH (22) and HTTPS (`HTTPS_PORT`, 443) from outside, optionally only from `MGMT_ALLOW` networks; ICMP and DHCP replies allowed. Inspected and bridged traffic is not affected (XDP and the bridge run before netfilter, no `br_netfilter`) |
| Web UI | stunnel terminates TLS on 443 and forwards to Mantis on 127.0.0.1. Mantis has no TLS of its own. The certificate is self-signed on first boot in `/opt/mantis/tls/mantis.{crt,key}`; put your own there and run `/etc/init.d/S22security restart`. `WEB_HTTP=1` opens the plain port too |
| SSH | keys in `/opt/mantis/ssh/authorized_keys`; `SSH_PASSWORD=no` (default once a key is added) disables password logins. Host keys live in `/opt/mantis/ssh/dropbear`, so they do not change after an upgrade |

### Time

`S42ntp` runs BusyBox `ntpd` with `NTP_SERVERS` (default `pool.ntp.org`,
`off` to disable) and writes the hardware clock once synced, so the time is
right after a reboot without network. `TIMEZONE` sets `/etc/localtime`.
Alert times, login token expiry and the per-day record files depend on it.

### Log rotation and disk guard

`mantis-logrotate` runs every 10 minutes (crond):

- `state/logs/console.log` is rotated above `LOG_MAX_MB` (50) into
  `console.log.1.gz` ... `.7.gz` (`LOG_KEEP`), by copy and truncate, so Mantis
  keeps writing without a restart;
- Mantis' daily logs `state/logs/Mantis.*` older than `LOG_DAYS` (30) are deleted;
- above `DISK_MAX_PCT` (90 %) data partition usage, the oldest rotated logs,
  daily logs and then per-day traffic records are deleted until usage is 5 %
  lower. `app.db`, `config.toml` and today's files are never deleted; every
  deletion is logged in `console.log`.

## Build-time checks

The build fails instead of producing a broken image when:

- the kernel lacks a required option (including the WiFi stack and drivers) (`post-image.sh`: eBPF/AF_XDP/BTF, igb,
  r8169, … must be `=y`; the firmware-loading NIC drivers must be present);
- any shared library of `mantis` or of an ELF embedded in it (Suricata, ONNX
  Runtime, the Suricata capture plugin) is missing, or any of their symbols does
  not bind with `LD_BIND_NOW` (`check-needed.py`, run from `post-build.sh`);
- `libhs.so.5` is missing one of its fat-runtime CPU variants (`vectorscan.mk`).

`post-build.sh` also prunes the Intel WiFi firmware to the one file per chip the
kernel's iwlwifi loads (`prune-iwlwifi.py`, reading its `MODULE_FIRMWARE` list):
linux-firmware installs every API version, about 130 MiB, which becomes 24 MiB.

## Test in QEMU

```bash
sudo apt-get install -y qemu-system-x86 ovmf python3-pexpect openssh-client
buildroot/test/qemu-boot-test.py buildroot/output/images/disk.img /tmp/mantis-qemu
```

Boots the image with UEFI, two emulated Intel 82576 NICs (igb driver) and a
virtio management NIC, and checks (54 checks):

- first boot: forced root password, data partition growth, drivers, firmware,
  libraries; `mantis-setup` with scripted answers; XDP attach, Suricata;
- security: web UI over HTTPS, plain HTTP and other ports blocked from outside,
  SSH key login, password logins off, host keys and certificate on the data
  partition, time zone and ntpd;
- log rotation (copy + truncate, writers keep appending) and the disk guard on
  a partition filled past 90 %;
- traffic across the box (two extra NICs in network namespaces): ICMP and TCP
  through Mantis, through the fail-open bridge on stop, crash and hang, across a
  restart (with a timeline of any TCP request that failed) and a reboot;
- A/B upgrades: a good image (trial, confirmed, data kept), an image that is
  not a Mantis rootfs (refused), a broken one (no confirmation, automatic
  reboot and rollback, app.db restored) and one without a kernel (GRUB fallback);
- WiFi: `mac80211_hwsim`, a WPA2 access point on one simulated radio, and
  `mantis-setup` making the other one the management NIC.

Without KVM it takes about 35 minutes, with KVM a few.

## CI

[`.github/workflows/build-appliance.yml`](../.github/workflows/build-appliance.yml)
runs after every release made by `build-mantis.yml` (and can be started by hand
with a release sha):

1. builds the **baseline** image, then tests it with `qemu-boot-test.py` (KVM on
   the runner); a failed check stops the job and nothing is uploaded;
2. builds the **avx**, **avx2** and **avx512** images (only the mantis package
   and the image are rebuilt, a few minutes each);
3. uploads them to the same `build-<sha>` release, with a SHA256SUMS file:
   `mantis-appliance-<version>-linux-amd64-<variant>-<sha>.img.gz`.

Buildroot downloads and ccache are cached between runs; build and QEMU logs are
kept as the `appliance-logs` artifact for 7 days.

## Upgrade (A/B)

Two root slots: the running one is never written. An upgrade goes into the
other slot, the next boot tries it once, and it only becomes the system once
Mantis is up on it; otherwise the box goes back to the previous slot by itself.

```bash
mantis-upgrade status                     # slots, versions, last result
mantis-upgrade latest                     # newest release of this variant from GitHub, then reboot
mantis-upgrade latest --tag build-<sha>   # a given release (also to go back)
mantis-upgrade install mantis-rootfs-...ext4.gz --sha256 <hex>   # offline: a file (USB) or a URL
```

1. `mantis-upgrade` downloads `mantis-rootfs-<version>-<variant>-<sha>.ext4.gz`
   (CI attaches one per variant to every release), checks it against the
   release's `SHA256SUMS`, writes it to the inactive slot and reads it back
   byte for byte. It refuses an image without a kernel, Mantis and
   `/etc/mantis-release`, and copies `app.db` and `config.toml` aside.
2. It sets `try_slot` in `grubenv` and reboots. `grub.cfg` boots the new slot
   once (`try_count=1`, `mantis.trial=1`) and `S05trialdog` arms the software
   watchdog (15 min) as a deadline.
3. `S99upgrade` waits until Mantis answers on its web UI (or just the end of the
   boot when Mantis is not configured), then commits the slot (`slot=` in
   `grubenv`) and disarms the watchdog.
4. Not healthy within `UPGRADE_CONFIRM_SECS` (600 s): reboot. A panic
   (`panic=10`) or a hang (watchdog) also reboots. GRUB then finds an
   unconfirmed trial and boots the previous slot; if the new kernel cannot even
   be loaded, GRUB's fallback entry does the same. `S99upgrade` restores
   `app.db` and `config.toml` from before the trial and records the failure
   (`mantis-upgrade status`, `state/logs/upgrade.log`).

The data partition is shared by both slots, so settings, keys, certificate,
database and records are the same before and after. While the new slot is
written Mantis keeps running; during the reboot the fail-open bridge forwards.

Machines installed before A/B (one root partition) need one reinstall from
`disk.img`: copy `/opt/mantis/config.toml`, `appliance.conf`, `state/app.db`,
`ssh/` and `tls/` off the box first and back afterwards.

## Limits

- UEFI only (no legacy BIOS boot yet).
- x86_64 only; an aarch64 defconfig needs its own board files.
- The rootfs is writable, but anything outside `/opt/mantis` (e.g. SSH host keys)
  is lost when partition 2 is replaced.
- virtio NICs in a VM keep kernel names (`eth0`): eudev 3.2 has no path name for
  them. PCI NICs get the same names as on Ubuntu (`enp4s0f0`).
- Tested in QEMU only (emulated igb, generic XDP). Native XDP / zero-copy on a
  real i350 and the Realtek / Broadcom drivers still need a run on hardware.
