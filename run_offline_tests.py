"""Run tests with an audit guard against accidental host writes or SSH/control commands.

This is a regression-test tripwire, not an OS sandbox for untrusted code.
"""
import os
from pathlib import Path
import sys
import tempfile
import traceback
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
TEMP = Path(tempfile.gettempdir()).resolve()
violations = []


def _writable(path, directory_fd=None):
    if isinstance(path, int):
        return True
    try:
        value = Path(os.fsdecode(path))
        if not value.is_absolute() and directory_fd is not None and directory_fd >= 0:
            if sys.platform == "darwin":
                import fcntl
                parent = fcntl.fcntl(directory_fd, 50, bytes(1024)).split(b"\0", 1)[0]
                value = Path(os.fsdecode(parent)) / value
            else:
                value = Path(os.readlink(f"/proc/self/fd/{directory_fd}")) / value
        value = value.resolve()
        return TEMP in value.parents and value != TEMP
    except (OSError, TypeError):
        return False


def guard(event, args):
    targets = []
    if event == "open":
        path, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            targets = [(path, None)]
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.chown", "os.truncate"):
        index = {"os.mkdir": 2, "os.remove": 1, "os.rmdir": 1, "os.chmod": 2, "os.chown": 3}.get(event)
        directory_fd = args[index] if index is not None and len(args) > index else None
        targets = [(args[0], directory_fd)]
    elif event in ("os.rename", "os.link", "os.symlink"):
        if event == "os.rename":
            targets = [(args[0], args[2]), (args[1], args[3])]
        elif event == "os.link":
            targets = [(args[0], args[2]), (args[1], args[3])]
        else:
            targets = [(args[1], args[2])]
    elif event == "subprocess.Popen":
        command = args[1]
        executable = Path(command[0] if isinstance(command, (list, tuple)) else args[0]).name
        if executable not in ("openssl", "pveversion"):
            violations.append("unexpected_subprocess:" + executable)
            raise PermissionError("offline test attempted an unexpected subprocess")
    if any(not _writable(path, directory_fd) for path, directory_fd in targets):
        frames = [frame for frame in traceback.extract_stack() if str(ROOT) in frame.filename]
        location = ";".join(Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-5:-1])
        violations.append("host_write:" + event + ":" + location)
        raise PermissionError("offline test attempted a write outside temporary fixtures")


if __name__ == "__main__":
    sys.addaudithook(guard)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if violations:
        print("offline_guard_violations=" + str(len(violations)), file=sys.stderr)
        from collections import Counter
        print(dict(Counter(violations)), file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() and not violations else 1)
