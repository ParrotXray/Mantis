#!/usr/bin/env python3
"""Keep only the iwlwifi firmware the kernel in the image can load.

linux-firmware ships every API version of every Intel WiFi firmware (about
130 MB for the chips in the defconfig). The iwlwifi driver declares, through
MODULE_FIRMWARE, the newest API it supports for each firmware prefix
(e.g. iwlwifi-Qu-b0-hr-b0-77.ucode) and loads the newest file at or below it.
Keep exactly that file per prefix (plus the .pnvm files of kept prefixes) and
drop prefixes the driver does not know.

Newer linux-firmware keeps the files in intel/iwlwifi/ and links them from
/lib/firmware; both places are pruned, so no link is left dangling.

usage: prune-iwlwifi.py <target dir>
"""
import re
import sys
from pathlib import Path

target = Path(sys.argv[1])
mods = list((target / "lib/modules").glob("*/kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko*"))
if not mods:
    print("prune-iwlwifi: iwlwifi.ko not in the image, nothing to do")
    sys.exit(0)
if mods[0].suffix != ".ko":
    sys.exit(f"prune-iwlwifi: compressed module {mods[0].name} not supported")

# .modinfo strings: "firmware=iwlwifi-Qu-b0-hr-b0-77.ucode"
max_api = {}
for prefix, api in re.findall(rb"firmware=(iwlwifi-[A-Za-z0-9_.-]+?)-(\d+)\.ucode\x00", mods[0].read_bytes()):
    prefix, api = prefix.decode(), int(api)
    max_api[prefix] = max(api, max_api.get(prefix, 0))
if not max_api:
    sys.exit("prune-iwlwifi: no firmware= entries found in iwlwifi.ko")

fw_dir = target / "lib/firmware"
dirs = [fw_dir, fw_dir / "intel/iwlwifi"]
entries = [p for d in dirs if d.is_dir() for p in d.glob("iwlwifi-*")]


def real_size(paths):
    return sum(p.stat().st_size for p in paths if p.is_file() and not p.is_symlink())


before = real_size(entries)

# Newest usable API per prefix, by file name (wherever the file lives)
versions = {}
for p in entries:
    m = re.fullmatch(r"(iwlwifi-.+)-(\d+)\.ucode", p.name)
    if m:
        versions.setdefault(m.group(1), set()).add(int(m.group(2)))
keep = set()
for prefix, apis in versions.items():
    usable = [a for a in apis if a <= max_api.get(prefix, -1)]
    if usable:
        keep.add(f"{prefix}-{max(usable)}.ucode")
        keep.add(f"{prefix}.pnvm")

for p in entries:
    if p.name not in keep:
        p.unlink()
remaining = [p for d in dirs if d.is_dir() for p in d.glob("iwlwifi-*")]
dangling = [p for p in remaining if p.is_symlink() and not p.exists()]
if dangling:
    sys.exit(f"prune-iwlwifi: dangling links left: {', '.join(p.name for p in dangling)}")
kept_ucode = sorted({p.name for p in remaining if p.name.endswith(".ucode")})
print(f"prune-iwlwifi: kept {len(kept_ucode)} firmware files for {len(max_api)} driver prefixes, "
      f"{before / 2**20:.0f} MiB -> {real_size(remaining) / 2**20:.0f} MiB")
