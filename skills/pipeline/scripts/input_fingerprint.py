"""Shared explicit-input scanner and compatible local evidence hashes."""
import hashlib
import json
from pathlib import Path
import stat


def _local(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("inputs must be project relative without ..")
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink inputs are not reusable")
    return path


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode)


def fingerprint(project_root, paths, environment=None, *, narrow=False):
    """Scan once with shared safety checks; retain the two established encodings.

    Supplying environment uses the workflow digest envelope. Omitting it uses
    the run-evidence digest. This does not prove semantic scope completeness or
    provide a filesystem transaction against noncooperating external writers.
    """
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    entries = {}

    def visit(path):
        _local(root, path.relative_to(root).as_posix())
        key = path.relative_to(root).as_posix()
        try:
            before = path.stat()
        except FileNotFoundError:
            entries[key] = {"missing": True}
            return
        if stat.S_ISDIR(before.st_mode):
            entries[key] = {"directory": True}
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(before.st_mode):
            entries[key] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "mode": before.st_mode & 0o777}
        else:
            raise ValueError("inputs must be regular files/directories")
        _local(root, key)
        if _identity(before) != _identity(path.stat()):
            raise ValueError("input changed during fingerprint")

    for relative in paths:
        path = _local(root, relative)
        if narrow and path == root:
            raise ValueError("declare narrow input directories, not the project root")
        visit(path)
    if not entries:
        raise ValueError("at least one input required")
    if environment is not None:
        value = {"inputs": entries, "environment": environment}
    else:
        value = {key: ("missing" if entry.get("missing") else "directory" if entry.get("directory")
                       else [entry["sha256"], entry["mode"]]) for key, entry in entries.items()}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
