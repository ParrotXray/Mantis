#!/usr/bin/env python3
"""Boot the Mantis Buildroot image in QEMU and check it end to end.

UEFI (OVMF), two emulated Intel 82576 NICs (igb driver) as ingress/egress,
a virtio management NIC with the web UI forwarded to 127.0.0.1:18080.
Runs mantis-setup with scripted answers, then checks that Mantis, XDP,
Suricata and the web UI come up.

usage: qemu-boot-test.py <disk.img> <workdir>
needs: qemu-system-x86 (>= 8.0, for -device igb), ovmf, python3-pexpect
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pexpect

img_src, work = Path(sys.argv[1]), Path(sys.argv[2])
work.mkdir(parents=True, exist_ok=True)
img = work / "test.img"
shutil.copy(img_src, img)
subprocess.run(["truncate", "-s", "3G", str(img)], check=True)  # first boot grows partition 3
vars_fd = work / "OVMF_VARS.fd"
shutil.copy("/usr/share/OVMF/OVMF_VARS_4M.fd", vars_fd)

MGMT_MAC, LEFT_MAC, RIGHT_MAC = "52:54:00:00:00:10", "52:54:00:00:00:a1", "52:54:00:00:00:a2"

# KVM when the host offers it (GitHub runners do), else TCG emulation
accel = ["-accel", "kvm", "-cpu", "host"] if os.access("/dev/kvm", os.R_OK | os.W_OK) else ["-cpu", "max"]
qemu = [
    "qemu-system-x86_64", "-machine", "q35", *accel, "-smp", "4", "-m", "4096",
    "-nographic",
    "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
    "-drive", f"if=pflash,format=raw,file={vars_fd}",
    "-drive", f"file={img},format=raw,if=virtio",
    # management NIC: SSH and web UI forwarded to the host
    "-netdev", "user,id=mgmt,hostfwd=tcp:127.0.0.1:18080-:8080,hostfwd=tcp:127.0.0.1:12222-:22,"
    "hostfwd=tcp:127.0.0.1:18443-:443,hostfwd=tcp:127.0.0.1:19999-:9999",
    "-device", f"virtio-net-pci,netdev=mgmt,mac={MGMT_MAC}",
    # two Intel 82576 (igb driver) NICs as ingress / egress, each wired (QEMU
    # hub) to a virtio NIC that the guest moves into its own namespace:
    #   ns left (10.50.0.1) -- ingress [Mantis or bypass bridge] egress -- ns right (10.50.0.2)
    "-netdev", "hubport,id=in0,hubid=1", "-device", "igb,netdev=in0",
    "-netdev", "hubport,id=out0,hubid=2", "-device", "igb,netdev=out0",
    "-netdev", "hubport,id=lhost,hubid=1", "-device", f"virtio-net-pci,netdev=lhost,mac={LEFT_MAC}",
    "-netdev", "hubport,id=rhost,hubid=2", "-device", f"virtio-net-pci,netdev=rhost,mac={RIGHT_MAC}",
]
log = open(work / "console.log", "w")
child = pexpect.spawn(qemu[0], qemu[1:], encoding="utf-8", codec_errors="replace", timeout=900)
# never leave the VM running (it would hold the forwarded ports), even on a crash
atexit.register(lambda: child.isalive() and child.terminate(force=True))
child.logfile_read = log
PROMPT = "MANTISPROMPT# "
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


_seq = [0]


def run(cmd, timeout=120):
    """Run a shell command in the VM and return its output. Each command
    ends with a unique marker, so a stray prompt (e.g. a job notice) can
    never shift the output of later commands."""
    _seq[0] += 1
    marker = f"__DONE_{_seq[0]}__"
    sep = " " if cmd.rstrip().endswith("&") else "; "
    child.sendline(f"{cmd}{sep}echo {marker}")
    child.expect_exact(marker, timeout=timeout)
    out = child.before
    child.expect_exact(PROMPT, timeout=30)
    return out.replace("\r", "").replace(PROMPT, "").strip()


ROOT_PW = "Mantis-test-pw1"


def login():
    """log in as root on the serial console; returns how the password went"""
    child.sendline("root")
    how = "none"
    while True:
        i = child.expect([r"New password:", r"Retype password:", r"Password:", r"# "], timeout=120)
        if i in (0, 1):
            how = "forced to set one"
            child.sendline(ROOT_PW)
        elif i == 2:
            how = "asked"
            child.sendline(ROOT_PW)
        else:
            break
    # quoted so the echoed command does not contain the marker itself
    child.sendline("stty -echo cols 250; export PS1='MANTIS''PROMPT# '")
    child.expect_exact(PROMPT)
    return how


t0 = time.time()
child.expect("mantis login:", timeout=900)
check("boots to login prompt", True, f"{time.time() - t0:.0f} s ({'KVM' if 'kvm' in accel else 'TCG emulation'})")
how = login()
check("first login forces a root password", how == "forced to set one", how)

out = run("uname -r; grep -c . /proc/cpuinfo >/dev/null; mountpoint /opt/mantis; df -h /opt/mantis | tail -1")
check("data partition mounted on /opt/mantis", "is a mountpoint" in out, out.splitlines()[-1] if out else "")
size = run("df -m /opt/mantis | awk 'NR==2 {print $2}'")
check("data partition grown to the disk", int(size or 0) > 2000, size + " MiB")

out = run("grep ' /opt/mantis/' /proc/mounts | awk '{print $2, $4}'")
check("release bind-mounted into /opt/mantis", "/opt/mantis/mantis" in out and "/opt/mantis/share" in out, out.replace("\n", " | "))

out = run("ls /sys/kernel/btf/vmlinux && echo BTF_OK")
check("kernel BTF present", "BTF_OK" in out)

nics = {}
for line in run("for n in /sys/class/net/*; do b=$(basename $n); [ -e $n/device ] && echo \"$b $(basename $(readlink $n/device/driver))\"; done").splitlines():
    parts = line.split()
    if len(parts) == 2:
        nics[parts[0]] = parts[1]
igb = sorted(n for n, d in nics.items() if d == "igb")
macs = {}
for line in run("for n in /sys/class/net/*; do echo \"$(basename $n) $(cat $n/address)\"; done").splitlines():
    if len(line.split()) == 2:
        macs[line.split()[1]] = line.split()[0]
mgmt = [macs[MGMT_MAC]]
left_if, right_if = macs[LEFT_MAC], macs[RIGHT_MAC]
for ns, nic, ip in (("left", left_if, "10.50.0.1"), ("right", right_if, "10.50.0.2")):
    run(f"ip netns add {ns}; ip link set {nic} netns {ns}; "
        f"ip netns exec {ns} sh -c 'ip link set lo up; ip link set {nic} up; ip addr add {ip}/24 dev {nic}'")
nics = {n: d for n, d in nics.items() if n not in (left_if, right_if)}


def wire_ping(count=3, need=None):
    """ping from ns left to ns right, across ingress -> egress; ok when at
    least `need` replies came back (default: all of them)"""
    out = run(f"ip netns exec left ping -c {count} -W 2 10.50.0.2 | tail -2", timeout=60)
    m = re.search(r"(\d+) packets received", out)
    got = int(m.group(1)) if m else 0
    return got >= (count if need is None else need), out.replace("\n", " | ")


def wait_for(cmd, needle, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        out = run(cmd, timeout=60)
        if needle in out:
            return True, out
        time.sleep(2)
    return False, out
check("igb driver bound to both Intel NICs", len(igb) == 2, str(nics))
pci = [n for n, d in nics.items() if not d.startswith("virtio")]  # eudev 3.2 has no path name for virtio
check("predictable names for PCI NICs (eudev)", all(n.startswith("en") for n in pci), " ".join(nics))

out = run("ethtool -i %s | head -3; ethtool -l %s | grep -A4 maximums | grep Combined" % (igb[0], igb[0]))
check("ethtool reads igb", "driver: igb" in out, out.replace("\n", " | "))

out = run("ls /lib/firmware/rtl_nic | wc -l; ls /lib/firmware/intel/ice/ddp; find /lib/modules -name r8152.ko* | grep -q . && echo R8152_MOD")
check("Realtek / ice firmware and r8152 module present", "R8152_MOD" in out, out.replace("\n", " | "))

out = run("/lib64/ld-linux-x86-64.so.2 --list /usr/share/mantis-dist/mantis; echo RC=$?")
check("dynamic loader resolves mantis", "RC=0" in out and "not found" not in out, out.replace("\n", " | ")[:300])

out = run(f"ip -4 addr show {mgmt[0]} | grep inet")
check("management NIC got DHCP before setup", "inet " in out, out)

# --- mantis-setup with scripted answers (empty = default) ---
key_file = work / "id_ed25519"
if not key_file.exists():
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)], check=True)
pubkey = (work / "id_ed25519.pub").read_text().strip()
run("awk -F: '$1==\"root\"{print $2}' /etc/shadow > /opt/mantis/appliance.shadow")  # skip passwd prompt
answers = [
    mgmt[0], "dhcp",          # management NIC, mode
    igb[0], igb[1],           # ingress, egress
    "1",                      # combined queues
    "generic",                # xdp_mode (QEMU's igb has no native XDP fast path)
    "y", "n",                 # GRO off, VLAN
    "open",                   # fail mode
    "", "4096",               # frame_size, frame_count (small for the VM)
    "",                       # hugepages
    "", "", "", "", "",       # xsk start/end, ml_cpu, port, refresh
    "10.0.2.0/24",            # home_net
    "", "", "",               # suricata workers start/end, management cpu
    "Asia/Taipei",            # time zone
    "",                       # NTP servers (pool.ntp.org)
    pubkey,                   # SSH key
    "n",                      # no SSH password logins
    "",                       # management networks allowed (any)
    "n",                      # change root password
    "y",                      # apply now
]
setup_out = run("printf '%s\\n' " + " ".join(f"'{a}'" for a in answers) + " | mantis-setup 2>&1 | tail -15",
                timeout=300)
check("mantis-setup completes", "Mantis running" in setup_out, setup_out.strip().splitlines()[-1] if setup_out.strip() else "")
print(setup_out)

import ssl  # noqa: E402
_insecure = ssl.create_default_context()
_insecure.check_hostname = False
_insecure.verify_mode = ssl.CERT_NONE
# Give Mantis time to load eBPF, Suricata rules and the ONNX model (TCG is slow)
deadline = time.time() + 600
web_ok = False
while time.time() < deadline:
    try:
        with urllib.request.urlopen("https://127.0.0.1:18443/", timeout=5, context=_insecure) as r:
            web_ok = r.status < 500
            body = r.read(200)
            break
    except Exception as e:  # noqa: BLE001
        last = e
        time.sleep(10)
check("web UI over HTTPS (stunnel, self-signed certificate)", web_ok, "HTTPS OK" if web_ok else repr(last))

out = run("mountpoint /sys/fs/bpf; ls /sys/fs/bpf | head; /etc/init.d/S90mantis status; echo exe=$(readlink /proc/$(pidof mantis)/exe); ip link show %s | head -1; ip link show %s | grep -o 'xdp[a-z]*' | head -1" % (igb[0], igb[0]))
check("Mantis running, XDP attached to ingress", "running" in out and "xdp" in out, out.replace("\n", " | "))
time.sleep(30)
out = run("grep -cE 'Suricata .*exited' /opt/mantis/state/logs/console.log; pidof Suricata-Main || ps | grep -ic '[s]uricata'")
lines = out.splitlines()
check("embedded Suricata stays up", bool(lines) and lines[0] == "0", out.replace("\n", " | "))
print("---- console.log (last 40 lines) ----")
print(run("tail -40 /opt/mantis/state/logs/console.log", timeout=30))

# --- Security, time, log rotation -------------------------------------------
import socket  # noqa: E402


def host_reach(port, payload=b"GET / HTTP/1.0\r\n\r\n"):
    """does the guest answer on a forwarded port? (slirp accepts on the host
    side either way, so only data coming back counts)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.settimeout(8)
            c.sendall(payload)
            return bool(c.recv(64))
    except OSError:
        return False


