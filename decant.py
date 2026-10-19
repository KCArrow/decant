#!/usr/bin/env python3
"""
Decant: flash bzip2-compressed disk images to USB drives and SD cards.

A desktop front end for bzcat, in the spirit of image flashers like Etcher:
choose an image, choose a drive, flash, and read the drive back to verify.

  decant.py [IMAGE]          open the app (optionally with an image preselected)
  decant.py --list-drives    print the drives Decant sees and how it classifies them
  decant.py --install        add Decant to your application menu (~/.local)
  decant.py --version        print the version
  decant.py --screenshots D  render every screen with demo data into folder D

https://github.com/KCArrow/decant  (MIT license)

Writing to a drive needs root. The window runs as you; the write itself runs in
a small helper started through pkexec (polkit), which asks for your password.
The helper only uses the Python standard library, so PySide6 is needed for the
window but not for the privileged part.

Requires: Python 3.9+, PySide6, bzip2 (bzcat), util-linux (lsblk), polkit.
Optional: lbzip2 for multi-threaded decompression; xz, gzip/pigz, zstd for
images compressed with those.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import mmap
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

APP_NAME = "Decant"
APP_ID = "decant"
VERSION = "1.0.0"
FROZEN = bool(getattr(sys, "frozen", False))  # running as the portable binary


def self_command() -> list:
    """argv prefix that re-runs this program: the portable binary, or python + script."""
    if FROZEN:
        return [sys.executable]
    return [sys.executable, os.path.realpath(__file__)]


def sys_env() -> dict:
    """Environment for running system tools. The portable binary points
    LD_LIBRARY_PATH at its bundled libraries, which system programs must not load."""
    env = dict(os.environ)
    if FROZEN:
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env

# ---------------------------------------------------------------------------
# Shared: image formats and decompressors
# ---------------------------------------------------------------------------

# Decompressors the helper is allowed to run as root, by name. Resolved only
# against this PATH so a user-controlled binary can never be run as root.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

DECOMPRESSORS = {
    # name: argv (reads stdin, writes stdout)
    "lbzip2": ["lbzip2", "-d", "-c"],
    "bzcat": ["bzcat"],
    "xz": ["xz", "-d", "-c", "-T0"],
    "pigz": ["pigz", "-d", "-c"],
    "gzip": ["gzip", "-d", "-c"],
    "zstd": ["zstd", "-d", "-c", "-q"],
}

FORMAT_LABELS = {
    "bzip2": "bzip2",
    "xz": "xz",
    "gzip": "gzip",
    "zstd": "zstd",
    "raw": "uncompressed",
}

# Preference order for each format; first one installed wins.
FORMAT_DECOMPRESSORS = {
    "bzip2": ["lbzip2", "bzcat"],
    "xz": ["xz"],
    "gzip": ["pigz", "gzip"],
    "zstd": ["zstd"],
}


def which_safe(name: str) -> str | None:
    return shutil.which(name, path=SAFE_PATH)


def detect_format(path: str) -> str | None:
    """Identify the image by its magic bytes. None if unreadable or unsupported."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    lower = path.lower()
    if lower.endswith((".img", ".iso", ".raw", ".bin", ".dd")):
        return "raw"
    return None


def pick_decompressor(fmt: str, bzcat_only: bool = False) -> str | None:
    """Name of the decompressor to use for fmt, or None for raw images.

    Raises LookupError if the format needs a tool that isn't installed."""
    if fmt == "raw":
        return None
    names = FORMAT_DECOMPRESSORS.get(fmt, [])
    if fmt == "bzip2" and bzcat_only:
        names = ["bzcat"]
    for name in names:
        if which_safe(name):
            return name
    raise LookupError(names[-1] if names else fmt)


def uncompressed_size(path: str, fmt: str) -> int | None:
    """Uncompressed size when it can be known cheaply; bzip2 can't."""
    if fmt == "raw":
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    if fmt == "xz" and which_safe("xz"):
        try:
            out = subprocess.run(
                [which_safe("xz"), "--robot", "--list", path],
                capture_output=True, text=True, timeout=15, env=sys_env(),
            ).stdout
            for line in out.splitlines():
                parts = line.split("\t")
                if parts and parts[0] == "totals" and len(parts) > 4:
                    return int(parts[4])
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


# ---------------------------------------------------------------------------
# Shared: drive discovery (lsblk)
# ---------------------------------------------------------------------------

CRITICAL_MOUNTS = {
    "/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "/opt",
    "/srv", "/root", "/nix", "/gnu", "/tmp", "/var/log", "/var/lib",
    "/sysroot", "/run/initramfs/live",
}
HOLDER_TYPES = {"lvm", "crypt", "dm", "mpath"}
LARGE_DRIVE = 128 * 10**9  # bytes; flag anything bigger than a big USB stick

LSBLK_COLUMNS = ["NAME", "PATH", "SIZE", "MODEL", "VENDOR", "SERIAL", "TRAN", "RM",
                 "HOTPLUG", "TYPE", "RO", "LABEL", "PARTLABEL", "FSTYPE"]


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip() in ("1", "true", "yes")
    return False


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _mounts_of(node: dict) -> list:
    mps = node.get("mountpoints")
    if mps is None:
        mps = [node.get("mountpoint")]
    return [m for m in mps if m]


def run_lsblk(target: str | None = None) -> list:
    lsblk = which_safe("lsblk") or "lsblk"
    for mount_col in ("MOUNTPOINTS", "MOUNTPOINT"):  # MOUNTPOINTS needs util-linux 2.37+
        cmd = [lsblk, "-J", "-b", "-o", ",".join(LSBLK_COLUMNS + [mount_col])]
        if target:
            cmd.append(target)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=sys_env())
        except (OSError, subprocess.SubprocessError):
            return []
        if res.returncode == 0:
            try:
                return json.loads(res.stdout).get("blockdevices", [])
            except ValueError:
                return []
    return []


def _clean(s) -> str:
    return " ".join(str(s or "").replace("_", " ").split())


def describe_drive(node: dict) -> dict:
    """Flatten one lsblk disk node into the facts the UI and helper need."""
    name = node.get("name") or ""
    path = node.get("path") or ("/dev/" + name)
    tran = (node.get("tran") or "").lower()
    size = _int(node.get("size"))

    mounts, labels, holders, swap = [], [], [], False

    def walk(n, depth):
        nonlocal swap
        for mp in _mounts_of(n):
            if mp == "[SWAP]":
                swap = True
            else:
                mounts.append(mp)
        if depth > 0:
            label = n.get("label") or n.get("partlabel")
            if label and n.get("type") == "part":
                labels.append(label)
            if (n.get("type") or "") in HOLDER_TYPES or (n.get("type") or "").startswith("raid"):
                holders.append(n.get("type"))
        if (n.get("fstype") or "") == "zfs_member":
            holders.append("zfs")  # pool datasets never show up as lsblk mounts
        for c in n.get("children") or []:
            walk(c, depth + 1)

    walk(node, 0)

    system_reason = None
    for mp in mounts:
        if mp in CRITICAL_MOUNTS or mp.startswith("/boot") or mp.startswith("/run/initramfs"):
            system_reason = "Holds " + mp
            break
    if not system_reason and swap:
        system_reason = "Used for swap"

    removable = (_truthy(node.get("rm")) or _truthy(node.get("hotplug"))
                 or tran in ("usb", "mmc", "sdio", "ieee1394") or name.startswith("mmcblk"))

    if name.startswith("mmcblk") or tran in ("mmc", "sdio"):
        kind = "sd"
    elif tran == "usb":
        kind = "usb"
    elif name.startswith("nvme") or tran == "nvme":
        kind = "nvme"
    else:
        kind = "disk"

    vendor = _clean(node.get("vendor"))
    if vendor.upper() == "ATA":  # placeholder the SATA layer reports, not a brand
        vendor = ""
    model = _clean(" ".join(x for x in (vendor, _clean(node.get("model"))) if x))
    if not model or model.startswith("0x"):
        model = {"sd": "SD card", "usb": "USB drive", "nvme": "NVMe drive"}.get(kind, "Drive " + name)

    return {
        "name": name,
        "path": path,
        "size": size,
        "model": model,
        "serial": _clean(node.get("serial")),
        "tran": tran,
        "kind": kind,
        "removable": removable,
        "readonly": _truthy(node.get("ro")),
        "mounts": sorted(set(mounts)),
        "labels": labels,
        "system": system_reason,
        "holders": sorted(set(holders)),
        "large": size > LARGE_DRIVE,
    }


def list_drives() -> list:
    drives = []
    for node in run_lsblk():
        if node.get("type") != "disk":
            continue
        name = node.get("name") or ""
        if name.startswith(("zram", "ram", "loop", "sr", "fd")):
            continue
        if _int(node.get("size")) <= 0:
            continue
        drives.append(describe_drive(node))
    return drives


def drive_block_reason(d: dict) -> str | None:
    """Why a drive can never be chosen, or None if it can."""
    if d["system"]:
        return "System drive"
    if d["readonly"]:
        return "Read-only"
    if d["holders"]:
        h = d["holders"]
        return "In use by " + ("ZFS" if "zfs" in h else "LVM" if "lvm" in h else
                               "encryption" if "crypt" in h else "RAID")
    return None


# ---------------------------------------------------------------------------
# Privileged helper: decompress, write, sync, verify
# ---------------------------------------------------------------------------

CHUNK = 4 << 20           # write unit
SYNC_EVERY = 32 << 20     # flush to the device this often so progress stays honest
VERIFY_BLOCK = 32 << 20   # hashing granularity; a mismatch is located to this
EMIT_INTERVAL = 0.2       # seconds between progress events
BLKFLSBUF = 0x1261
BLKRRPART = 0x125F
F_SETPIPE_SZ = getattr(fcntl, "F_SETPIPE_SZ", 1031)


class Cancelled(Exception):
    pass


