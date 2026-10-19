"""
Tests for Decant's write engine: the privileged helper and drive classification.

They need no GUI and no PySide6. Run from the repo root:

    python3 -m unittest discover -s tests -v          # file targets only
    sudo python3 -m unittest discover -s tests -v     # also real loop block devices
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECANT = os.path.join(ROOT, "decant.py")
IS_ROOT = os.geteuid() == 0


def load_engine() -> dict:
    """The Qt-free top of decant.py, as a namespace."""
    with open(DECANT) as f:
        src = f.read().split("# Desktop integration")[0]
    ns = {"__name__": "decant_engine", "__file__": DECANT}
    exec(compile(src, DECANT, "exec"), ns)
    return ns


ENGINE = load_engine()


def helper(*args, stdin_text=None):
    """Run the helper; return (exit code, list of events)."""
    cmd = [sys.executable, DECANT, "--helper"] + list(args)
    if stdin_text is None:
        cmd.append("--no-stdin")
    p = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=600)
    events = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    return p.returncode, events


def files_equal(a, b, length=None) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        remaining = length
        while True:
            n = 1 << 20 if remaining is None else min(1 << 20, remaining)
            x, y = fa.read(n), fb.read(n)
            if x != y:
                return False
            if not x:
                return True
            if remaining is not None:
                remaining -= len(x)
                if remaining == 0:
                    return True


def make_images():
    """A small disk-like image (random, zeros, text) in every format available."""
    imgs = type("Images", (), {})()
    imgs.tmp = tempfile.mkdtemp(prefix="decant-test-")
    imgs.raw = os.path.join(imgs.tmp, "disk.img")
    n = 6 << 20
    with open(imgs.raw, "wb") as f:
        f.write(random.Random(1234).getrandbits(8 * n).to_bytes(n, "little"))
        f.write(b"\0" * (12 << 20))
        f.write("\n".join(str(i) for i in range(300000)).encode())
    imgs.size = os.path.getsize(imgs.raw)
    imgs.compressed = {}
    for tool, ext, args in (("bzip2", "bz2", ["-k", "-f"]), ("xz", "xz", ["-k", "-f", "-T0"]),
                            ("gzip", "gz", ["-k", "-f"]), ("zstd", "zst", ["-k", "-f", "-q"])):
        if shutil.which(tool):
            subprocess.run([tool] + args + [imgs.raw], check=True)
            imgs.compressed[ext] = imgs.raw + "." + ext
    imgs.truncated = os.path.join(imgs.tmp, "truncated.img.bz2")
    with open(imgs.compressed["bz2"], "rb") as f, open(imgs.truncated, "wb") as g:
        g.write(f.read(os.path.getsize(imgs.compressed["bz2"]) // 3))
    return imgs


Images = make_images()


def target_file(name="out.img"):
    path = os.path.join(Images.tmp, name)
    open(path, "wb").close()
    return path


class WriteToFile(unittest.TestCase):
    def check_done(self, rc, events, verified=True):
        self.assertEqual(rc, 0, events[-1:])
        self.assertEqual(events[-1]["ev"], "done")
        self.assertEqual(events[-1]["bytes"], Images.size)
        self.assertEqual(events[-1]["verified"], verified)

    def test_bzip2_with_bzcat(self):
        out = target_file()
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", out,
                        "--decompressor", "bzcat", "--verify", "--allow-file")
        self.check_done(rc, ev)
        self.assertTrue(files_equal(Images.raw, out))
        phases = [e["phase"] for e in ev if e["ev"] == "phase"]
        self.assertEqual(phases, ["preparing", "writing", "syncing", "verifying"])

    @unittest.skipUnless(shutil.which("lbzip2"), "lbzip2 not installed")
    def test_bzip2_with_lbzip2(self):
        out = target_file()
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", out,
                        "--decompressor", "lbzip2", "--verify", "--allow-file")
        self.check_done(rc, ev)
        self.assertTrue(files_equal(Images.raw, out))

    def test_other_formats(self):
        for ext, dec in (("xz", "xz"), ("gz", "gzip"), ("zst", "zstd")):
            if ext not in Images.compressed or not shutil.which(dec):
                continue
            with self.subTest(ext):
                out = target_file()
                rc, ev = helper("--source", Images.compressed[ext], "--target", out,
                                "--decompressor", dec, "--verify", "--allow-file")
                self.check_done(rc, ev)
                self.assertTrue(files_equal(Images.raw, out))

    def test_raw_image(self):
        out = target_file()
        rc, ev = helper("--source", Images.raw, "--target", out, "--verify", "--allow-file")
        self.check_done(rc, ev)
        self.assertTrue(files_equal(Images.raw, out))

    def test_without_verify(self):
        out = target_file()
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", out,
                        "--decompressor", "bzcat", "--allow-file")
        self.check_done(rc, ev, verified=False)

    def test_progress_is_reported(self):
        out = target_file()
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", out,
                        "--decompressor", "bzcat", "--verify", "--allow-file")
        writes = [e for e in ev if e["ev"] == "progress" and e["phase"] == "writing"]
        self.assertTrue(writes)
        self.assertEqual(writes[-1]["in"], writes[-1]["in_total"])
        self.assertEqual(writes[-1]["out"], Images.size)


class Failures(unittest.TestCase):
    def test_truncated_image(self):
        rc, ev = helper("--source", Images.truncated, "--target", target_file(),
                        "--decompressor", "bzcat", "--allow-file")
        self.assertEqual(rc, 1)
        self.assertEqual(ev[-1]["code"], "corrupt")
        self.assertIn("ends unexpectedly", ev[-1]["msg"])

    def test_missing_image(self):
        rc, ev = helper("--source", "/nonexistent.img.bz2", "--target", target_file(),
                        "--decompressor", "bzcat", "--allow-file")
        self.assertEqual(ev[-1]["code"], "no_source")

    def test_file_target_needs_test_flag(self):
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", target_file(),
                        "--decompressor", "bzcat")
        self.assertEqual(ev[-1]["code"], "bad_target")

    def test_only_known_decompressors_run(self):
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", target_file(),
                        "--decompressor", "rm", "--allow-file")
        self.assertEqual(ev[-1]["code"], "bad_decompressor")

    def test_verify_detects_corruption(self):
        Helper = ENGINE["Helper"]
        out = target_file()

        class Corrupting(Helper):
            def verify_readback(self, total):
                with open(self.target, "r+b") as f:
                    f.seek(15_000_000)
                    b = f.read(1)
                    f.seek(15_000_000)
                    f.write(bytes([b[0] ^ 0xFF]))
                return super().verify_readback(total)

        events = []
        h = Corrupting(Images.compressed["bz2"], out, "bzcat", True, False, True, watch_stdin=False)
        h.emit = lambda ev, **kw: events.append(dict(kw, ev=ev))
        self.assertEqual(h.run(), 1)
        self.assertEqual(events[-1]["code"], "verify_mismatch")
        self.assertIn("between 0 B and %s" % ENGINE["fmt_bytes"](Images.size), events[-1]["msg"])


class Cancelling(unittest.TestCase):
    def test_cancel_mid_write(self):
        big = os.path.join(Images.tmp, "big.img.bz2")
        if not os.path.exists(big):  # a few copies, so there's time to cancel
            with open(big, "wb") as g:
                for _ in range(6):
                    with open(Images.compressed["bz2"], "rb") as f:
                        g.write(f.read())
        p = subprocess.Popen([sys.executable, DECANT, "--helper", "--source", big,
                              "--target", target_file(), "--decompressor", "bzcat", "--allow-file"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        last, sent = None, None
        for line in p.stdout:
            last = json.loads(line)
            if last["ev"] == "progress" and sent is None:
                p.stdin.write("cancel\n")
                p.stdin.flush()
                sent = time.monotonic()
        p.wait(timeout=60)
        self.assertEqual(last["ev"], "cancelled")
        self.assertEqual(p.returncode, 3)
        self.assertLess(time.monotonic() - sent, 5)

    def test_window_closed_stops_writer(self):
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", target_file(),
                        "--decompressor", "bzcat", "--allow-file", stdin_text="")
        self.assertEqual(ev[-1]["ev"], "cancelled")

    def test_cancel_before_start_writes_nothing(self):
        out = target_file()
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", out,
                        "--decompressor", "bzcat", "--allow-file", stdin_text="cancel\n")
        self.assertEqual(ev[-1]["ev"], "cancelled")
        self.assertEqual(os.path.getsize(out), 0)


@unittest.skipUnless(IS_ROOT and shutil.which("losetup"), "needs root and losetup")
class LoopDevices(unittest.TestCase):
    """The real block-device path: O_EXCL, flushes, O_DIRECT read-back."""

    def attach(self, size_mb):
        backing = os.path.join(Images.tmp, "loop-%d.bin" % size_mb)
        with open(backing, "wb") as f:
            f.truncate(size_mb << 20)
        dev = subprocess.run(["losetup", "-f", "--show", backing], capture_output=True,
                             text=True, check=True).stdout.strip()
        self.addCleanup(subprocess.run, ["losetup", "-d", dev])
        return dev

    def test_write_and_verify(self):
        dev = self.attach(64)
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", dev, "--decompressor", "bzcat",
                        "--verify", "--allow-file", "--expect-size", str(64 << 20), "--expect-serial=")
        self.assertEqual(rc, 0, ev[-1:])
        self.assertTrue(ev[-1]["verified"])
        self.assertTrue(files_equal(Images.raw, dev, Images.size))

    def test_drive_too_small(self):
        dev = self.attach(16)
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", dev, "--decompressor", "bzcat",
                        "--allow-file")
        self.assertEqual(ev[-1]["code"], "too_small")

    def test_swapped_drive_is_refused(self):
        dev = self.attach(64)
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", dev, "--decompressor", "bzcat",
                        "--allow-file", "--expect-size", str(32010928128), "--expect-serial=")
        self.assertEqual(ev[-1]["code"], "no_target")

    def test_loop_device_needs_test_flag(self):
        dev = self.attach(64)
        rc, ev = helper("--source", Images.compressed["bz2"], "--target", dev, "--decompressor", "bzcat")
        self.assertEqual(ev[-1]["code"], "bad_target")


class DriveClassification(unittest.TestCase):
    def describe(self, node):
        d = ENGINE["describe_drive"](node)
        return d, ENGINE["drive_block_reason"](d)

    def test_usb_stick_old_lsblk_strings(self):
        d, block = self.describe({
            "name": "sdb", "path": "/dev/sdb", "size": "32010928128", "model": "Ultra_Fit ",
            "vendor": "Vendor ", "serial": "ABC", "tran": "usb", "rm": "1", "hotplug": "1",
            "type": "disk", "ro": "0", "mountpoint": None, "children": [
                {"name": "sdb1", "type": "part", "label": "BOOT", "mountpoint": "/run/media/u/BOOT"}]})
        self.assertIsNone(block)
        self.assertTrue(d["removable"])
        self.assertEqual(d["model"], "Vendor Ultra Fit")
        self.assertEqual(d["mounts"], ["/run/media/u/BOOT"])
        self.assertEqual(d["labels"], ["BOOT"])
        self.assertFalse(d["large"])

    def test_system_drive(self):
        d, block = self.describe({
            "name": "nvme0n1", "size": 512110190592, "tran": "nvme", "rm": False, "hotplug": False,
            "type": "disk", "mountpoints": [None], "children": [
                {"name": "p1", "type": "part", "mountpoints": ["/boot/efi"]},
                {"name": "p2", "type": "part", "mountpoints": [None], "children": [
                    {"name": "rl-root", "type": "lvm", "mountpoints": ["/"]}]}]})
        self.assertEqual(block, "System drive")

    def test_swap_is_system(self):
        d, block = self.describe({"name": "sdc", "size": 8 << 30, "type": "disk", "mountpoints": [None],
                                  "children": [{"name": "sdc1", "type": "part", "mountpoints": ["[SWAP]"]}]})
        self.assertEqual(block, "System drive")

    def test_lvm_zfs_and_raid_members(self):
        for child, want in (
            ({"name": "a1", "type": "part", "children": [{"name": "vg-data", "type": "lvm"}]}, "In use by LVM"),
            ({"name": "a1", "type": "part", "fstype": "zfs_member"}, "In use by ZFS"),
            ({"name": "a1", "type": "part", "children": [{"name": "md0", "type": "raid1"}]}, "In use by RAID"),
            ({"name": "a1", "type": "part", "children": [{"name": "luks-x", "type": "crypt"}]},
             "In use by encryption"),
        ):
            with self.subTest(want):
                _, block = self.describe({"name": "sda", "size": 4 << 40, "type": "disk", "vendor": "ATA",
                                          "children": [child]})
                self.assertEqual(block, want)

    def test_internal_data_drive_is_allowed_but_internal(self):
        d, block = self.describe({"name": "sdd", "size": 4000787030016, "vendor": "ATA", "model": "HDD",
                                  "tran": "sata", "rm": False, "hotplug": False, "type": "disk",
                                  "children": [{"name": "sdd1", "type": "part", "mountpoints": ["/srv/media"]}]})
        self.assertIsNone(block)
        self.assertFalse(d["removable"])
        self.assertEqual(d["model"], "HDD")  # the ATA placeholder vendor is dropped
        self.assertTrue(d["large"])

    def test_sd_card_without_removable_flag(self):
        d, block = self.describe({"name": "mmcblk0", "size": 63864569856, "type": "disk",
                                  "rm": False, "hotplug": False})
        self.assertTrue(d["removable"])
        self.assertEqual(d["kind"], "sd")
        self.assertEqual(d["model"], "SD card")

    def test_read_only(self):
        _, block = self.describe({"name": "sde", "size": 1 << 30, "type": "disk", "ro": True, "tran": "usb"})
        self.assertEqual(block, "Read-only")


class Formats(unittest.TestCase):
    def test_detection_by_magic_bytes(self):
        detect = ENGINE["detect_format"]
        self.assertEqual(detect(Images.compressed["bz2"]), "bzip2")
        self.assertEqual(detect(Images.raw), "raw")
        for ext, fmt in (("xz", "xz"), ("gz", "gzip"), ("zst", "zstd")):
            if ext in Images.compressed:
                self.assertEqual(detect(Images.compressed[ext]), fmt)

    def test_uncompressed_size_when_knowable(self):
        size = ENGINE["uncompressed_size"]
        self.assertEqual(size(Images.raw, "raw"), Images.size)
        self.assertIsNone(size(Images.compressed["bz2"], "bzip2"))
        if "xz" in Images.compressed:
            self.assertEqual(size(Images.compressed["xz"], "xz"), Images.size)


if __name__ == "__main__":
    unittest.main()