out = run("nft list table inet mantis | grep -E 'policy drop|dport' | tr -s ' ' | tr '\\n' ';'")
check("management firewall loaded (input policy drop, 22 and 443 open)",
      "policy drop" in out and "22" in out and "443" in out, out)
check("plain HTTP web UI (8080) blocked from outside", not host_reach(18080), "port 8080 from the host")
run("(nc -l -p 9999 -e echo open >/dev/null 2>&1 &)")
time.sleep(2)
check("other ports blocked from outside (nc listening on 9999)", not host_reach(19999), "port 9999 from the host")
run("for p in $(pidof nc); do kill $p; done")

out = run("openssl x509 -in /opt/mantis/tls/mantis.crt -noout -subject -enddate; ls -l /opt/mantis/tls/mantis.key | cut -c1-10")
check("web UI certificate generated on the data partition", "CN" in out and out.strip().endswith("-rw-------"),
      out.replace("\n", " | "))

ssh = ["ssh", "-i", str(key_file), "-p", "12222", "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
r = subprocess.run(ssh + ["root@127.0.0.1", "echo ssh-key-ok"], capture_output=True, text=True, timeout=90)
check("SSH login with the key from mantis-setup", "ssh-key-ok" in r.stdout, (r.stdout + r.stderr).strip()[-160:])
r = subprocess.run(["ssh", "-p", "12222", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "root@127.0.0.1", "true"],
                   capture_output=True, text=True, timeout=90)
out = run("ps | grep '[d]ropbear'; ls /opt/mantis/ssh/dropbear/ | tr '\\n' ' '")
check("SSH password logins off, host keys kept on the data partition",
      r.returncode != 0 and " -s" in out and "host_key" in out, out.replace("\n", " | ")[:200])

out = run("date +%Z; readlink /etc/localtime; ps | grep '[n]tpd'")
check("time zone and NTP (Asia/Taipei, ntpd -p pool.ntp.org)",
      out.startswith("CST") and "Asia/Taipei" in out and "pool.ntp.org" in out, out.replace("\n", " | "))
synced = run("cat /run/ntp-synced 2>/dev/null || echo not-synced")
print(f"[INFO] NTP sync: {synced} (needs outbound UDP 123 from the build host)")

# log rotation: blow console.log past LOG_MAX_MB (5 MiB for the test), rotate,
# Mantis keeps writing
LOGF = "/opt/mantis/state/logs/console.log"
run(f"dd if=/dev/zero bs=1M count=8 2>/dev/null >> {LOGF}", timeout=120)
run("LOG_MAX_MB=5 /usr/sbin/mantis-logrotate", timeout=300)
out = run(f"wc -c < {LOGF}; ls /opt/mantis/state/logs/ | tr '\\n' ' '; grep -c 'console.log rotated' {LOGF}")
# BusyBox wc pads the number
lines = [line.strip() for line in out.splitlines()]
small = bool(lines) and lines[0].isdigit() and int(lines[0]) < 1024 * 1024
check("console.log rotated past LOG_MAX_MB (copy + truncate, gzip)",
      small and "console.log.1.gz" in out and out.split()[-1] == "1", out.replace("\n", " | "))
run("kill -9 $(pidof mantis)")
time.sleep(10)
out = run(f"echo $(wc -c < {LOGF}) $(du -k {LOGF} | cut -f1) $(grep -c 'mantis exited' {LOGF}) "
          f"$(grep -c 'starting mantis' {LOGF})")
n = [int(x) for x in out.split()[:4]] if len(out.split()) >= 4 else [0, 0, 0, 0]
check("writers keep appending to the rotated console.log (no sparse file)",
      n[0] < 5 * 1024 * 1024 and n[1] < 5 * 1024 and n[2] >= 1 and n[3] >= 1,
      f"{n[0]} bytes, {n[1]} KiB on disk; after rotation: {n[2]} 'exited', {n[3]} 'starting' lines")

# disk guard: really fill the data partition past DISK_MAX_PCT (90 %) with a
# big oldest record file; small newer ones must survive (it stops once below)
size_mb = int(run("df -m /opt/mantis | awk 'NR==2 {print $2}'") or 0)
run("cd /opt/mantis/state/records && for d in 2026-01-01 2026-01-02; do "
    "dd if=/dev/zero of=traffic_log-$d.csv bs=1M count=2 2>/dev/null; touch -d \"$d 12:00\" traffic_log-$d.csv; done; "
    f"dd if=/dev/zero of=traffic_log-2025-12-31.csv bs=1M count={int(size_mb * 0.93)} 2>/dev/null; "
    "touch -d '2025-12-31 12:00' traffic_log-2025-12-31.csv; cd /", timeout=900)
before = run("df -P /opt/mantis | awk 'NR==2 {print $5}'")
run("/usr/sbin/mantis-logrotate", timeout=300)
after = run("df -P /opt/mantis | awk 'NR==2 {print $5}'")
out = run(f"ls /opt/mantis/state/records/ | tr '\\n' ' '; ls /opt/mantis/state/app.db; grep 'disk guard' {LOGF} | head -3 | cut -c1-90")
recs = out.splitlines()[0] if out else ""
check("disk guard above 90 %: deletes the oldest record, stops once below, keeps app.db",
      "2025-12-31" not in recs and "2026-01-01" in recs and "app.db" in out and "disk guard: deleted" in out,
      f"usage {before} -> {after} | " + out.replace("\n", " | ")[:260])
wait_for("wget -q -T 3 -O /dev/null http://127.0.0.1:8080/ && echo READY", "READY", 180)

# --- Forwarding and fail-open (permanent bridge br-mantis) ---------------
STATUS = "/etc/init.d/S90mantis status"
LOG = "/opt/mantis/state/logs/console.log"
# a TCP service on the far side, reached across the box (AF_XDP path)
run("ip netns exec right sh -c 'mkdir -p /tmp/www && echo mantis-tcp-ok > /tmp/www/t "
    "&& httpd -p 10.50.0.2:8088 -h /tmp/www'")


def wire_tcp():
    out = run("ip netns exec left wget -q -T 3 -O - http://10.50.0.2:8088/t 2>&1", timeout=30)
    return "mantis-tcp-ok" in out, out


def wait_ready(seconds=180):
    """the restarted Mantis serves its web UI (it starts it after init)"""
    return wait_for("wget -q -T 3 -O /dev/null http://127.0.0.1:8080/ && echo READY", "READY", seconds)[0]


out = run("cat /sys/class/net/br-mantis/bridge/group_fwd_mask; ls /sys/class/net/br-mantis/brif | tr '\\n' ' '")
check("fail-open bridge br-mantis holds ingress and egress, link-local frames forwarded",
      igb[0] in out and igb[1] in out and "0xfff8" in out, out.replace("\n", " | "))

out = run(STATUS)
p = run("ip netns exec left ping -c 10 -W 2 10.50.0.2 | grep -cE 'DUP|bytes from'; "
        "ip netns exec left ping -c 10 -W 2 10.50.0.2 | grep -c DUP", timeout=60).split()
tcp, tcp_out = wire_tcp()
check("Mantis forwards (XDP before the bridge): ICMP, no duplicates, TCP via AF_XDP",
      "XDP attached" in out and len(p) == 2 and p[0] == "10" and p[1] == "0" and tcp,
      f"{out} | replies={p} | tcp={tcp_out}")

out = run("/etc/init.d/S90mantis stop", timeout=90)
ok, detail = wire_ping()
tcp, _ = wire_tcp()
check("Mantis stopped -> bridge forwards ICMP and TCP", ok and tcp and "fail-open" in out, f"{out} | {detail}")

run("/etc/init.d/S90mantis start", timeout=90)
up = wait_ready()
ok, detail = wire_ping()
tcp, _ = wire_tcp()
check("Mantis started again: XDP takes over, traffic flows", up and ok and tcp and "XDP attached" in run(STATUS), detail)

# across a restart: ICMP once a second, and TCP requests once a second
# (this shows whether TCP is lost between XDP attach and the AF_XDP sockets)
run("(ip netns exec left ping -c 45 -W 1 10.50.0.2 > /tmp/ping-restart.log 2>&1 &)")
run("(i=0; while [ $i -lt 45 ]; do ip netns exec left wget -q -T 1 -O - http://10.50.0.2:8088/t "
    ">/dev/null 2>&1 && echo \"$(date -u +%T) ok\" || echo \"$(date -u +%T) fail\"; i=$((i+1)); sleep 1; "
    "done > /tmp/tcp-restart.log &)")
run(f"echo '=== TEST RESTART MARKER' >> {LOG}")
run("/etc/init.d/S90mantis restart", timeout=90)
wait_ready()
wait_for("grep -c 'packets received' /tmp/ping-restart.log", "1", 90)
wait_for("wc -l < /tmp/tcp-restart.log", "45", 120)
out = run("tail -2 /tmp/ping-restart.log | head -1; grep -c DUP /tmp/ping-restart.log; "
          "grep -c fail /tmp/tcp-restart.log")
m = re.search(r"(\d+) packets transmitted, (\d+) packets received", out)
lost = int(m.group(1)) - int(m.group(2)) if m else -1
tail = out.splitlines()
dups = int(tail[-2]) if len(tail) >= 2 and tail[-2].isdigit() else -1
tcp_fail = int(tail[-1]) if tail and tail[-1].isdigit() else -1
check("restart: ICMP without a gap (<= 2 of 45 lost, no duplicates)", 0 <= lost <= 2 and dups == 0,
      out.replace("\n", " | "))
# Not a pass/fail check: TCP needs Mantis' AF_XDP sockets, so a short window
# while Mantis stops / starts depends on the Mantis binary, not the image.
# Report when the failures happened against Mantis' own events, for upstream.
fails = run("grep fail /tmp/tcp-restart.log | cut -d' ' -f1 | tr '\\n' ' '")
raw = run(f"awk '/TEST RESTART MARKER/ {{f = 1}} f' {LOG} | "
          "grep -E 'stopping mantis|Termination|starting mantis|XDP attached|Queue pair .* started|ready after' "
          "| head -12")
events = []
for line in raw.splitlines():
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    # Mantis: 2026-11-13T03:56:05.583850Z  INFO ThreadId(01) src/...: text  (UTC)
    line = re.sub(r"^\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)\.\d+Z\s+\w+\s+ThreadId\(\d+\)\s+\S+\s+", r"\1 UTC ", line)
    # supervisor: === 2026-11-13 12:43:31 text  (local time)
    line = re.sub(r"^=== \d{4}-\d\d-\d\d (\d\d:\d\d:\d\d) ", r"\1 local ", line)
    events.append(line)
print(f"[INFO] restart: {tcp_fail} of 45 TCP requests failed (1/s, 1 s timeout) at (UTC): {fails or '-'}")
print("[INFO] events during the restart:\n       " + "\n       ".join(events or ["(none found)"]))

# crash: the kernel detaches XDP with the process, the bridge forwards at once
run("kill -9 $(pidof mantis)")
ok, detail = wire_ping(need=2)
check("crash (kill -9): bridge forwards right away", ok and "XDP attached" not in run(STATUS), detail)
check("supervisor restarts Mantis after the crash", wait_ready() and "XDP attached" in run(STATUS),
      run(f"grep -E 'exited|ready after' {LOG} | tail -2"))

# hang: SIGSTOP keeps the XDP program attached with nobody behind it.
# ICMP/ARP still cross (DEVMAP, all in the kernel); TCP/UDP go to the frozen
# process over AF_XDP and stall until the supervisor kills it.
run("kill -STOP $(pidof mantis)")
time.sleep(3)
tcp_during, _ = wire_tcp()
hung, _ = wait_for(f"grep -c 'not answering' {LOG}", "1", 150)
time.sleep(2)
tcp_after, _ = wire_tcp()
ok, detail = wire_ping(need=2)
check("hang: TCP stalls, supervisor kills Mantis after 30 s, bridge forwards TCP and ICMP",
      not tcp_during and hung and tcp_after and ok,
      f"TCP during hang: {'flowing' if tcp_during else 'stalled'}, after kill: "
      f"{'flowing' if tcp_after else 'stalled'} | {detail}")
check("Mantis restarted after the hang", wait_ready(), run(STATUS))

# fail-closed when asked to
run("sed -i 's/^BYPASS=.*/BYPASS=\"closed\"/' /opt/mantis/appliance.conf; /etc/init.d/S90mantis stop", timeout=90)
ok, detail = wire_ping(2, need=1)
check("BYPASS=closed: Mantis stopped -> no bridge, no traffic",
      not ok and "no bridge" in run(STATUS), detail)
run("sed -i 's/^BYPASS=.*/BYPASS=\"open\"/' /opt/mantis/appliance.conf; /etc/init.d/S90mantis start", timeout=90)
wait_ready()

# early boot: S21bypass bridges before S90mantis starts Mantis
run(f"echo '=== TEST REBOOT MARKER' >> {LOG}")
child.sendline("reboot")
child.expect("mantis login:", timeout=900)
how = login()
check("after reboot the console asks for the root password", how == "asked", how)
for ns, nic, ip in (("left", left_if, "10.50.0.1"), ("right", right_if, "10.50.0.2")):
    run(f"ip netns add {ns}; ip link set {nic} netns {ns}; "
        f"ip netns exec {ns} sh -c 'ip link set lo up; ip link set {nic} up; ip addr add {ip}/24 dev {nic}'")
up = wait_ready()
# the supervisor logs "ready after" on its next poll once the web UI answers
wait_for(f"sed -n '/TEST REBOOT MARKER/,$p' {LOG} | grep -c 'ready after'", "1", 30)
out = run(f"sed -n '/TEST REBOOT MARKER/,$p' {LOG} | grep -E 'boot: fail-open|starting mantis|ready after' | head -3")
lines = out.splitlines()
check("fail-open from early boot: bridge up before Mantis starts",
      len(lines) >= 3 and "boot: fail-open" in lines[0] and "starting mantis" in lines[1]
      and "ready after" in lines[2], out.replace("\n", " | "))
ok, detail = wire_ping()
check("after reboot: Mantis forwards, traffic flows", up and ok and "XDP attached" in run(STATUS), detail)

# --- A/B upgrades --------------------------------------------------------------
# Images served from the host (QEMU user network: 10.0.2.2) to mantis-upgrade:
#   good:    the rootfs.ext4 of this build
#   broken:  the same without the Mantis binary: boots, never confirms -> rollback
#   nokern:  without /boot/bzImage: GRUB cannot load it -> fallback entry
#   foreign: an ext4 image that is not a Mantis rootfs -> refused
import hashlib  # noqa: E402
import gzip  # noqa: E402

images = img_src.parent
srv = work / "upgrade-srv"
srv.mkdir(exist_ok=True)
debugfs = shutil.which("debugfs") or "/sbin/debugfs"


def make_image(name, remove=None):
    raw = srv / f"{name}.ext4"
    shutil.copy(images / "rootfs.ext4", raw)
    if remove:
        subprocess.run([debugfs, "-w", "-R", f"rm {remove}", str(raw)], check=True, capture_output=True)
    with open(raw, "rb") as f, gzip.open(srv / f"{name}.ext4.gz", "wb", compresslevel=1) as g:
        shutil.copyfileobj(f, g)
    raw.unlink()
    return hashlib.sha256((srv / f"{name}.ext4.gz").read_bytes()).hexdigest()


sha_good = make_image("good")
sha_broken = make_image("broken", "/usr/share/mantis-dist/mantis")
sha_nokern = make_image("nokern", "/boot/bzImage")
shutil.copy(images / "data.ext4", srv / "foreign.ext4")
sha_foreign = hashlib.sha256((srv / "foreign.ext4").read_bytes()).hexdigest()
http = subprocess.Popen([sys.executable, "-m", "http.server", "18999", "--bind", "127.0.0.1",
                         "--directory", str(srv)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
atexit.register(http.terminate)
URL = "http://10.0.2.2:18999"


def upgrade(name, sha, extra=""):
    return run(f"mantis-upgrade install {URL}/{name} --sha256 {sha} --no-reboot {extra} 2>&1 | tail -4; "
               "echo RC=$?", timeout=900)


def reboot_and_login(expect_reboots=1, timeout=900):
    """reboot; with expect_reboots=2 the system reboots once more by itself
    (a trial that did not confirm) before the final login prompt"""
    child.sendline("reboot")
    for _ in range(expect_reboots - 1):
        child.expect("mantis login:", timeout=timeout)
    child.expect("mantis login:", timeout=timeout)
    login()
    for ns, nic, ip in (("left", left_if, "10.50.0.1"), ("right", right_if, "10.50.0.2")):
        run(f"ip netns add {ns}; ip link set {nic} netns {ns}; "
            f"ip netns exec {ns} sh -c 'ip link set lo up; ip link set {nic} up; ip addr add {ip}/24 dev {nic}'")


def cmdline():
    out = run("cat /proc/cmdline")
    slot = re.search(r"mantis\.slot=(\w)", out)
    return (slot.group(1) if slot else "?"), ("mantis.trial=1" in out)


out = run("ls /dev/disk/by-partlabel | tr '\\n' ' '; mantis-upgrade status")
slot, trial = cmdline()
check("A/B layout: root-a / root-b, booted and committed slot a",
      "root-a" in out and "root-b" in out and slot == "a" and not trial and "committed slot: a" in out,
      out.replace("\n", " | ")[:220])

out = upgrade("foreign.ext4", sha_foreign)
check("refuses an image that is not a Mantis rootfs (no trial armed)",
      "not a Mantis rootfs" in out and "pending trial" not in run("mantis-upgrade status"), out.replace("\n", " | "))

before = run("md5sum /opt/mantis/config.toml /opt/mantis/state/app.db | cut -d' ' -f1 | tr '\\n' ' '")
out = upgrade("good.ext4.gz", sha_good)
check("upgrade written to slot b and verified, trial armed",
      "Slot b verified" in out and "pending trial:  b" in run("mantis-upgrade status"), out.replace("\n", " | "))

reboot_and_login()
slot, trial = cmdline()
confirmed, _ = wait_for("grep -c 'slot b confirmed' /opt/mantis/state/logs/upgrade.log", "1", 600)
out = run("mantis-upgrade status")
after = run("md5sum /opt/mantis/config.toml | cut -d' ' -f1")
ok, detail = wire_ping()
check("trial boot of slot b confirmed once Mantis is up, now committed",
      slot == "b" and trial and confirmed and "committed slot: b" in out, out.replace("\n", " | ")[:220])
check("after the upgrade: config.toml kept, Mantis forwards",
      after.strip() in before and ok and "XDP attached" in run(STATUS), detail)

# broken slot: boots, Mantis never comes up -> S99upgrade reboots -> rollback
run("sed -i '/^UPGRADE_CONFIRM_SECS=/d' /opt/mantis/appliance.conf; "
    "echo UPGRADE_CONFIRM_SECS=150 >> /opt/mantis/appliance.conf")
run("echo marker-before-broken > /opt/mantis/state/test-marker")
out = upgrade("broken.ext4.gz", sha_broken, "--force")
check("broken image written to slot a (forced: Mantis binary removed)", "Slot a verified" in out,
      out.replace("\n", " | "))
reboot_and_login(expect_reboots=2, timeout=1200)
slot, trial = cmdline()
out = run("mantis-upgrade status")
check("broken slot a never confirmed: rebooted by itself, rolled back to slot b",
      slot == "b" and not trial and "FAILED, rolled back to slot b" in out and "committed slot: b" in out,
      out.replace("\n", " | ")[:260])
out = run("ls /opt/mantis/state/ | tr '\\n' ' '; tail -3 /opt/mantis/state/logs/upgrade.log | cut -c1-120")
check("rollback restored app.db saved before the trial, Mantis back",
      "app.db.failed-upgrade" in out and wait_ready(), out.replace("\n", " | ")[:260])

# slot whose kernel cannot be loaded: GRUB's fallback entry boots slot b
out = upgrade("nokern.ext4.gz", sha_nokern, "--force")
reboot_and_login()
slot, trial = cmdline()
out = run("mantis-upgrade status")
check("slot without a kernel: GRUB falls back to slot b, failure recorded",
      slot == "b" and not trial and "upgrade to slot a" in out and "FAILED" in out and "pending trial" not in out,
      out.replace("\n", " | ")[:260])
run("sed -i '/^UPGRADE_CONFIRM_SECS=/d' /opt/mantis/appliance.conf")
wait_ready()

# --- WiFi management NIC: mac80211_hwsim gives two simulated radios ---
# wlan1 runs a WPA2 access point (wpa_supplicant AP mode); mantis-setup then
# makes wlan0 the management NIC and must associate with it.
out = run("modprobe mac80211_hwsim radios=2 && sleep 3; ls /sys/class/net | grep wlan | tr '\\n' ' '")
wlans = out.split()
check("WiFi stack loads (cfg80211, mac80211, hwsim radios)", len(wlans) >= 2, out)
if len(wlans) >= 2:
    sta, ap = wlans[0], wlans[1]
    run("printf '%s\\n' 'ctrl_interface=/var/run/wpa_ap' 'network={' 'ssid=\"MantisTestAP\"' 'mode=2' "
        "'frequency=2437' 'key_mgmt=WPA-PSK' 'proto=RSN' 'pairwise=CCMP' 'group=CCMP' "
        "'psk=\"mantis-wifi-pass\"' '}' > /tmp/ap.conf")
    # The AP radio goes into its own network namespace: with both radios in
    # one namespace, 10.99.0.1 is a local address and ping never uses the air.
    out = run(f"ip netns add apns && iw phy $(cat /sys/class/net/{ap}/phy80211/name) set netns name apns && "
              f"ip netns exec apns sh -c 'ip link set lo up; wpa_supplicant -B -D nl80211 -i {ap} "
              f"-c /tmp/ap.conf -P /var/run/test-ap.pid && ip addr add 10.99.0.1/24 dev {ap}' && sleep 3 && echo AP_UP")
    check("test access point up", "AP_UP" in out, out.replace("\n", " | ")[-200:])
    wifi_answers = [
        sta,                          # management NIC: WiFi
        "MantisTestAP",               # SSID
        "mantis-wifi-pass",           # password
        "",                           # country
        "static", "10.99.0.2/24", "", "",  # address, gateway, dns
        "", "",                       # ingress, egress (keep)
        "",                           # queues (keep)
        "generic", "y", "n", "open",  # xdp_mode, GRO off, VLAN, fail mode
        "", "4096", "",               # frame_size, frame_count, hugepages
        "n",                          # keep config.toml
        "", "",                       # time zone, NTP (keep)
        "", "",                       # no new SSH key, keep password logins off
        "",                           # management networks (keep)
        "n",                          # root password
        "y",                          # apply now
    ]
    rc = run("printf '%s\\n' " + " ".join(f"'{a}'" for a in wifi_answers)
             + " | mantis-setup > /tmp/setup-wifi.log 2>&1; echo SETUP_RC=$?", timeout=300)
    out = run("tail -6 /tmp/setup-wifi.log; grep -c 'psk=' /opt/mantis/wpa_supplicant.conf; "
              "stat -c %a /opt/mantis/wpa_supplicant.conf")
    check("mantis-setup configures the WiFi management NIC", "SETUP_RC=0" in rc and "Mantis running" in out,
          out.replace("\n", " | ")[-300:])
    out = run(f"wpa_cli -i {sta} status | grep -E '^(wpa_state|ssid|key_mgmt)='")
    check("WiFi associated (WPA2-PSK)", "wpa_state=COMPLETED" in out and "MantisTestAP" in out, out.replace("\n", " | "))
    out = run(f"ip -4 addr show {sta} | grep inet; ping -c 3 -W 2 -I {sta} 10.99.0.1 | tail -2")
    check("traffic over the WiFi management NIC", " 0% packet loss" in out, out.replace("\n", " | "))
    out = run("/etc/init.d/S90mantis status")
    check("Mantis still running after the management NIC change", "running" in out, out)

run("/etc/init.d/S90mantis stop", timeout=60)
child.sendline("poweroff")
try:
    child.expect(pexpect.EOF, timeout=120)
except pexpect.TIMEOUT:
    child.terminate(force=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