class HelperError(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def fmt_bytes(n: float) -> str:
    """Drive-vendor style decimal units: 31.9 GB."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return ("%.1f %s" % (n, unit)) if n < 100 else ("%.0f %s" % (n, unit))
        n /= 1000.0
    return "%.1f TB" % n


class Helper:
    def __init__(self, source, target, decompressor, verify, eject, allow_file, watch_stdin=True,
                 expect=None):
        self.expect = expect or {}
        self.source = source
        self.target = target
        self.decompressor = decompressor
        self.verify = verify
        self.eject = eject
        self.allow_file = allow_file
        self.watch_stdin = watch_stdin
        self.cancel = threading.Event()
        self.proc = None
        self.feed_error = None
        self.stderr_tail = b""
        self.t0 = time.monotonic()
        self.last_emit = 0.0
        self.digests = []

    # -- events -------------------------------------------------------------
    def emit(self, ev: str, **kw):
        kw["ev"] = ev
        try:
            sys.stdout.write(json.dumps(kw) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # The window went away; stop rather than write unsupervised.
            self.request_cancel()

    def progress(self, force=False, **kw):
        now = time.monotonic()
        if force or now - self.last_emit >= EMIT_INTERVAL:
            self.last_emit = now
            self.emit("progress", t=round(now - self.t0, 3), **kw)

    def request_cancel(self):
        self.cancel.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _stdin_watch(self):
        try:
            for line in sys.stdin:
                if line.strip() == "cancel":
                    break
        except (OSError, ValueError):
            pass
        # "cancel" or EOF (window closed): stop.
        self.request_cancel()

    # -- safety -------------------------------------------------------------
    def prepare_target(self):
        try:
            st = os.stat(self.target)
        except OSError as e:
            raise HelperError("no_target", "The drive %s is gone (%s)." % (self.target, e.strerror))
        self.is_block = stat.S_ISBLK(st.st_mode)
        if not self.is_block:
            if self.allow_file and stat.S_ISREG(st.st_mode):
                return
            raise HelperError("bad_target", "%s isn't a drive." % self.target)

        nodes = run_lsblk(self.target)
        if not nodes:
            raise HelperError("bad_target", "lsblk can't see %s." % self.target)
        node = nodes[0]
        # Loop devices are only accepted in test mode (--allow-file).
        if node.get("type") != "disk" and not (self.allow_file and node.get("type") == "loop"):
            if node.get("type") == "part":
                raise HelperError("bad_target", "%s is a partition. Choose the whole drive." % self.target)
            raise HelperError("bad_target", "%s isn't a drive Decant can write to." % self.target)
        d = describe_drive(node)
        # The drive must still be the one the user confirmed (it may have been
        # swapped while the password prompt was open).
        if self.expect and (d["size"] != self.expect.get("size")
                            or d["serial"] != self.expect.get("serial", d["serial"])):
            raise HelperError("no_target", "The drive at %s changed since you chose it. "
                              "Choose it again." % self.target)
        reason = drive_block_reason(d)
        if reason:
            raise HelperError("system_drive", "Refusing to write to %s: %s." % (self.target, reason.lower()))

        umount = which_safe("umount") or "umount"
        for mp in sorted(d["mounts"], key=len, reverse=True):
            self.emit("phase", phase="unmounting", detail=mp)
            r = subprocess.run([umount, mp], capture_output=True, text=True, env=sys_env())
            if r.returncode != 0:
                raise HelperError(
                    "busy",
                    "Couldn't unmount %s. Close any files or terminals using it and try again." % mp,
                )

    def open_target(self) -> int:
        if self.is_block:
            flags = os.O_WRONLY | os.O_EXCL | os.O_CLOEXEC
        else:
            flags = os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC
        try:
            return os.open(self.target, flags)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise HelperError("busy", "%s is in use by another program. Close it and try again." % self.target)
            if e.errno == errno.EROFS:
                raise HelperError("readonly", "%s is write-protected. Check the lock switch on the card." % self.target)
            raise HelperError("open_failed", "Couldn't open %s: %s." % (self.target, e.strerror))

    # -- source stream ------------------------------------------------------
    def open_stream(self):
        """Returns (reader, compressed_total). reader has readinto()."""
        try:
            src = open(self.source, "rb", buffering=0)
        except OSError as e:
            raise HelperError("no_source", "Couldn't read the image: %s." % e.strerror)
        total = os.fstat(src.fileno()).st_size
        self.src_file = src
        self.in_bytes = 0

        if not self.decompressor:
            return src, total

        if self.decompressor not in DECOMPRESSORS:
            raise HelperError("bad_decompressor", "Unknown decompressor %r." % self.decompressor)
        argv = list(DECOMPRESSORS[self.decompressor])
        exe = which_safe(argv[0])
        if not exe:
            raise HelperError("no_decompressor", "%s isn't installed." % argv[0])
        argv[0] = exe
        env = {"PATH": SAFE_PATH, "LC_ALL": "C"}
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, env=env)
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                fcntl.fcntl(pipe.fileno(), F_SETPIPE_SZ, 1 << 20)
            except OSError:
                pass
        threading.Thread(target=self._feed, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return self.proc.stdout, total

    def _feed(self):
        buf = bytearray(1 << 20)
        mv = memoryview(buf)
        try:
            while not self.cancel.is_set():
                n = self.src_file.readinto(mv)
                if not n:
                    break
                self.proc.stdin.write(mv[:n])
                self.in_bytes += n
        except BrokenPipeError:
            pass  # decompressor exited; its exit status explains why
        except OSError as e:
            self.feed_error = e
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _drain_stderr(self):
        try:
            for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
                self.stderr_tail = (self.stderr_tail + chunk)[-2048:]
        except (OSError, ValueError):
            pass

    # -- hashing ------------------------------------------------------------
    def _hash_init(self):
        self.digests = []
        self._h = hashlib.sha256()
        self._hlen = 0

    def _hash_feed(self, view):
        while len(view):
            take = min(len(view), VERIFY_BLOCK - self._hlen)
            self._h.update(view[:take])
            self._hlen += take
            view = view[take:]
            if self._hlen == VERIFY_BLOCK:
                self.digests.append(self._h.digest())
                self._h = hashlib.sha256()
                self._hlen = 0

    def _hash_finish(self):
        if self._hlen:
            self.digests.append(self._h.digest())
        self._hlen = 0

    # -- phases -------------------------------------------------------------
    def write(self, fd) -> int:
        reader, in_total = self.open_stream()
        raw = self.decompressor is None
        self._hash_init()
        buf = bytearray(CHUNK)
        mv = memoryview(buf)
        out = 0
        since_sync = 0
        self.emit("phase", phase="writing", in_total=in_total)

        while True:
            n = 0
            eof = False
            while n < CHUNK:
                self.check_cancel()
                r = reader.readinto(mv[n:])
                if not r:
                    eof = True
                    break
                n += r
            self.check_cancel()
            if n:
                view = mv[:n]
                while len(view):
                    try:
                        w = os.write(fd, view)
                    except OSError as e:
                        if e.errno in (errno.ENOSPC, errno.EFBIG, errno.ENXIO):
                            raise HelperError(
                                "too_small",
                                "The image doesn't fit. The drive filled up after %s; "
                                "choose a larger drive." % fmt_bytes(out),
                            )
                        if e.errno == errno.EIO:
                            raise HelperError(
                                "io_error",
                                "The drive reported a write error after %s. It may be failing "
                                "or was unplugged." % fmt_bytes(out),
                            )
                        raise HelperError("write_failed", "Writing failed: %s." % e.strerror)
                    if w == 0:
                        raise HelperError("too_small", "The image doesn't fit on this drive.")
                    out += w
                    view = view[w:]
                self._hash_feed(mv[:n])
                since_sync += n
                if since_sync >= SYNC_EVERY:
                    os.fdatasync(fd)
                    since_sync = 0
                if raw:
                    self.in_bytes = out
                self.progress(phase="writing", **{"in": self.in_bytes, "in_total": in_total, "out": out})
            if eof:
                break

        self._hash_finish()
        if self.proc:
            rc = self.proc.wait()
            self.check_cancel()
            if self.feed_error:
                raise HelperError("read_failed", "Reading the image failed: %s." % self.feed_error.strerror)
            if rc != 0:
                tool = DECOMPRESSORS[self.decompressor][0]
                lines = [l.strip() for l in self.stderr_tail.decode("utf-8", "replace").splitlines() if l.strip()]
                tagged = [l[len(tool) + 1:].strip() for l in lines if l.startswith(tool + ":")]
                detail = (tagged or lines or ["exit status %d" % rc])[0].rstrip(";.:")
                detail = detail[:1].lower() + detail[1:]
                raise HelperError(
                    "corrupt",
                    "The image couldn't be decompressed, so the drive is only partly written. "
                    "The file may be damaged or incomplete (%s)." % detail,
                )
        if out == 0:
            raise HelperError("empty", "The image decompressed to nothing. Check that it's a disk image.")
        self.progress(force=True, phase="writing", **{"in": in_total, "in_total": in_total, "out": out})
        return out

    def sync(self, fd):
        self.emit("phase", phase="syncing")
        os.fsync(fd)
        if self.is_block:
            try:
                fcntl.ioctl(fd, BLKFLSBUF, 0)
            except OSError:
                pass

    def verify_readback(self, total: int) -> float:
        self.emit("phase", phase="verifying", total=total)
        t_start = time.monotonic()
        direct = True
        try:
            fd = os.open(self.target, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECT", 0))
        except OSError:
            direct = False
            fd = os.open(self.target, os.O_RDONLY | os.O_CLOEXEC)
        buf = mmap.mmap(-1, VERIFY_BLOCK)  # page-aligned, as O_DIRECT requires
        mv = memoryview(buf)
        try:
            if not direct:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            done = 0
            for i, expected in enumerate(self.digests):
                self.check_cancel()
                want = min(VERIFY_BLOCK, total - done)
                ask = (want + 4095) & ~4095 if direct else want
                got = 0
                while got < want:
                    try:
                        r = os.preadv(fd, [mv[got:ask]], done + got)
                    except OSError as e:
                        if e.errno == errno.EINVAL and direct and done == 0 and got == 0:
                            # Filesystem or device without O_DIRECT support.
                            os.close(fd)
                            fd = os.open(self.target, os.O_RDONLY | os.O_CLOEXEC)
                            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                            direct = False
                            ask = want
                            continue
                        raise HelperError("verify_read", "Reading the drive back failed at %s: %s."
                                          % (fmt_bytes(done + got), e.strerror))
                    if r == 0:
                        break
                    got += r
                if got < want:
                    raise HelperError("verify_short", "The drive ended early while reading back, at %s."
                                      % fmt_bytes(done + got))
                if hashlib.sha256(mv[:want]).digest() != expected:
                    raise HelperError(
                        "verify_mismatch",
                        "Verification failed: the data read back from the drive differs from the image "
                        "between %s and %s. The drive may be faulty or smaller than it claims."
                        % (fmt_bytes(done), fmt_bytes(done + want)),
                    )
                done += want
                self.progress(phase="verifying", done=done, total=total)
            self.progress(force=True, phase="verifying", done=done, total=total)
        finally:
            mv.release()
            buf.close()
            os.close(fd)
        return time.monotonic() - t_start

    def finish(self) -> bool:
        """Re-read the partition table or eject. Returns True if ejected."""
        if not self.is_block:
            return False
        if self.eject:
            self.emit("phase", phase="ejecting")
            eject = which_safe("eject")
            if eject:
                r = subprocess.run([eject, self.target], capture_output=True, env=sys_env())
                if r.returncode == 0:
                    return True
        try:
            fd = os.open(self.target, os.O_RDONLY | os.O_CLOEXEC)
            try:
                fcntl.ioctl(fd, BLKRRPART, 0)
            finally:
                os.close(fd)
        except OSError:
            pass
        return False

    def run(self) -> int:
        if self.watch_stdin:
            threading.Thread(target=self._stdin_watch, daemon=True).start()
        fd = None
        try:
            self.emit("phase", phase="preparing")
            self.check_cancel()  # cancelled while the password prompt was up
            self.prepare_target()
            self.check_cancel()
            fd = self.open_target()
            written = self.write(fd)
            self.sync(fd)
            os.close(fd)
            fd = None
            write_s = time.monotonic() - self.t0
            verify_s = self.verify_readback(written) if self.verify else 0.0
            ejected = self.finish()
            self.emit("done", bytes=written, write_s=round(write_s, 2), verify_s=round(verify_s, 2),
                      verified=self.verify, ejected=ejected)
            return 0
        except Cancelled:
            self.emit("cancelled")
            return 3
        except HelperError as e:
            if self.cancel.is_set():
                self.emit("cancelled")
                return 3
            self.emit("error", code=e.code, msg=e.msg)
            return 1
        except Exception as e:  # last resort: never die silently as root
            self.emit("error", code="internal", msg="%s: %s" % (type(e).__name__, e))
            return 1
        finally:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            if self.proc:
                self.proc.wait()
                for pipe in (self.proc.stdout, self.proc.stderr):
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
            src = getattr(self, "src_file", None)
            if src is not None:
                src.close()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def helper_main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="decant --helper")
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--decompressor", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--eject", action="store_true")
    ap.add_argument("--allow-file", action="store_true")
    ap.add_argument("--no-stdin", action="store_true")
    ap.add_argument("--expect-size", type=int)
    ap.add_argument("--expect-serial")
    a = ap.parse_args(argv)
    os.environ["PATH"] = SAFE_PATH
    os.umask(0o077)
    expect = {}
    if a.expect_size is not None:
        expect["size"] = a.expect_size
        if a.expect_serial is not None:
            expect["serial"] = a.expect_serial
    h = Helper(a.source, a.target, a.decompressor or None, a.verify, a.eject,
               a.allow_file, watch_stdin=not a.no_stdin, expect=expect)
    return h.run()


def list_drives_main() -> int:
    drives = list_drives()
    if not drives:
        print("No drives found.")
        return 0
    for d in drives:
        flags = []
        reason = drive_block_reason(d)
        if reason:
            flags.append("blocked: " + reason)
        flags.append("removable" if d["removable"] else "internal")
        if d["large"]:
            flags.append("large")
        if d["mounts"]:
            flags.append("mounted at " + ", ".join(d["mounts"]))
        print("%-14s %9s  %-28s %s" % (d["path"], fmt_bytes(d["size"]), d["model"][:28], "; ".join(flags)))
    return 0


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
    print("%s %s" % (APP_NAME, VERSION))
    sys.exit(0)
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
    print(__doc__.strip())
    sys.exit(0)
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--helper":
    sys.exit(helper_main(sys.argv[2:]))
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--list-drives":
    sys.exit(list_drives_main())

# ---------------------------------------------------------------------------
# Desktop integration (no Qt needed)
# ---------------------------------------------------------------------------

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="28" fill="#1A1F1E"/>
  <clipPath id="c"><circle cx="64" cy="64" r="44"/></clipPath>
  <circle cx="64" cy="64" r="44" fill="#222927"/>
  <g clip-path="url(#c)">
    <path d="M14 70 Q 39 60 64 70 T 114 70 V 120 H 14 Z" fill="#A8722C"/>
    <path d="M14 74 Q 39 84 64 74 T 114 74 V 120 H 14 Z" fill="#E3A54C"/>
  </g>
  <circle cx="64" cy="64" r="44" fill="none" stroke="#5E6C67" stroke-width="4"/>
</svg>
"""


def install_desktop_main() -> int:
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    apps = os.path.join(data, "applications")
    icons = os.path.join(data, "icons", "hicolor", "scalable", "apps")
    os.makedirs(apps, exist_ok=True)
    os.makedirs(icons, exist_ok=True)
    with open(os.path.join(icons, APP_ID + ".svg"), "w") as f:
        f.write(ICON_SVG)
    exec_line = " ".join('"%s"' % part for part in self_command()) + " %f"
    desktop = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + APP_NAME,
        "GenericName=Image Writer",
        "Comment=Flash compressed disk images to USB drives and SD cards",
        "Exec=" + exec_line,
        "Icon=" + APP_ID,
        "Terminal=false",
        "Categories=System;Utility;",
        "Keywords=bzip2;bzcat;flash;usb;iso;image;sd card;",
        "MimeType=application/x-bzip2;application/x-bzip;application/x-xz;application/gzip;"
        "application/zstd;application/x-cd-image;application/x-raw-disk-image;",
        "StartupWMClass=" + APP_ID,
        "",
    ])
    path = os.path.join(apps, APP_ID + ".desktop")
    with open(path, "w") as f:
        f.write(desktop)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", apps], capture_output=True, env=sys_env())
    print("Installed %s" % path)
    return 0


