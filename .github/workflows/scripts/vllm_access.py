"""Count reads of tracked regular files in the Linux runner's public vLLM checkout."""

import ctypes
import os
import struct
import subprocess
import threading
from pathlib import Path

IN_ACCESS = 0x00000001
IN_Q_OVERFLOW = 0x00004000
IN_ISDIR = 0x40000000
EVENT_HEADER = struct.Struct("=iIII")


class PublicTreeReads:
    """Observe read accesses during a review without opening any source files."""

    def __init__(self, root):
        self.root = Path(root)
        self.seen = {}
        self.directories = {}
        self.overflow = False
        self.stop = threading.Event()

    def __enter__(self):
        listing = subprocess.check_output(["git", "-C", self.root, "ls-files", "--stage", "-z"])
        self.files = set()
        for entry in listing.split(b"\0"):
            if entry.startswith((b"100644 ", b"100755 ")):
                self.files.add(Path(os.fsdecode(entry.split(b"\t", 1)[1])))

        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd == -1:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        try:
            for directory in {p.parent for p in self.files}:
                wd = libc.inotify_add_watch(self.fd, os.fsencode(self.root / directory), IN_ACCESS)
                if wd == -1:
                    raise OSError(ctypes.get_errno(), "inotify_add_watch", str(directory))
                self.directories[wd] = directory
            self.worker = threading.Thread(target=self._watch, daemon=True)
            self.worker.start()
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def _watch(self):
        while not self.stop.wait(0.1):
            self._drain()

    def _drain(self):
        while True:
            try:
                events = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            if not events:
                return
            self._record(events)

    def _record(self, events):
        cursor = 0
        while cursor < len(events):
            wd, mask, _, size = EVENT_HEADER.unpack_from(events, cursor)
            cursor += EVENT_HEADER.size
            name = events[cursor : cursor + size].split(b"\0", 1)[0]
            cursor += size
            if mask & IN_Q_OVERFLOW:
                self.overflow = True
            elif mask & IN_ACCESS and not mask & IN_ISDIR:
                relative = self.directories[wd] / os.fsdecode(name)
                if relative in self.files:
                    self.seen.setdefault(relative, None)

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.worker.join()
        try:
            self._drain()
        finally:
            os.close(self.fd)

    def report(self):
        return {
            "vllm_tracked_files_read": len(self.seen),
            "vllm_read_examples": [str(p) for p in list(self.seen)[:12]],
            "vllm_read_queue_overflow": self.overflow,
        }
