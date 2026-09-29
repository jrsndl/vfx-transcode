"""Running external tools and resolving their paths."""

import logging
import os
import shlex
import shutil
import subprocess
import sys

from .errors import ToolError, TranscodeError

log = logging.getLogger(__name__)

OUTPUT_TAIL_LINES = 25


def app_dir():
    """Folder that relative tool paths (./oiio, ./ffmpeg, ./fonts, ./ocio) are resolved against."""
    if getattr(sys, "frozen", False):
        # PyInstaller one-file build: next to the .exe, not the unpacked _MEIPASS
        return os.path.dirname(sys.executable)
    # repository root = parent of the package folder
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(value, base_dir):
    """Paths starting with ./ or ../ are relative to the application folder, not the CWD."""
    if not value:
        return value
    if value == "." or value.startswith(("./", ".\\", "../", "..\\")):
        return os.path.normpath(os.path.join(base_dir, value))
    return value


def resolve_tool(value, base_dir, fallback_name):
    """Return an existing executable path: the configured one, else the one on PATH."""
    path = resolve_path(value, base_dir)
    if path and os.path.isfile(path):
        return path
    found = shutil.which(path or fallback_name) or shutil.which(fallback_name)
    if found:
        log.debug("%s not found at %s, using %s from PATH", fallback_name, path, found)
        return found
    raise TranscodeError('{} not found at "{}" and not on PATH.'.format(fallback_name, path))


def split_args(text):
    """Split a user-supplied argument string, honouring quotes but keeping backslashes
    (Windows paths) intact."""
    lexer = shlex.shlex(text or "", posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def cmdline(cmd):
    return subprocess.list2cmdline([str(c) for c in cmd])


def _tail(text, lines=OUTPUT_TAIL_LINES):
    parts = (text or "").strip().splitlines()
    return "\n".join(parts[-lines:])


def run_tool(cmd, what, verbose=0, check=True):
    """Run a command, return CompletedProcess. Raise ToolError with the output tail on failure
    (unless check is False: `ffmpeg -i` alone exits 1 but prints what we need)."""
    cmd = [str(c) for c in cmd]
    log.debug("%s: %s", what, cmdline(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="backslashreplace",
            stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise ToolError("{}: could not start {}: {}".format(what, cmd[0], exc))
    if verbose >= 2:
        for stream in (result.stdout, result.stderr):
            if stream and stream.strip():
                log.debug(stream.rstrip())
    if check and result.returncode != 0:
        output = _tail("\n".join(s for s in (result.stdout, result.stderr) if s))
        raise ToolError("{} failed with exit code {}.".format(what, result.returncode), output)
    return result