if __name__ == "__main__" and "--install" in sys.argv[1:]:
    sys.exit(install_desktop_main())

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import (QObject, QPointF, QProcess, QProcessEnvironment, QRectF,
                                QSettings, QSize, Qt, QTimer, Signal)
    from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics, QIcon,
                               QPainter, QPainterPath, QPalette, QPen, QPixmap)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                                   QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                                   QMessageBox, QPushButton, QSizePolicy, QStackedWidget,
                                   QToolButton, QVBoxLayout, QWidget)
except ImportError:
    if __name__ == "__main__":
        sys.stderr.write(
            "Decant's window needs PySide6 (Qt for Python).\n"
            "  pip install --user PySide6        # any distro\n"
            "  sudo pacman -S pyside6            # Arch / CachyOS\n"
            "The engine itself works without it:  decant.py --list-drives\n"
        )
        sys.exit(1)
    raise

import math
import re
from string import Template


# -- tokens -----------------------------------------------------------------
# Bottle-glass green and an amber pour: the image is decanted into the drive.

GROUND = "#1A1F1E"
SURFACE = "#222927"
SURFACE_2 = "#2B3431"
LINE = "#3A4743"
TEXT = "#E7ECE9"
MUTED = "#95A59E"
FAINT = "#62716B"
AMBER = "#E3A54C"
AMBER_HI = "#EDB867"
AMBER_DEEP = "#A8722C"
AMBER_CLEAR = "#F6DFB0"
DANGER = "#E27463"
DANGER_DEEP = "#7E3A31"

DISPLAY_FAMILY = ""
TEXT_FAMILY = ""


def init_fonts():
    global DISPLAY_FAMILY, TEXT_FAMILY
    fams = set(QFontDatabase.families())
    fallback = QApplication.font().family()

    def first(cands):
        for c in cands:
            if c in fams:
                return c
        return fallback

    # Red Hat's faces ship with Rocky/RHEL; Plex, Inter, Noto cover the rest.
    DISPLAY_FAMILY = first(["Red Hat Display", "IBM Plex Sans", "Inter Display", "Inter",
                            "Noto Sans", "Cantarell"])
    TEXT_FAMILY = first(["Red Hat Text", "IBM Plex Sans", "Inter", "Noto Sans", "Cantarell"])


def font(px, weight=QFont.Weight.Normal, display=False, tabular=False) -> QFont:
    f = QFont(DISPLAY_FAMILY if display else TEXT_FAMILY)
    f.setPixelSize(px)
    f.setWeight(weight)
    if tabular:
        try:  # tabular figures keep changing numbers from jittering; Qt 6.7+
            tag = QFont.Tag.fromString("tnum") if hasattr(QFont.Tag, "fromString") else QFont.Tag("tnum")
            f.setFeature(tag, 1)
        except Exception:
            pass
    return f


QSS = Template("""
QWidget#ground, QDialog { background: $GROUND; }
QLabel { color: $TEXT; background: transparent; }
QLabel[role="muted"] { color: $MUTED; }
QLabel[role="faint"] { color: $FAINT; }
QLabel[role="warn"] { color: $AMBER; }
QLabel[role="danger"] { color: $DANGER; }
QLabel:disabled { color: $FAINT; }

QPushButton {
    font-family: "$TEXTF"; font-size: 14px; font-weight: 600;
    min-height: 40px; border-radius: 20px; padding: 0 26px;
}
QPushButton#primary { background: $AMBER; color: $GROUND; border: 2px solid $AMBER; }
QPushButton#primary:hover { background: $AMBER_HI; border-color: $AMBER_HI; }
QPushButton#primary:pressed { background: $AMBER_DEEP; border-color: $AMBER_DEEP; }
QPushButton#primary:focus { border-color: $AMBER_CLEAR; }
QPushButton#primary:disabled { background: $SURFACE_2; border-color: $SURFACE_2; color: $FAINT; }
QPushButton#secondary { background: transparent; color: $TEXT; border: 1px solid $LINE; }
QPushButton#secondary:hover { border-color: $MUTED; }
QPushButton#secondary:focus { border: 2px solid $AMBER; }
QPushButton#secondary:disabled { color: $FAINT; border-color: $SURFACE_2; }
QPushButton#danger { background: $DANGER; color: $GROUND; border: 2px solid $DANGER; }
QPushButton#danger:focus { border-color: $AMBER_CLEAR; }
QPushButton#danger:disabled { background: $SURFACE_2; border-color: $SURFACE_2; color: $FAINT; }
QPushButton#link, QToolButton#link {
    background: transparent; border: none; color: $AMBER;
    padding: 0 4px; min-height: 22px; border-radius: 0;
    font-family: "$TEXTF"; font-size: 13px; font-weight: 500;
}
QPushButton#link:hover, QToolButton#link:hover,
QPushButton#link:focus, QToolButton#link:focus { color: $AMBER_HI; text-decoration: underline; }
QToolButton#link:disabled { color: $FAINT; }
QToolButton#link::menu-indicator { image: none; width: 0; }

QMenu { background: $SURFACE_2; color: $TEXT; border: 1px solid $LINE; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 7px 20px 7px 30px; border-radius: 6px; }
QMenu::item:selected { background: $LINE; }
QMenu::item:disabled { color: $FAINT; }
QMenu::separator { height: 1px; background: $LINE; margin: 5px 8px; }

QListWidget#drives { background: transparent; border: none; outline: none; }
QListWidget#drives::item { background: $SURFACE; border: 1px solid $SURFACE; border-radius: 10px; margin: 3px 2px; }
QListWidget#drives::item:hover { background: $SURFACE_2; border-color: $SURFACE_2; }
QListWidget#drives::item:selected { background: $SURFACE_2; border: 1px solid $AMBER; }
QListWidget#drives::item:disabled { background: transparent; border: 1px solid $SURFACE; }

QCheckBox { color: $MUTED; spacing: 8px; }
QCheckBox:focus { color: $TEXT; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid $FAINT; border-radius: 4px; background: $SURFACE; }
QCheckBox::indicator:hover { border-color: $MUTED; }
QCheckBox::indicator:checked { background: $AMBER; border-color: $AMBER; }

QLabel#badge { border-radius: 9px; padding: 1px 8px; }
QLabel#badge[tone="warn"] { color: $AMBER; border: 1px solid $AMBER_DEEP; }
QLabel#badge[tone="block"] { color: $FAINT; border: 1px solid $LINE; }

QToolTip { background: $SURFACE_2; color: $TEXT; border: 1px solid $LINE; padding: 4px 8px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: $LINE; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QMessageBox QLabel { min-width: 280px; }
""")


def apply_theme(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: GROUND, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE, QPalette.ColorRole.AlternateBase: SURFACE_2,
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE_2,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: AMBER,
        QPalette.ColorRole.HighlightedText: GROUND, QPalette.ColorRole.ToolTipBase: SURFACE_2,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: FAINT,
        QPalette.ColorRole.Link: AMBER,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(FAINT))
    app.setPalette(pal)
    app.setFont(font(13))
    tokens = dict(GROUND=GROUND, SURFACE=SURFACE, SURFACE_2=SURFACE_2, LINE=LINE, TEXT=TEXT,
                  MUTED=MUTED, FAINT=FAINT, AMBER=AMBER, AMBER_HI=AMBER_HI, AMBER_DEEP=AMBER_DEEP,
                  AMBER_CLEAR=AMBER_CLEAR, DANGER=DANGER, TEXTF=TEXT_FAMILY)
    app.setStyleSheet(QSS.substitute(tokens))


def set_role(w: QWidget, prop: str, value: str):
    """Change a styling property and re-apply the stylesheet to w."""
    w.setProperty(prop, value)
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def label(text="", px=13, weight=QFont.Weight.Normal, role=None, display=False, tabular=False,
          wrap=False, align=None) -> QLabel:
    lab = QLabel(text)
    lab.setFont(font(px, weight, display, tabular))
    if role:
        lab.setProperty("role", role)
    if wrap:
        lab.setWordWrap(True)
    if align is not None:
        lab.setAlignment(align)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    return lab


