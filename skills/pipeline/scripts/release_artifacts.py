"""Local, layer-specific release manifests; never contacts or verifies a remote host.

Run capture on the actual target to describe its final artifact. An artifact
manifest cannot substitute for a source-upload manifest. Identity is supplied
by the caller, not inferred or authenticated here. Concurrent hostile writers
cannot be made transactional; descriptor checks and a second scan fail closed
on observed changes. This module requires POSIX no-follow descriptor support.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

FORMAT = "pipeline-release-manifest-v1"


def _path(value, *, root=False):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("paths must be canonical relative POSIX paths")
    if root and value == ".":
        return
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("paths must be canonical relative POSIX paths")


def _context(layer, identity):
    if layer not in ("source", "artifact"):
        raise ValueError("layer must be source or artifact")
    if (not isinstance(identity, dict) or set(identity) != {"build_id", "platform"}
            or any(not isinstance(v, str) or not v.strip() for v in identity.values())):
        raise ValueError("identity requires nonempty build_id and platform strings")


def validate_manifest(value):
    """Reject malformed imported manifests before any comparison."""
    if not isinstance(value, dict) or set(value) != {"format", "layer", "identity", "entries"}:
        raise ValueError("invalid manifest fields")
    if value["format"] != FORMAT:
        raise ValueError("unsupported manifest format")
    _context(value["layer"], value["identity"])
    entries = value["entries"]
    if not isinstance(entries, dict) or entries.get(".") != {"kind": "directory"}:
        raise ValueError("manifest requires a directory root")
    for path, entry in entries.items():
        _path(path, root=True)
        if not isinstance(entry, dict):
            raise ValueError("invalid manifest entry")
        if entry == {"kind": "directory"}:
            pass
        elif (set(entry) == {"kind", "sha256", "executable"} and entry["kind"] == "file"
              and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
              and type(entry["executable"]) is int and entry["executable"] & ~0o111 == 0):
            pass
        else:
            raise ValueError("invalid manifest entry")
        if path != "." and entries.get(str(Path(path).parent)) != {"kind": "directory"}:
            raise ValueError("manifest entry requires its directory parent")
    return value


def loads_manifest(text):
    """Import JSON without silently accepting duplicate keys or NaN."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonstandard JSON constant")

    return validate_manifest(json.loads(text, object_pairs_hook=unique, parse_constant=invalid))


def _snapshot(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mode, st.st_mtime_ns, st.st_ctime_ns)


def _scan(root, parts):
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    entries, snapshots = {}, {}
    descriptors = []
    try:
        fd = os.open(root, directory_flags)
        descriptors.append(fd)
        for part in parts:
            fd = os.open(part, directory_flags, dir_fd=fd)
            descriptors.append(fd)

        def visit(directory, key):
            before = os.fstat(directory)
            entries[key] = {"kind": "directory"}
            snapshots[key] = _snapshot(before)
            names = sorted(os.listdir(directory))
            for name in names:
                child_key = name if key == "." else key + "/" + name
                _path(child_key)
                initial = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(initial.st_mode):
                    child = os.open(name, directory_flags, dir_fd=directory)
                elif stat.S_ISREG(initial.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                else:
                    raise ValueError("manifest inputs must be regular files/directories, without symlinks")
                try:
                    opened = os.fstat(child)
                    if _snapshot(initial) != _snapshot(opened):
                        raise ValueError("input changed during manifest capture")
                    if stat.S_ISDIR(opened.st_mode):
                        visit(child, child_key)
                    else:
                        digest = hashlib.sha256()
                        while True:
                            block = os.read(child, 1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                        entries[child_key] = {"kind": "file", "sha256": digest.hexdigest(),
                                              "executable": opened.st_mode & 0o111}
                        snapshots[child_key] = _snapshot(opened)
                    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if _snapshot(opened) != _snapshot(os.fstat(child)) or _snapshot(opened) != _snapshot(after):
                        raise ValueError("input changed during manifest capture")
                finally:
                    os.close(child)
            if names != sorted(os.listdir(directory)) or _snapshot(before) != _snapshot(os.fstat(directory)):
                raise ValueError("input changed during manifest capture")

        visit(fd, ".")
        return entries, snapshots
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def capture(project_root, base_path, *, layer, identity):
    """Capture every file and directory below a project-relative directory.

    build_id identifies the actual source commit/build and platform identifies
    the actual environment. Base paths differ between machines and therefore
    are not part of the portable identity; all entry paths are base-relative.
    """
    _context(layer, identity)
    _path(base_path, root=True)
    if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise ValueError("safe manifest capture requires POSIX directory descriptors")
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    parts = () if base_path == "." else base_path.split("/")
    try:
        first = _scan(root, parts)
        second = _scan(root, parts)
    except OSError as error:
        raise ValueError("cannot safely capture manifest inputs") from error
    if first != second:
        raise ValueError("input changed during manifest capture")
    return validate_manifest({"format": FORMAT, "layer": layer, "identity": dict(identity), "entries": first[0]})


def compare(expected, observed):
    """Compare matching identities only; complete means this local comparison."""
    validate_manifest(expected)
    validate_manifest(observed)
    if expected["layer"] != observed["layer"] or expected["identity"] != observed["identity"]:
        raise ValueError("cannot compare different layers, builds, or platforms")
    left, right = expected["entries"], observed["entries"]
    added = sorted(set(right) - set(left))
    missing = sorted(set(left) - set(right))
    changed = sorted(key for key in set(left) & set(right) if left[key] != right[key])
    return {"complete": not (added or missing or changed), "added": added, "missing": missing, "changed": changed}
