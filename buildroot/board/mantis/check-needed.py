#!/usr/bin/env python3
"""Fail the build when the Mantis binary, or an ELF embedded in it (Suricata,
ONNX Runtime), needs a shared library the target rootfs does not provide.

usage: check-needed.py <target dir> <mantis binary>
"""
import platform
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

DT_NEEDED, DT_STRTAB = 1, 5
LIB_DIRS = ("lib", "usr/lib", "lib64", "usr/lib64")


def needed(data, base):
    """DT_NEEDED entries of the ELF64 little-endian image at data[base:]."""
    if data[base:base + 7] != b"\x7fELF\x02\x01\x01":
        return None
    e_type, = struct.unpack_from("<H", data, base + 0x10)
    if e_type not in (2, 3):  # executable or shared object
        return None
    phoff, = struct.unpack_from("<Q", data, base + 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, base + 0x36)
    loads, dynamic = [], None
    for i in range(phnum):
        off = base + phoff + i * phentsize
        p_type, _, p_offset, p_vaddr, _, p_filesz = struct.unpack_from("<IIQQQQ", data, off)
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return []

    def vaddr_to_off(v):
        for vaddr, offset, size in loads:
            if vaddr <= v < vaddr + size:
                return v - vaddr + offset
        raise ValueError("address outside PT_LOAD")

    entries, strtab = [], None
    for i in range(dynamic[1] // 16):
        tag, val = struct.unpack_from("<qQ", data, base + dynamic[0] + i * 16)
        if tag == 0:
            break
        if tag == DT_NEEDED:
            entries.append(val)
        elif tag == DT_STRTAB:
            strtab = vaddr_to_off(val)
    names = []
    for off in entries:
        start = base + strtab + off
        names.append(data[start:data.index(b"\0", start)].decode())
    return names


def main():
    target, binary = Path(sys.argv[1]), Path(sys.argv[2])
    data = binary.read_bytes()
    missing, checked, pos = {}, 0, 0
    while (pos := data.find(b"\x7fELF", pos)) >= 0:
        try:
            libs = needed(data, pos)
        except (struct.error, ValueError):
            libs = None
        if libs is not None:
            checked += 1
            label = "mantis" if pos == 0 else f"embedded ELF @{pos:#x}"
            for lib in libs:
                if not any((target / d / lib).exists() for d in LIB_DIRS):
                    missing.setdefault(lib, []).append(label)
        pos += 4
    if missing:
        for lib, users in sorted(missing.items()):
            print(f"ERROR: {lib} not in the target rootfs (needed by {', '.join(users)})", file=sys.stderr)
        sys.exit(1)
    print(f"check-needed: all libraries of {checked} ELF images in {binary.name} resolve")
    check_symbols(target, data)


def check_symbols(target, data):
    """`ldd -r` with the target's own loader: every symbol of every ELF
    (and of the libraries they pull in) must bind. Only possible when the
    build host can run target code."""
    loader = target / "lib64/ld-linux-x86-64.so.2"
    if platform.machine() != "x86_64" or not loader.exists():
        print("check-needed: symbol check skipped (build host cannot run the target loader)")
        return
    libpath = ":".join(str(target / d) for d in LIB_DIRS)
    env = {"LD_TRACE_LOADED_OBJECTS": "1", "LD_WARN": "yes", "LD_BIND_NOW": "yes"}
    errors, pos = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        while (pos := data.find(b"\x7fELF", pos)) >= 0:
            try:
                libs = needed(data, pos)
            except (struct.error, ValueError):
                libs = None
            if libs is not None:
                size = elf_size(data, pos)
                path = Path(tmp) / f"elf_{pos:x}"
                path.write_bytes(data[pos:pos + size])
                out = subprocess.run([str(loader), "--library-path", libpath, str(path)],
                                     env=env, capture_output=True, text=True)
                # A plugin (shared object, no interpreter: the Suricata capture
                # plugin) takes its own undefined symbols from the program that
                # loads it; only the libraries it pulls in must be complete.
                plugin = not has_interp(data, pos)
                for line in (out.stdout + out.stderr).splitlines():
                    if plugin and line.rstrip().endswith(f"({path})"):
                        continue
                    if "undefined symbol" in line or "not found" in line:
                        errors.append(f"{'mantis' if pos == 0 else f'embedded ELF @{pos:#x}'}: {line.strip()}")
            pos += 4
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print("check-needed: all symbols bind (LD_BIND_NOW)")


def has_interp(data, base):
    phoff, = struct.unpack_from("<Q", data, base + 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, base + 0x36)
    return any(struct.unpack_from("<I", data, base + phoff + i * phentsize)[0] == 3  # PT_INTERP
               for i in range(phnum))


def elf_size(data, base):
    shoff, = struct.unpack_from("<Q", data, base + 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, base + 0x3a)
    return shoff + shentsize * shnum


if __name__ == "__main__":
    main()