def button(text, variant="secondary") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def animations_enabled() -> bool:
    if os.environ.get("DECANT_REDUCED_MOTION"):
        return False
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    gs = shutil.which("gsettings")
    if gs and ("GNOME" in desktop or "CINNAMON" in desktop or "BUDGIE" in desktop):
        try:
            r = subprocess.run([gs, "get", "org.gnome.desktop.interface", "enable-animations"],
                               capture_output=True, text=True, timeout=1, env=sys_env())
            if r.stdout.strip() == "false":
                return False
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        with open(os.path.expanduser("~/.config/kdeglobals")) as f:
            if re.search(r"^AnimationDurationFactor=0(\.0*)?\s*$", f.read(), re.M):
                return False
    except OSError:
        pass
    return True


# -- formatting ---------------------------------------------------------------

def fmt_elapsed(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min %d s" % (s // 60, s % 60)
    return "%d h %d min" % (s // 3600, (s % 3600) // 60)


def fmt_eta(s: float) -> str:
    if s < 45:
        return "Less than a minute left"
    if s < 90:
        return "About a minute left"
    minutes = int(round(s / 60.0))
    if minutes < 60:
        return "About %d min left" % minutes
    return "About %d h %d min left" % divmod(minutes, 60)


def elide(text: str, f: QFont, width: int) -> str:
    return QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideMiddle, width)


TRANSPORTS = {"usb": "USB", "nvme": "NVMe", "sata": "SATA", "ata": "ATA", "sas": "SAS",
              "scsi": "SCSI", "mmc": "SD", "sdio": "SD", "virtio": "virtual", "iscsi": "iSCSI"}


def drive_summary(d: dict) -> str:
    parts = [d["path"]]
    if d["tran"] in TRANSPORTS:
        parts.append(TRANSPORTS[d["tran"]])
    elif d["kind"] == "sd":
        parts.append("SD")
    if d["labels"]:
        parts.append("holds " + ", ".join(d["labels"][:3]) + ("…" if len(d["labels"]) > 3 else ""))
    return ", ".join(parts)


def same_drive(a: dict, b: dict) -> bool:
    """Same physical drive, not just the same /dev name: identical sticks swapped
    within one poll differ by serial (when the drive reports one)."""
    return (a["path"] == b["path"] and a["size"] == b["size"] and a["model"] == b["model"]
            and a.get("serial", "") == b.get("serial", ""))


# -- glyphs -------------------------------------------------------------------

def draw_glyph(p: QPainter, kind: str, c: QPointF, s: float, color: str):
    """Line glyphs, drawn rather than shipped so they scale and theme cleanly."""
    pen = QPen(QColor(color), max(1.6, s / 13.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = c.x(), c.y()

    if kind == "image":  # archive: page with folded corner and a zipper
        x0, y0, w, h, fold = cx - 0.3 * s, cy - 0.4 * s, 0.6 * s, 0.8 * s, 0.17 * s
        path = QPainterPath()
        path.moveTo(x0, y0)
        path.lineTo(x0 + w - fold, y0)
        path.lineTo(x0 + w, y0 + fold)
        path.lineTo(x0 + w, y0 + h)
        path.lineTo(x0, y0 + h)
        path.closeSubpath()
        path.moveTo(x0 + w - fold, y0)
        path.lineTo(x0 + w - fold, y0 + fold)
        path.lineTo(x0 + w, y0 + fold)
        p.drawPath(path)
        zx = cx - 0.04 * s
        for k in range(4):
            y = y0 + 0.1 * s + k * 0.09 * s
            dx = 0.07 * s if k % 2 == 0 else -0.07 * s
            p.drawLine(QPointF(zx, y), QPointF(zx + dx, y))
        p.drawRoundedRect(QRectF(zx - 0.065 * s, y0 + 0.47 * s, 0.13 * s, 0.15 * s), 0.03 * s, 0.03 * s)

    elif kind == "usb":
        p.drawRoundedRect(QRectF(cx - 0.19 * s, cy - 0.1 * s, 0.38 * s, 0.52 * s), 0.07 * s, 0.07 * s)
        p.drawRect(QRectF(cx - 0.13 * s, cy - 0.4 * s, 0.26 * s, 0.3 * s))
        p.drawLine(QPointF(cx - 0.05 * s, cy - 0.3 * s), QPointF(cx - 0.05 * s, cy - 0.22 * s))
        p.drawLine(QPointF(cx + 0.05 * s, cy - 0.3 * s), QPointF(cx + 0.05 * s, cy - 0.22 * s))

    elif kind == "sd":
        x0, y0, w, h, clip = cx - 0.27 * s, cy - 0.38 * s, 0.54 * s, 0.76 * s, 0.17 * s
        path = QPainterPath()
        path.moveTo(x0, y0)
        path.lineTo(x0 + w - clip, y0)
        path.lineTo(x0 + w, y0 + clip)
        path.lineTo(x0 + w, y0 + h)
        path.lineTo(x0, y0 + h)
        path.closeSubpath()
        p.drawPath(path)
        for i in range(3):
            x = x0 + 0.12 * s + i * 0.1 * s
            p.drawLine(QPointF(x, y0 + 0.1 * s), QPointF(x, y0 + 0.22 * s))

    elif kind in ("disk", "nvme"):
        p.drawRoundedRect(QRectF(cx - 0.38 * s, cy - 0.24 * s, 0.76 * s, 0.48 * s), 0.08 * s, 0.08 * s)
        p.drawLine(QPointF(cx - 0.26 * s, cy + 0.08 * s), QPointF(cx + 0.06 * s, cy + 0.08 * s))
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(cx + 0.22 * s, cy + 0.08 * s), 0.035 * s, 0.035 * s)

    elif kind == "pour":  # a drop
        path = QPainterPath()
        path.moveTo(cx, cy - 0.42 * s)
        path.cubicTo(cx + 0.1 * s, cy - 0.24 * s, cx + 0.3 * s, cy - 0.06 * s, cx + 0.3 * s, cy + 0.13 * s)
        path.cubicTo(cx + 0.3 * s, cy + 0.31 * s, cx + 0.16 * s, cy + 0.42 * s, cx, cy + 0.42 * s)
        path.cubicTo(cx - 0.16 * s, cy + 0.42 * s, cx - 0.3 * s, cy + 0.31 * s, cx - 0.3 * s, cy + 0.13 * s)
        path.cubicTo(cx - 0.3 * s, cy - 0.06 * s, cx - 0.1 * s, cy - 0.24 * s, cx, cy - 0.42 * s)
        p.drawPath(path)
        hl = QPainterPath()
        hl.moveTo(cx - 0.15 * s, cy + 0.1 * s)
        hl.quadTo(cx - 0.15 * s, cy + 0.26 * s, cx - 0.01 * s, cy + 0.28 * s)
        p.drawPath(hl)

    elif kind == "check":
        path = QPainterPath()
        path.moveTo(cx - 0.3 * s, cy + 0.02 * s)
        path.lineTo(cx - 0.09 * s, cy + 0.23 * s)
        path.lineTo(cx + 0.32 * s, cy - 0.21 * s)
        p.drawPath(path)

    elif kind == "alert":
        p.drawLine(QPointF(cx, cy - 0.3 * s), QPointF(cx, cy + 0.06 * s))
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy + 0.26 * s), 0.055 * s, 0.055 * s)

    elif kind == "stop":
        p.drawLine(QPointF(cx - 0.24 * s, cy), QPointF(cx + 0.24 * s, cy))


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = size / 128.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(GROUND))
        p.drawRoundedRect(QRectF(0, 0, size, size), 28 * k, 28 * k)
        circle = QPainterPath()
        circle.addEllipse(QPointF(64 * k, 64 * k), 44 * k, 44 * k)
        p.fillPath(circle, QColor(SURFACE))
        p.save()
        p.setClipPath(circle)
        p.fillRect(QRectF(0, 70 * k, size, size), QColor(AMBER_DEEP))
        p.fillRect(QRectF(0, 75 * k, size, size), QColor(AMBER))
        p.restore()
        p.setPen(QPen(QColor(FAINT), 4 * k))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(64 * k, 64 * k), 44 * k, 44 * k)
        p.end()
        icon.addPixmap(pm)
    return icon


class Glyph(QWidget):
    """A glyph in a disc. tone: idle, active, done, plain."""

    def __init__(self, kind, size=40, tone="plain", parent=None):
        super().__init__(parent)
        self.kind, self.tone = kind, tone
        self.setFixedSize(size, size)

    def set(self, kind=None, tone=None):
        if kind:
            self.kind = kind
        if tone:
            self.tone = tone
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        c = r.center()
        disabled = not self.isEnabled()
        if self.tone == "done":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(AMBER))
            p.drawEllipse(r)
            draw_glyph(p, self.kind, c, r.width() * 0.5, GROUND)
        elif self.tone == "plain":
            draw_glyph(p, self.kind, c, r.width() * 0.8, FAINT if disabled else MUTED)
        else:
            ring = AMBER if self.tone == "active" else LINE
            p.setPen(QPen(QColor(ring), 2 if self.tone == "active" else 1.5))
            p.setBrush(QColor(SURFACE))
            p.drawEllipse(r)
            draw_glyph(p, self.kind, c, r.width() * 0.5, AMBER if self.tone == "active" else FAINT)


