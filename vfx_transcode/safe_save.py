"""Saving a shared preferences file (super_presets.json on a studio share) so that several
people can save at the same time and a failed save never damages the file.

1. The new content is written to a temporary file in the same folder, named after the user
   and the time: super_presets_jdoe_261124-1649.json. Same folder, so the final rename stays
   on one disk and is atomic.
2. It is read back and compared; a short or broken write stops here, the old file untouched.
3. A lock file (<name>.lock, made with an exclusive create, which also works on network
   shares) is taken for the few milliseconds of steps 3-5, so two saves can't pass the next
   check together. A lock older than LOCK_STALE seconds is left over from a crash and removed.
   If the file changed on disk since it was read (someone else saved in between), the save is
   given up with SaveConflict, so the caller can read again and redo its change.
4. The current file is copied to super_presets_safecopyNNN.json (the next free number).
5. The temporary file replaces the prefs file in one rename. The old file is copied rather
   than renamed away first, so there is never a moment without a prefs file for someone
   else's tuner or vfx-transcode to read.
"""

import datetime
import getpass
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import TranscodeError

SAFECOPY = "_safecopy"
REPLACE_ATTEMPTS = 10           # Windows: a reader holding the file open blocks the rename
REPLACE_WAIT = 0.2
LOCK_WAIT = 10.0                # seconds to wait for someone else's lock
LOCK_STALE = 60.0               # a lock this old is left over from a crash


class SaveConflict(TranscodeError):
    """The file changed on disk after it was read: read it again and redo the change."""


@dataclass
class SaveResult:
    path: Path
    backup: Optional[Path]      # the safe copy of the previous version, None for a new file


def file_stamp(path):
    """(modification time, size) of a file, or None if it does not exist: compared before
    replacing, to notice someone else's save."""
    try:
        st = os.stat(str(path))
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def user_name():
    try:
        name = getpass.getuser()
    except Exception:                       # no user name in the environment
        name = "user"
    return re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-") or "user"


def temp_path(path, user=None, now=None, n=1):
    """<stem>_<user>_<YYMMDD-HHMM><suffix> in the file's folder; with n > 1: ..._<n><suffix>."""
    path = Path(path)
    stamp = (now or datetime.datetime.now()).strftime("%y%m%d-%H%M")
    base = "{}_{}_{}".format(path.stem, user or user_name(), stamp)
    return path.with_name(base + path.suffix if n == 1 else "{}_{}{}".format(base, n, path.suffix))


def create_temp(path, data, user=None, now=None):
    """Write data to a new temporary file, claiming its name with an exclusive create (the same
    user saving twice in a minute, from two tuners, gets _2, _3 ...). Returns its path."""
    n = 1
    while True:
        tmp = temp_path(path, user, now, n)
        try:
            fh = open(str(tmp), "xb")
        except FileExistsError:
            n += 1
            continue
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return tmp


def safecopy_numbers(path):
    path = Path(path)
    pattern = re.compile(re.escape(path.stem + SAFECOPY) + r"(\d+)" + re.escape(path.suffix) + "$")
    numbers = []
    for item in path.parent.glob(path.stem + SAFECOPY + "*" + path.suffix):
        match = pattern.match(item.name)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def make_safecopy(path):
    """Copy the file to <stem>_safecopyNNN<suffix>, the next free number. The number is
    claimed with an exclusive create, so two people saving at once never share one."""
    path = Path(path)
    number = max(safecopy_numbers(path) + [0]) + 1
    data = path.read_bytes()
    while True:
        target = path.with_name("{}{}{:03d}{}".format(path.stem, SAFECOPY, number, path.suffix))
        try:
            with open(str(target), "xb") as fh:
                fh.write(data)
            shutil.copystat(str(path), str(target))
            return target
        except FileExistsError:
            number += 1


class _Lock:
    """<file>.lock, created exclusively; holds the user name and time for whoever finds it."""

    def __init__(self, path, user=None):
        self.path = Path(str(path) + ".lock")
        self.user = user or user_name()

    def __enter__(self):
        deadline = time.time() + LOCK_WAIT
        while True:
            try:
                with open(str(self.path), "x", encoding="utf-8") as fh:
                    fh.write("{} {}\n".format(self.user, datetime.datetime.now().isoformat()))
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(str(self.path)) > LOCK_STALE:
                        os.remove(str(self.path))       # left over from a crash
                        continue
                except OSError:
                    continue                            # released meanwhile
                if time.time() > deadline:
                    try:
                        holder = self.path.read_text(encoding="utf-8").strip()
                    except OSError:
                        holder = "?"
                    raise TranscodeError("{} is locked by {}; try again in a moment.".format(
                        self.path.name[:-5], holder))
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(str(self.path))
        except OSError:
            pass


def safe_write_text(path, text, expect_stamp=False, stamp=None, user=None, now=None,
                    validate=None):
    """Write text to path as described in the module docstring. expect_stamp: give up with
    SaveConflict if the file's file_stamp is no longer `stamp`. validate(temp path): raises if
    the new file is not usable; the old file then stays. Returns a SaveResult."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = create_temp(path, data, user, now)      # ours from here on: removed in finally
    try:
        if tmp.read_bytes() != data:
            raise TranscodeError("{}: the written file does not read back the same.".format(tmp))
        if validate is not None:
            validate(tmp)
        with _Lock(path, user):
            if expect_stamp and file_stamp(path) != stamp:
                raise SaveConflict("{} was changed by someone else while saving.".format(path))
            backup = make_safecopy(path) if path.is_file() else None
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(str(tmp), str(path))
                    break
                except PermissionError:
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_WAIT)
        return SaveResult(path, backup)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