class StepTrack(QWidget):
    """Three discs joined by lines; the line fills amber once the step before it is done."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kinds = ["image", "usb", "pour"]
        self.tones = ["active", "idle", "idle"]
        self.setFixedHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, kinds, tones):
        self.kinds, self.tones = list(kinds), list(tones)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        cy, rad = 38.0, 32.0
        xs = [w * (2 * i + 1) / 6.0 for i in range(3)]
        for i in range(2):
            pen = QPen(QColor(AMBER if self.tones[i] == "done" else LINE), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(xs[i] + rad + 12, cy), QPointF(xs[i + 1] - rad - 12, cy))
        for x, kind, tone in zip(xs, self.kinds, self.tones):
            r = QRectF(x - rad, cy - rad, 2 * rad, 2 * rad)
            if tone == "done":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(AMBER))
                p.drawEllipse(r)
                draw_glyph(p, kind, QPointF(x, cy), 30, GROUND)
            else:
                active = tone == "active"
                p.setPen(QPen(QColor(AMBER if active else LINE), 2 if active else 1.5))
                p.setBrush(QColor(SURFACE))
                p.drawEllipse(r)
                draw_glyph(p, kind, QPointF(x, cy), 30, AMBER if active else FAINT)


class Vessel(QWidget):
    """The progress readout: a round vessel that fills as the image is written,
    then turns clear from the bottom up as the read-back verifies it."""

    ACTIVE = ("preparing", "writing", "syncing", "verifying")

    def __init__(self, diameter=212, animate=True, parent=None):
        super().__init__(parent)
        self.d = diameter
        self.setFixedSize(diameter + 8, diameter + 8)
        self.state = "idle"
        self.level = self.shown_level = 0.0
        self.clear = self.shown_clear = 0.0
        self.big = ""
        self.small = ""
        self.t = 0.0
        self.animate = animate
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)

    def set_state(self, state, big=None, small=None):
        self.state = state
        if big is not None:
            self.big = big
        if small is not None:
            self.small = small
        if state in self.ACTIVE:
            self.timer.start()
        self.update()

    def set_levels(self, level=None, clear=None, snap=False):
        if level is not None:
            self.level = max(0.0, min(1.0, level))
        if clear is not None:
            self.clear = max(0.0, min(1.0, clear))
        if snap or not self.animate:
            self.shown_level, self.shown_clear = self.level, self.clear
        if not self.timer.isActive():
            self.timer.start()
        self.update()

    def _tick(self):
        self.t += 0.033
        self.shown_level += (self.level - self.shown_level) * 0.18
        self.shown_clear += (self.clear - self.shown_clear) * 0.18
        settled = (abs(self.level - self.shown_level) < 0.0005
                   and abs(self.clear - self.shown_clear) < 0.0005)
        if settled:
            self.shown_level, self.shown_clear = self.level, self.clear
            if self.state not in self.ACTIVE or not self.animate:
                self.timer.stop()
        self.update()

    def _surface(self, r: QRectF, level: float, phase: float, amp: float) -> QPainterPath:
        y0 = r.bottom() - level * r.height()
        path = QPainterPath()
        wave = r.width() * 0.8
        x = r.left() - 2
        path.moveTo(x, y0 + amp * math.sin(phase))
        while x <= r.right() + 2:
            x += 4
            path.lineTo(x, y0 + amp * math.sin(phase + 2 * math.pi * (x - r.left()) / wave))
        path.lineTo(r.right() + 2, r.bottom() + 2)
        path.lineTo(r.left() - 2, r.bottom() + 2)
        path.closeSubpath()
        return path

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(4, 4, self.d, self.d)
        circle = QPainterPath()
        circle.addEllipse(r)
        p.fillPath(circle, QColor(SURFACE))

        moving = self.animate and self.state in ("writing", "verifying", "preparing")
        lv, cl = self.shown_level, self.shown_clear

        def amp(level):
            return 0.0 if not moving else 5.0 * max(0.0, min(1.0, level * 10, (1 - level) * 10))

        front_c, back_c = QColor(AMBER), QColor(AMBER_DEEP)
        if self.state in ("error", "cancelled"):
            front_c, back_c = QColor(DANGER_DEEP), QColor("#5A2C26")

        p.save()
        p.setClipPath(circle)
        liquid = QPainterPath()
        if lv > 0.001:
            p.fillPath(self._surface(r, lv, self.t * 1.3 + 2.1, amp(lv) * 0.8), back_c)
            liquid = self._surface(r, lv, self.t * 1.9, amp(lv))
            p.fillPath(liquid, front_c)
        if cl > 0.001:
            clear = self._surface(r, cl, self.t * 1.6 + 1.0, amp(cl) * 0.9)
            p.fillPath(clear, QColor(AMBER_CLEAR))
            liquid = liquid.united(clear)
        p.restore()

        ring = AMBER if self.state == "done" else (DANGER if self.state == "error" else LINE)
        p.setPen(QPen(QColor(ring), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(r)

        # Readout in two tones: light above the liquid line, dark where it's submerged.
        inside = circle.intersected(liquid)
        above = circle.subtracted(liquid)
        for clip, color in ((above, TEXT), (inside, GROUND)):
            p.save()
            p.setClipPath(clip)
            self._draw_readout(p, r, color)
            p.restore()

    def _draw_readout(self, p: QPainter, r: QRectF, color: str):
        c = r.center()
        if self.state == "done":
            draw_glyph(p, "check", c, r.width() * 0.34, color)
            return
        if self.state == "error":
            draw_glyph(p, "alert", c, r.width() * 0.34, color)
            return
        if self.state == "cancelled":
            draw_glyph(p, "stop", c, r.width() * 0.34, color)
            return
        p.setPen(QColor(color))
        p.setFont(font(44, QFont.Weight.DemiBold, display=True, tabular=True))
        p.drawText(QRectF(r.left(), c.y() - 40, r.width(), 52), Qt.AlignmentFlag.AlignCenter, self.big)
        p.setFont(font(13, QFont.Weight.Medium))
        p.drawText(QRectF(r.left(), c.y() + 14, r.width(), 22), Qt.AlignmentFlag.AlignCenter, self.small)


# -- the privileged job --------------------------------------------------------

class FlashJob(QObject):
    phase = Signal(str, dict)
    progress = Signal(dict)
    finished = Signal(str, dict)  # "done" | "error" | "cancelled", payload

    def __init__(self, image: dict, drive: dict, verify: bool, eject: bool, parent=None):
        super().__init__(parent)
        self.image, self.drive = image, drive
        self.verify, self.eject = verify, eject
        self.proc = None
        self.buf = b""
        self.err = b""
        self.terminal = None
        self.cancel_requested = False
        self.via_pkexec = False
        self.reported = False

    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self):
        args = ["--helper", "--source", self.image["path"], "--target", self.drive["path"]]
        if self.image["decompressor"]:
            args += ["--decompressor", self.image["decompressor"]]
        if self.verify:
            args.append("--verify")
        if self.eject:
            args.append("--eject")
        if self.drive.get("test_file"):
            args.append("--allow-file")
        else:
            args += ["--expect-size", str(self.drive["size"]),
                     "--expect-serial=" + self.drive.get("serial", "")]

        cmd = self_command() + args
        if os.geteuid() == 0 or os.environ.get("DECANT_NO_ROOT"):
            program, argv = cmd[0], cmd[1:]
        else:
            pkexec = shutil.which("pkexec")
            if not pkexec:
                self._finish("error", {"code": "no_pkexec", "msg":
                             "Decant needs pkexec (polkit) to write to drives. Install polkit, "
                             "or start Decant with sudo."})
                return
            program, argv = pkexec, cmd
            self.via_pkexec = True

        self.proc = QProcess(self)
        env = QProcessEnvironment()
        for k, v in sys_env().items():
            env.insert(k, v)
        self.proc.setProcessEnvironment(env)
        self.proc.setProgram(program)
        self.proc.setArguments(argv)
        self.proc.readyReadStandardOutput.connect(self._read_out)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(self._exited)
        self.proc.errorOccurred.connect(self._proc_error)
        self.proc.start()

    def cancel(self):
        if self.running() and not self.cancel_requested:
            self.cancel_requested = True
            self.proc.write(b"cancel\n")
            self.proc.closeWriteChannel()

    def _read_err(self):
        self.err = (self.err + self.proc.readAllStandardError().data())[-4096:]

    def _read_out(self):
        self.buf += self.proc.readAllStandardOutput().data()
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("ev")
            if kind == "phase":
                self.phase.emit(ev.get("phase", ""), ev)
            elif kind == "progress":
                self.progress.emit(ev)
            elif kind in ("done", "error", "cancelled"):
                self.terminal = (kind, ev)

    def _proc_error(self, err):
        if err == QProcess.ProcessError.FailedToStart:
            self._finish("error", {"code": "start_failed",
                                   "msg": "Couldn't start the writer: %s." % self.proc.errorString()})

    def _exited(self, code, _status):
        self._read_out()
        self._read_err()
        if self.terminal:
            self._finish(*self.terminal)
            return
        if self.via_pkexec and code == 126:
            self._finish("error", {"code": "auth_cancelled",
                                   "msg": "Nothing was written: the password prompt was dismissed."})
            return
        if self.via_pkexec and code == 127:
            self._finish("error", {"code": "auth_failed", "msg":
                                   "Authorization failed, so nothing was written. Decant asks for your "
                                   "password through polkit; check that your desktop's authentication "
                                   "agent is running."})
            return
        tail = self.err.decode("utf-8", "replace").strip().splitlines()
        if tail:
            why = ": " + tail[-1]
        elif _status == QProcess.ExitStatus.CrashExit:
            why = " (it was terminated)."
        else:
            why = " (exit status %d)." % code
        self._finish("error", {"code": "crashed", "msg": "The writer stopped unexpectedly" + why})

    def _finish(self, kind, payload):
        if self.reported:
            return
        self.reported = True
        self.terminal = (kind, payload)
        self.finished.emit(kind, payload)


# -- dialogs -------------------------------------------------------------------

class DriveRow(QWidget):
    def __init__(self, d: dict, block: str | None, warn: str | None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 16, 10)
        h.setSpacing(14)
        h.addWidget(Glyph(d["kind"], 34, "plain"))
        text = QVBoxLayout()
        text.setSpacing(2)
        name = label(d["model"], 14, QFont.Weight.DemiBold)
        info = label(drive_summary(d), 12, role="muted")
        text.addWidget(name)
        text.addWidget(info)
        h.addLayout(text, 1)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size = label(fmt_bytes(d["size"]), 14, QFont.Weight.Medium, tabular=True,
                     align=Qt.AlignmentFlag.AlignRight)
        right.addWidget(size)
        badge_text, tone = (block, "block") if block else (warn, "warn")
        if badge_text:
            badge = label(badge_text, 11, QFont.Weight.DemiBold)
            badge.setObjectName("badge")
            badge.setProperty("tone", tone)
            right.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
        h.addLayout(right)
        if block:
            self.setEnabled(False)
            self.setToolTip(block)
        elif d["mounts"]:
            self.setToolTip("Mounted at %s. Decant unmounts it before writing." % ", ".join(d["mounts"]))


class DrivePicker(QDialog):
    def __init__(self, parent, current: dict | None, min_size: int | None, settings: QSettings):
        super().__init__(parent)
        self.setWindowTitle("Choose a drive")
        self.setModal(True)
        self.resize(580, 440)
        self.settings = settings
        self.min_size = min_size
        self.selected = None
        self.drives = []
        self._key = None
        self._want = current  # a drive dict; matched with same_drive, not by path

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(12)
        v.addWidget(label("Choose a drive", 20, QFont.Weight.DemiBold, display=True))
        v.addWidget(label("Everything on the drive you choose will be replaced.", 13, role="muted"))

        self.list = QListWidget()
        self.list.setObjectName("drives")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self._choose())
        v.addWidget(self.list, 1)

        self.empty = label("Plug in a USB drive or SD card. It shows up here on its own.", 14,
                           role="muted", wrap=True, align=Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        v.addWidget(self.empty, 1)

        bottom = QHBoxLayout()
        self.show_internal = QCheckBox()
        self.show_internal.setChecked(settings.value("show_internal", False, type=bool))
        self.show_internal.toggled.connect(self._toggle_internal)
        bottom.addWidget(self.show_internal)
        bottom.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = button("Use this drive", "primary")
        self.ok.clicked.connect(self._choose)
        bottom.addWidget(cancel)
        bottom.addWidget(self.ok)
        v.addLayout(bottom)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def _toggle_internal(self, on):
        self.settings.setValue("show_internal", on)
        self._key = None
        self.refresh()

    def _reasons(self, d):
        block = drive_block_reason(d)
        if not block and self.min_size and d["size"] < self.min_size:
            block = "Too small"
        warn = None
        if not d["removable"]:
            warn = "Internal"
        elif d["large"]:
            warn = "Large drive"
        return block, warn

    def refresh(self):
        drives = list_drives()
        key = json.dumps(drives, sort_keys=True) + str(self.show_internal.isChecked())
        if key == self._key:
            return
        self._key = key
        cur = self.list.currentItem()
        if cur is not None and cur.isSelected():
            path = cur.data(Qt.ItemDataRole.UserRole)
            self._want = next((d for d in self.drives if d["path"] == path), None)

        self.drives = drives
        hidden = [d for d in drives if not d["removable"]]
        self.show_internal.setText("Show internal drives (%d)" % len(hidden))
        self.show_internal.setVisible(bool(hidden))
        show_internal = self.show_internal.isChecked()

        self.list.clear()
        order = sorted(drives, key=lambda d: (not d["removable"], bool(self._reasons(d)[0]), d["path"]))
        for d in order:
            if not d["removable"] and not show_internal:
                continue
            block, warn = self._reasons(d)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, d["path"])
            row = DriveRow(d, block, warn)
            item.setSizeHint(QSize(0, max(64, row.sizeHint().height())))
            if block:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            if not block and self._want and same_drive(d, self._want):
                self.list.setCurrentItem(item)
        has_rows = self.list.count() > 0
        self.list.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        if not has_rows and hidden and not show_internal:
            self.empty.setText("No USB drives or SD cards found. Plug one in, or show internal drives below.")
        else:
            self.empty.setText("Plug in a USB drive or SD card. It shows up here on its own.")
        self._sync_buttons()

    def _sync_buttons(self):
        items = self.list.selectedItems()
        self.ok.setEnabled(bool(items))

    def _choose(self):
        items = self.list.selectedItems()
        if not items:
            return
        path = items[0].data(Qt.ItemDataRole.UserRole)
        for d in self.drives:
            if d["path"] == path and not self._reasons(d)[0]:
                self.selected = d
                self.accept()
                return


class ConfirmDialog(QDialog):
    def __init__(self, parent, image: dict, d: dict):
        super().__init__(parent)
        self.setWindowTitle("Erase drive?")
        self.setModal(True)
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        v.setContentsMargins(26, 24, 26, 20)
        v.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(Glyph(d["kind"], 40, "plain"), 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Erase %s?" % d["model"], 19, QFont.Weight.DemiBold, display=True, wrap=True))
        titles.addWidget(label("%s, %s" % (fmt_bytes(d["size"]), d["path"]), 13, role="muted",
                               tabular=True))
        head.addLayout(titles, 1)
        v.addLayout(head)
        v.addSpacing(4)

        lines = ["Everything on it will be replaced with %s." % image["name"]]
        if d["labels"]:
            lines.append("It currently holds %s." % ", ".join(d["labels"][:4]))
        if d["mounts"]:
            lines.append("Decant will unmount %s first." % ", ".join(d["mounts"][:3]))
        v.addWidget(label(" ".join(lines), 14, wrap=True))

        self.ack = None
        if not d["removable"]:
            v.addSpacing(4)
            v.addWidget(label("This is an internal drive, not a USB stick or SD card.", 13,
                              QFont.Weight.Medium, role="warn", wrap=True))
            self.ack = QCheckBox("I've checked this is the drive I mean to erase")
            v.addWidget(self.ack)
        elif d["large"]:
            v.addSpacing(4)
            v.addWidget(label("This drive is bigger than most USB sticks. Check it's the one you mean.",
                              13, QFont.Weight.Medium, role="warn", wrap=True))

        v.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        self.go = button("Erase and flash", "danger")
        self.go.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.go)
        v.addLayout(row)
        if self.ack is not None:
            self.go.setEnabled(False)
            self.ack.toggled.connect(self.go.setEnabled)
        cancel.setDefault(True)
        cancel.setFocus()


# -- main window ---------------------------------------------------------------

class StepColumn(QWidget):
    """One step: a button until something is chosen, then what was chosen."""

    WIDTH = 230

    def __init__(self, button_text, variant="secondary", parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 0, 8, 0)
        v.setSpacing(4)
        v.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        center = Qt.AlignmentFlag.AlignHCenter
        self.button = button(button_text, variant)
        self.name = label("", 15, QFont.Weight.DemiBold, display=True, align=center)
        self.detail = label("", 13, role="muted", tabular=True, align=center)
        self.note = label("", 12, QFont.Weight.Medium, role="warn", align=center, wrap=True)
        self.note.setMaximumWidth(self.WIDTH)
        self.change = button("Change", "link")
        v.addWidget(self.button, 0, center)
        v.addWidget(self.name, 0, center)
        v.addWidget(self.detail, 0, center)
        v.addWidget(self.note, 0, center)
        v.addSpacing(2)
        v.addWidget(self.change, 0, center)

    def show_button(self, enabled=True, variant=None, caption=""):
        self.button.setEnabled(enabled)
        if variant and self.button.objectName() != variant:
            self.button.setObjectName(variant)
            set_role(self.button, "variant", variant)
        self.button.show()
        self.name.hide()
        self.change.hide()
        self.detail.setText(caption)
        self.detail.setVisible(bool(caption))
        self.note.hide()

    def show_choice(self, name, detail, note="", tooltip=""):
        self.button.hide()
        self.name.setText(elide(name, self.name.font(), self.WIDTH))
        self.name.setToolTip(tooltip or name)
        self.name.show()
        self.detail.setText(detail)
        self.detail.show()
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.change.show()


class MainWindow(QMainWindow):
    def __init__(self, initial_image=None):
        super().__init__()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.image = None
        self.drive = None
        self.job = None
        self.last_phase = ""
        self.wrote_any = False
        self.allow_file = bool(os.environ.get("DECANT_ALLOW_FILE"))
        self.motion = animations_enabled()

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(800, 500)
        self.resize(880, 540)
        self.setAcceptDrops(True)

        root = QWidget()
        root.setObjectName("ground")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 18, 28, 16)
        outer.setSpacing(0)

        header = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(22, 22))
        header.addWidget(mark)
        header.addSpacing(8)
        header.addWidget(label("decant", 19, QFont.Weight.Bold, display=True))
        header.addStretch(1)
        self.options = QToolButton()
        self.options.setObjectName("link")
        self.options.setText("Options")
        self.options.setCursor(Qt.CursorShape.PointingHandCursor)
        self.options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.options.setMenu(self._build_menu())
        header.addWidget(self.options)
        outer.addLayout(header)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_setup())
        self.stack.addWidget(self._build_flashing())
        self.stack.addWidget(self._build_result())

        self.footer = label("", 12, role="faint", align=Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.footer)
        self.notice_timer = QTimer(self)
        self.notice_timer.setSingleShot(True)
        self.notice_timer.timeout.connect(self._reset_footer)

        self.watch = QTimer(self)
        self.watch.setInterval(2000)
        self.watch.timeout.connect(self._watch_drives)
        self.watch.start()

        self._update_setup()
        self._reset_footer()
        if initial_image:
            QTimer.singleShot(0, lambda: self.set_image(initial_image))

    # -- options ------------------------------------------------------------
    def _build_menu(self) -> QMenu:
        menu = QMenu(self)

        def toggle(text, key, default, tip=""):
            act = QAction(text, menu)
            act.setCheckable(True)
            act.setChecked(self.settings.value(key, default, type=bool))
            act.toggled.connect(lambda on: self._set_option(key, on))
            if tip:
                act.setToolTip(tip)
            menu.addAction(act)
            return act

        toggle("Verify after writing", "verify", True)
        toggle("Eject when finished", "eject", True)
        menu.addSeparator()
        par = toggle("Use lbzip2 for bzip2 images", "parallel", True)
        if not which_safe("lbzip2"):
            par.setText("Use lbzip2 for bzip2 images (not installed)")
            par.setEnabled(False)
        menu.addSeparator()
        add = QAction("Add Decant to the application menu", menu)
        add.triggered.connect(self._install)
        menu.addAction(add)
        menu.setToolTipsVisible(True)
        return menu

    def _set_option(self, key, on):
        self.settings.setValue(key, on)
        if key == "parallel" and self.image:
            self.set_image(self.image["path"], quiet=True)
        self._update_setup()

    def opt(self, key, default=True) -> bool:
        return self.settings.value(key, default, type=bool)

    def _install(self):
        try:
            install_desktop_main()
            self.notice("Decant is now in your application menu.")
        except OSError as e:
            self.notice("Couldn't add Decant to the menu: %s." % e.strerror, "danger")

    # -- footer -------------------------------------------------------------
    def notice(self, text, role="muted", ms=6000):
        self.footer.setText(text)
        set_role(self.footer, "role", role)
        self.notice_timer.start(ms)

    def _reset_footer(self):
        page = self.stack.currentIndex()
        text = ""
        if page == 0 and not self.image:
            text = "Or drop an image file anywhere in this window."
        elif page == 1:
            text = "Keep the drive plugged in until Decant says it's done."
        self.footer.setText(text)
        set_role(self.footer, "role", "faint")

    # -- setup page ---------------------------------------------------------
    def _build_setup(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.addStretch(3)
        self.track = StepTrack()
        v.addWidget(self.track)
        v.addSpacing(18)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.col_image = StepColumn("Choose image", "primary")
        self.col_drive = StepColumn("Choose drive")
        self.col_flash = StepColumn("Flash", "primary")
        for col in (self.col_image, self.col_drive, self.col_flash):
            row.addWidget(col, 1)
        v.addLayout(row)
        v.addStretch(4)
        self.col_image.button.clicked.connect(self.pick_image)
        self.col_image.change.clicked.connect(self.pick_image)
        self.col_drive.button.clicked.connect(self.pick_drive)
        self.col_drive.change.clicked.connect(self.pick_drive)
        self.col_flash.button.clicked.connect(self.start_flash)
        return page

    def _update_setup(self):
        img, drv = self.image, self.drive
        if img:
            exp = (", expands to %s" % fmt_bytes(img["usize"])) if img["usize"] and img["fmt"] != "raw" else ""
            via = ""
            if img["decompressor"]:
                via = "Decompressed with %s" % img["decompressor"]
            self.col_image.show_choice(img["name"], "%s %s%s" % (fmt_bytes(img["size"]),
                                       FORMAT_LABELS[img["fmt"]], exp), tooltip=img["path"] +
                                       ("\n" + via if via else ""))
        else:
            self.col_image.show_button(True, "primary")

        if drv:
            note = ""
            if not drv["removable"]:
                note = "Internal drive"
            elif drv["large"]:
                note = "Larger than most USB drives"
            self.col_drive.show_choice(drv["model"], "%s, %s" % (fmt_bytes(drv["size"]), drv["path"]),
                                       note)
        else:
            self.col_drive.show_button(bool(img), "primary" if img else "secondary")

        ready = bool(img and drv)
        caption = ""
        if ready:
            caption = "Then reads it back to verify" if self.opt("verify") else "Verification is off"
        self.col_flash.show_button(ready, "primary", caption)

        drive_kind = drv["kind"] if drv else "usb"
        if drive_kind == "nvme":
            drive_kind = "disk"
        tones = ["done" if img else "active",
                 "done" if drv else ("active" if img else "idle"),
                 "active" if ready else "idle"]
        self.track.set_state(["image", drive_kind, "pour"], tones)
        # Don't wipe a notice that's still on screen (e.g. "was unplugged").
        if self.stack.currentIndex() == 0 and not self.notice_timer.isActive():
            self._reset_footer()

    def pick_image(self):
        start = self.settings.value("last_dir", "") or os.path.expanduser("~/Downloads")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an image", start,
            "Disk images (*.bz2 *.tbz2 *.xz *.gz *.zst *.img *.iso *.raw);;All files (*)")
        if path:
            self.set_image(path)

    def set_image(self, path, quiet=False):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            self.notice("%s isn't a file." % os.path.basename(path), "danger")
            return
        fmt = detect_format(path)
        name = os.path.basename(path)
        if not fmt:
            self.notice("Decant can't read %s. Choose a .bz2, .xz, .gz, .zst, .img or .iso image." % name,
                        "danger", 9000)
            return
        try:
            dec = pick_decompressor(fmt, bzcat_only=not self.opt("parallel"))
        except LookupError as e:
            self.notice("%s needs %s, which isn't installed." % (name, e.args[0]), "danger", 9000)
            return
        self.settings.setValue("last_dir", os.path.dirname(path))
        self.image = {"path": path, "name": name, "fmt": fmt, "decompressor": dec,
                      "size": os.path.getsize(path), "usize": uncompressed_size(path, fmt)}
        if self.drive and self.image["usize"] and self.drive["size"] < self.image["usize"]:
            self.notice("%s is too small for this image, so it was deselected." % self.drive["model"], "warn")
            self.drive = None
        self._update_setup()
        if not quiet:
            self._autoselect()

    def _autoselect(self):
        if not self.image or self.drive:
            return
        need = self.image["usize"] or 0
        fits = [d for d in list_drives() if d["removable"] and not d["large"]
                and not drive_block_reason(d) and d["size"] >= need]
        if len(fits) == 1:
            self.drive = fits[0]
            self._update_setup()
            self.notice("Chose %s, the only USB drive or SD card plugged in." % self.drive["model"])

    def pick_drive(self):
        if not self.image:
            return
        if self.allow_file and QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            path, _ = QFileDialog.getSaveFileName(self, "Test target file", os.path.expanduser("~/decant-test.img"))
            if path:
                open(path, "ab").close()
                self.drive = {"path": path, "name": os.path.basename(path), "size": 0, "model": "Test file",
                              "serial": "",
                              "tran": "", "kind": "disk", "removable": True, "readonly": False,
                              "mounts": [], "labels": [], "system": None, "holders": [], "large": False,
                              "test_file": True}
                self._update_setup()
            return
        dlg = DrivePicker(self, self.drive, self.image["usize"], self.settings)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        dlg.timer.stop()  # otherwise it keeps running lsblk after closing
        dlg.deleteLater()
        if accepted and dlg.selected:
            self.drive = dlg.selected
            self._update_setup()

    def _watch_drives(self):
        if self.stack.currentIndex() != 0 or not self.drive or self.drive.get("test_file"):
            return
        now = {d["path"]: d for d in list_drives()}
        cur = now.get(self.drive["path"])
        if not cur or not same_drive(cur, self.drive):
            self.notice("%s was unplugged." % self.drive["model"], "warn")
            self.drive = None
            self._update_setup()
        elif cur != self.drive:
            self.drive = cur
            self._update_setup()

    # -- flashing -----------------------------------------------------------
    def _build_flashing(self) -> QWidget:
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(24, 0, 24, 0)
        h.setSpacing(40)

        left = QVBoxLayout()
        left.setSpacing(0)
        left.addStretch(1)
        self.sum_image = self._summary_row("image")
        left.addLayout(self.sum_image["layout"])
        stem = QHBoxLayout()
        stem.setContentsMargins(19, 0, 0, 0)
        line = QWidget()
        line.setFixedSize(2, 30)
        line.setStyleSheet("background: %s;" % AMBER)
        stem.addWidget(line)
        stem.addStretch(1)
        left.addLayout(stem)
        self.sum_drive = self._summary_row("usb")
        left.addLayout(self.sum_drive["layout"])
        left.addStretch(1)
        h.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(6)
        right.addStretch(1)
        self.vessel = Vessel(212, self.motion)
        right.addWidget(self.vessel, 0, Qt.AlignmentFlag.AlignHCenter)
        right.addSpacing(10)
        center = Qt.AlignmentFlag.AlignHCenter
        self.phase_label = label("", 17, QFont.Weight.DemiBold, display=True, align=center)
        self.speed_label = label("", 13, role="muted", tabular=True, align=center)
        self.eta_label = label("", 13, role="muted", tabular=True, align=center)
        right.addWidget(self.phase_label, 0, center)
        right.addWidget(self.speed_label, 0, center)
        right.addWidget(self.eta_label, 0, center)
        right.addSpacing(8)
        self.cancel_btn = button("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_flash)
        right.addWidget(self.cancel_btn, 0, center)
        right.addStretch(1)
        h.addLayout(right, 1)
        return page

    def _summary_row(self, kind):
        row = QHBoxLayout()
        row.setSpacing(14)
        glyph = Glyph(kind, 40, "done")
        row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = label("", 15, QFont.Weight.DemiBold, display=True)
        detail = label("", 13, role="muted", tabular=True)
        text.addWidget(name)
        text.addWidget(detail)
        row.addLayout(text, 1)
        return {"layout": row, "glyph": glyph, "name": name, "detail": detail}

    def start_flash(self):
        if not (self.image and self.drive):
            return
        if not self.drive.get("test_file"):
            fresh = {d["path"]: d for d in list_drives()}.get(self.drive["path"])
            if not fresh or not same_drive(fresh, self.drive):
                self.notice("%s isn't plugged in any more. Choose the drive again." % self.drive["model"], "warn")
                self.drive = None
                self._update_setup()
                return
            reason = drive_block_reason(fresh)
            if reason:
                self.notice("Can't use %s: %s." % (fresh["model"], reason.lower()), "danger")
                return
            self.drive = fresh
        if not os.path.isfile(self.image["path"]):
            self.notice("%s has moved or been deleted." % self.image["name"], "danger")
            return
        # Hold hotplug polling while the dialog is up; it would otherwise swap or
        # clear self.drive underneath the confirmation.
        drv = self.drive
        self.watch.stop()
        dlg = ConfirmDialog(self, self.image, drv)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        dlg.deleteLater()
        self.watch.start()
        if not accepted or self.drive is not drv:
            return
        fresh = None if drv.get("test_file") else {d["path"]: d for d in list_drives()}.get(drv["path"])
        if not drv.get("test_file") and (not fresh or not same_drive(fresh, drv)):
            self.notice("%s was unplugged. Choose the drive again." % drv["model"], "warn")
            self.drive = None
            self._update_setup()
            return

        self._enter_flashing()
        self.job = FlashJob(self.image, self.drive, self.opt("verify"), self.opt("eject"), self)
        self.job.phase.connect(self._on_phase)
        self.job.progress.connect(self._on_progress)
        self.job.finished.connect(self._on_finished)
        self.job.start()

    def _enter_flashing(self):
        """Switch to the progress page for the current image and drive."""
        for row, title, detail, kind in (
            (self.sum_image, self.image["name"], "%s %s" % (fmt_bytes(self.image["size"]),
                                                            FORMAT_LABELS[self.image["fmt"]]), "image"),
            (self.sum_drive, self.drive["model"], "%s, %s" % (fmt_bytes(self.drive["size"]),
                                                              self.drive["path"]), self.drive["kind"]),
        ):
            row["name"].setText(elide(title, row["name"].font(), 300))
            row["name"].setToolTip(title)
            row["detail"].setText(detail)
            row["glyph"].set(kind="disk" if kind == "nvme" else kind)

        self.vessel.set_levels(0, 0, snap=True)
        self.vessel.set_state("preparing", "0%", "written")
        root = os.geteuid() == 0 or os.environ.get("DECANT_NO_ROOT")
        self.phase_label.setText("Starting" if root else "Waiting for your password")
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.options.setEnabled(False)
        self.stack.setCurrentIndex(1)
        self._reset_footer()
        self._reset_rate()
        self.last_phase = "preparing"
        self.wrote_any = False

    def cancel_flash(self):
        if self.job and self.job.running():
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Stopping…")
            self.job.cancel()

    def _reset_rate(self):
        self._prev = None
        self._rate = None
        self._frac_rate = None
        self._phase_t0 = None

    def _on_phase(self, phase, ev):
        self.last_phase = phase
        titles = {"preparing": "Starting", "unmounting": "Unmounting", "writing": "Writing",
                  "syncing": "Finishing writes", "verifying": "Verifying", "ejecting": "Ejecting"}
        self.phase_label.setText(titles.get(phase, phase.capitalize()))
        if phase == "unmounting":
            self.eta_label.setText(ev.get("detail", ""))
        elif phase == "writing":
            self.vessel.set_state("writing", "0%", "written")
            self._reset_rate()
        elif phase == "syncing":
            self.vessel.set_levels(1.0)
            self.vessel.set_state("syncing", "100%", "written")
            self.speed_label.setText("")
            self.eta_label.setText("Flushing the last data to the drive")
        elif phase == "verifying":
            self.vessel.set_levels(1.0, 0.0)
            self.vessel.set_state("verifying", "0%", "verified")
            self.speed_label.setText("")
            self.eta_label.setText("")
            self._reset_rate()
        elif phase == "ejecting":
            self.speed_label.setText("")
            self.eta_label.setText("")

    def _on_progress(self, ev):
        t = ev.get("t", 0.0)
        if ev.get("phase") == "writing":
            total = ev.get("in_total") or 1
            frac = min(1.0, ev.get("in", 0) / float(total))
            done = ev.get("out", 0)
            if done:
                self.wrote_any = True
            self.vessel.set_levels(level=frac)
            self.vessel.set_state("writing", "%d%%" % int(frac * 100), "written")
        else:
            total = ev.get("total") or 1
            done = ev.get("done", 0)
            frac = min(1.0, done / float(total))
            self.vessel.set_levels(clear=frac)
            self.vessel.set_state("verifying", "%d%%" % int(frac * 100), "verified")

        if self._phase_t0 is None:
            self._phase_t0 = t
        if self._prev is not None:
            dt = t - self._prev[0]
            if dt >= 0.15:
                rate = (done - self._prev[1]) / dt
                frate = (frac - self._prev[2]) / dt
                a = 0.25
                self._rate = rate if self._rate is None else (1 - a) * self._rate + a * rate
                self._frac_rate = frate if self._frac_rate is None else (1 - a) * self._frac_rate + a * frate
                self._prev = (t, done, frac)
        else:
            self._prev = (t, done, frac)

        if self._rate is not None and self._rate > 0:
            self.speed_label.setText("%s/s" % fmt_bytes(self._rate))
        if t - self._phase_t0 < 3 or not self._frac_rate or self._frac_rate <= 0:
            self.eta_label.setText("Estimating time left")
        else:
            self.eta_label.setText(fmt_eta((1.0 - frac) / self._frac_rate))

    def _on_finished(self, kind, ev):
        self.options.setEnabled(True)
        self.vessel.set_state("idle")  # stops its animation timer on the hidden page
        job, self.job = self.job, None
        if job is not None:
            job.deleteLater()
        code = ev.get("code", "")
        if kind == "cancelled" and not self.wrote_any:
            self.stack.setCurrentIndex(0)
            self.notice("Cancelled before anything was written. The drive is unchanged.", "muted")
            return
        if kind == "error" and code in ("auth_cancelled", "auth_failed", "no_pkexec"):
            self.vessel.set_state("idle")
            self.stack.setCurrentIndex(0)
            self.notice(ev.get("msg", ""), "warn" if code == "auth_cancelled" else "danger", 10000)
            return
        if kind == "error" and code in ("busy", "system_drive", "no_target", "bad_target", "readonly"):
            # Nothing was written; let them fix it from the setup page.
            self.stack.setCurrentIndex(0)
            self.notice(ev.get("msg", ""), "danger", 12000)
            if code in ("no_target", "bad_target", "system_drive"):
                self.drive = None
                self._update_setup()
            return
        self._show_result(kind, ev)

    # -- result -------------------------------------------------------------
    def _build_result(self) -> QWidget:
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(40, 0, 40, 0)
        h.setSpacing(44)
        h.addStretch(1)
        self.result_vessel = Vessel(180, animate=False)
        h.addWidget(self.result_vessel, 0, Qt.AlignmentFlag.AlignVCenter)
        right = QVBoxLayout()
        right.setSpacing(10)
        right.addStretch(1)
        self.result_title = label("", 26, QFont.Weight.DemiBold, display=True)
        self.result_body = label("", 14, wrap=True)
        self.result_body.setMaximumWidth(400)
        self.result_body.setMinimumWidth(320)
        self.result_note = label("", 13, role="muted", wrap=True)
        self.result_note.setMaximumWidth(400)
        right.addWidget(self.result_title)
        right.addWidget(self.result_body)
        right.addWidget(self.result_note)
        right.addSpacing(12)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.result_primary = button("", "primary")
        self.result_secondary = button("")
        self.result_primary.clicked.connect(lambda: self._result_action(self._primary_action))
        self.result_secondary.clicked.connect(lambda: self._result_action(self._secondary_action))
        row.addWidget(self.result_primary)
        row.addWidget(self.result_secondary)
        row.addStretch(1)
        right.addLayout(row)
        right.addStretch(1)
        h.addLayout(right)
        h.addStretch(1)
        self._primary_action = self._secondary_action = "restart"
        return page

    def _show_result(self, kind, ev):
        rv = self.result_vessel
        img, drv = self.image, self.drive
        if kind == "done":
            rv.set_levels(1.0, 1.0 if ev.get("verified") else 0.0, snap=True)
            rv.set_state("done")
            secs = ev.get("write_s") or 0.001
            nbytes = ev.get("bytes", 0)
            self.result_title.setText("Flashed and verified" if ev.get("verified") else "Flashed")
            body = "Wrote %s to %s in %s, averaging %s/s." % (
                fmt_bytes(nbytes), drv["model"], fmt_elapsed(secs), fmt_bytes(nbytes / secs))
            if ev.get("verified"):
                body += " Reading it back matched the image exactly."
            self.result_body.setText(body)
            self.result_note.setText("The drive is ejected. You can unplug it." if ev.get("ejected")
                                     else "Everything is written to the drive. You can unplug it.")
            self._set_result_buttons(("Flash another drive", "again_new_drive"), ("Choose a new image", "restart"))
        elif kind == "cancelled":
            rv.set_levels(self.vessel.level, 0.0, snap=True)
            rv.set_state("cancelled")
            if self.last_phase == "verifying":
                self.result_title.setText("Verification cancelled")
                self.result_body.setText("The image was written to %s in full, but not checked." % drv["model"])
                self.result_note.setText("")
            else:
                self.result_title.setText("Flash cancelled")
                self.result_body.setText("%s is partly written and won't boot. Flash it again before "
                                         "using it." % drv["model"])
                self.result_note.setText("")
            self._set_result_buttons(("Flash again", "retry"), ("Start over", "restart"))
        else:
            code = ev.get("code", "")
            rv.set_levels(max(self.vessel.level, 0.15), 0.0, snap=True)
            rv.set_state("error")
            titles = {"too_small": "Image doesn't fit", "verify_mismatch": "Verification failed",
                      "corrupt": "Image is damaged", "io_error": "Drive error",
                      "verify_short": "Verification failed", "verify_read": "Verification failed",
                      "no_source": "Image unreadable", "read_failed": "Image unreadable",
                      "no_decompressor": "Missing decompressor"}
            self.result_title.setText(titles.get(code, "Flash failed"))
            self.result_body.setText(ev.get("msg", "Something went wrong."))
            note = ""
            if code == "verify_mismatch":
                note = "Try flashing again. If it fails at the same place, the drive is probably faulty."
            elif code == "corrupt":
                note = "Download the image again, and check its checksum if one is published."
            self.result_note.setText(note)
            if code == "too_small":
                self._set_result_buttons(("Choose another drive", "again_new_drive"), ("Start over", "restart"))
            elif code == "corrupt":
                self._set_result_buttons(("Choose another image", "restart"), ("Flash again", "retry"))
            else:
                self._set_result_buttons(("Flash again", "retry"), ("Start over", "restart"))
        self.result_note.setVisible(bool(self.result_note.text()))
        self.stack.setCurrentIndex(2)
        self._reset_footer()
        self.result_primary.setFocus()

    def _set_result_buttons(self, primary, secondary):
        self.result_primary.setText(primary[0])
        self._primary_action = primary[1]
        self.result_secondary.setText(secondary[0])
        self._secondary_action = secondary[1]

    def _result_action(self, action):
        if action == "retry":
            self.stack.setCurrentIndex(0)
            self._update_setup()
            self.start_flash()
            return
        if action == "again_new_drive":
            self.drive = None
        else:
            self.image = None
            self.drive = None
        self.stack.setCurrentIndex(0)
        self._update_setup()
        if action == "again_new_drive":
            self.pick_drive()

    # -- window events ------------------------------------------------------
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if self.stack.currentIndex() != 1 and md.hasUrls() and md.urls()[0].isLocalFile():
            e.acceptProposedAction()
            self.footer.setText("Drop to use %s" % os.path.basename(md.urls()[0].toLocalFile()))
            set_role(self.footer, "role", "warn")

    def dragLeaveEvent(self, e):
        self._reset_footer()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if not urls:
            return
        e.acceptProposedAction()
        self._reset_footer()
        if self.stack.currentIndex() == 2:
            self.stack.setCurrentIndex(0)
            self.drive = None
        self.set_image(urls[0].toLocalFile())

    def closeEvent(self, e):
        if self.job and self.job.running():
            box = QMessageBox(self)
            box.setWindowTitle("Stop flashing?")
            box.setText("Decant is still writing to %s. If you quit now, the drive won't be usable "
                        "until you flash it again." % self.drive["model"])
            keep = box.addButton("Keep flashing", QMessageBox.ButtonRole.RejectRole)
            stop = box.addButton("Stop and quit", QMessageBox.ButtonRole.DestructiveRole)
            keep.setObjectName("secondary")
            stop.setObjectName("danger")
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is not stop:
                e.ignore()
                return
            # The flash may have finished while the box was open.
            job = self.job
            if job and job.running():
                job.cancel()
                job.proc.waitForFinished(8000)
        e.accept()


# -- demo screens (README images and a CI smoke test) --------------------------

DEMO_IMAGE = {"path": "/home/demo/Downloads/server-arm64.img.bz2", "name": "server-arm64.img.bz2",
              "fmt": "bzip2", "decompressor": "lbzip2", "size": 1183000000, "usize": None}

DEMO_DRIVES = [
    {"name": "sdb", "path": "/dev/sdb", "size": 32010928128, "vendor": "Generic", "model": "Flash Disk",
     "serial": "0123456789", "tran": "usb", "rm": True, "hotplug": True, "type": "disk", "ro": False,
     "mountpoints": [None], "children": [
         {"name": "sdb1", "type": "part", "label": "BOOT", "mountpoints": ["/run/media/demo/BOOT"]},
         {"name": "sdb2", "type": "part", "label": "rootfs", "mountpoints": [None]}]},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 63864569856, "tran": None, "rm": False,
     "hotplug": False, "type": "disk", "ro": False, "mountpoints": [None]},
    {"name": "sdc", "path": "/dev/sdc", "size": 1000204886016, "vendor": "External", "model": "SSD",
     "tran": "usb", "rm": False, "hotplug": True, "type": "disk", "ro": False, "mountpoints": [None],
     "children": [{"name": "sdc1", "type": "part", "label": "Backups",
                   "mountpoints": ["/run/media/demo/Backups"]}]},
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 2000398934016, "model": "NVMe SSD 2TB",
     "tran": "nvme", "rm": False, "hotplug": False, "type": "disk", "ro": False, "mountpoints": [None],
     "children": [{"name": "nvme0n1p1", "type": "part", "mountpoints": ["/boot/efi"]},
                  {"name": "nvme0n1p2", "type": "part", "mountpoints": ["/"]}]},
    {"name": "sda", "path": "/dev/sda", "size": 4000787030016, "vendor": "ATA", "model": "HDD 4TB",
     "tran": "sata", "rm": False, "hotplug": False, "type": "disk", "ro": False, "mountpoints": [None],
     "children": [{"name": "sda1", "type": "part", "mountpoints": ["/srv/media"]}]},
]


def render_screenshots(outdir: str) -> int:
    """Render each screen with demo data to PNGs. Never touches real drives or settings."""
    global list_drives
    import tempfile
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="decant-demo-")
    demo = [describe_drive(n) for n in DEMO_DRIVES]
    list_drives = lambda: [dict(d) for d in demo]  # noqa: E731  (demo data only)

    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(APP_NAME)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    init_fonts()
    apply_theme(app)
    print("fonts: display=%s text=%s" % (DISPLAY_FAMILY, TEXT_FAMILY))

    def shot(widget, name):
        for _ in range(6):
            app.processEvents()
        path = os.path.join(outdir, name + ".png")
        if not widget.grab().save(path):
            raise RuntimeError("couldn't save " + path)
        print(path)

    win = MainWindow()
    win.resize(880, 540)
    win.show()
    shot(win, "01-start")

    win.image = dict(DEMO_IMAGE)
    win.drive = demo[0]
    win._update_setup()
    shot(win, "02-ready")

    picker = DrivePicker(win, win.drive, None, win.settings)
    picker.show_internal.setChecked(True)
    picker.resize(600, 500)
    picker.show()
    shot(picker, "03-drives")
    picker.timer.stop()
    picker.close()

    confirm = ConfirmDialog(win, win.image, demo[0])
    confirm.show()
    shot(confirm, "04-confirm")
    confirm.close()

    usize, write_secs = 3200000000, 120.0
    win._enter_flashing()
    win._on_phase("writing", {"in_total": DEMO_IMAGE["size"]})
    for i in range(145):
        t = i * 0.5
        frac = t / write_secs
        win._on_progress({"phase": "writing", "t": t, "in": int(frac * DEMO_IMAGE["size"]),
                          "in_total": DEMO_IMAGE["size"], "out": int(frac * usize)})
    win.vessel.set_levels(level=72 * 0.5 / write_secs, snap=True)
    win.vessel.t = 1.3
    shot(win, "05-writing")

    win._on_phase("syncing", {})
    win._on_phase("verifying", {"total": usize})
    for i in range(35):
        t = 130 + i * 0.5
        done = int((t - 130) / 40.0 * usize)
        win._on_progress({"phase": "verifying", "t": t, "done": done, "total": usize})
    win.vessel.set_levels(1.0, 17.0 / 40.0, snap=True)
    win.vessel.t = 2.1
    shot(win, "06-verifying")

    win.vessel.set_state("idle")
    win._show_result("done", {"bytes": usize, "write_s": 121.4, "verify_s": 40.2,
                              "verified": True, "ejected": True})
    shot(win, "07-done")

    win._show_result("error", {"code": "verify_mismatch", "msg":
                               "Verification failed: the data read back from the drive differs from "
                               "the image between 1.21 GB and 1.24 GB. The drive may be faulty or "
                               "smaller than it claims."})
    shot(win, "08-verify-failed")
    win.close()
    return 0


def main_gui(argv) -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(APP_NAME)
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication(argv)
    init_fonts()
    apply_theme(app)
    app.setWindowIcon(app_icon())
    initial = next((a for a in argv[1:] if not a.startswith("-") and os.path.isfile(a)), None)
    win = MainWindow(initial)
    win.show()
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--screenshots":
        sys.exit(render_screenshots(sys.argv[2]))
    sys.exit(main_gui(sys.argv))
