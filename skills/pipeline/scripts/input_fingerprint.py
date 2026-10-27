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


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonstandard JSON constant: {value}")


def _string_map(value, label):
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError(f"{label} must be an object of strings")


def _package_object(value):
    if not isinstance(value, dict):
        raise ValueError("package metadata must be an object")
    for key in ("name", "version"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"{key} must be a nonempty string")
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "scripts", "engines"):
        if key in value:
            _string_map(value[key], key)


def _legacy_lock_dependencies(value):
    if not isinstance(value, dict):
        raise ValueError("lock dependencies must be an object")
    for dependency in value.values():
        if not isinstance(dependency, dict):
            raise ValueError("lock dependency must be an object")
        if "version" in dependency and (not isinstance(dependency["version"], str) or not dependency["version"].strip()):
            raise ValueError("lock dependency version must be a nonempty string")
        if "requires" in dependency:
            _string_map(dependency["requires"], "requires")
        if "dependencies" in dependency:
            _legacy_lock_dependencies(dependency["dependencies"])


def dependency_identity(project_root, paths):
    """Explicit package/lock identity for checks declared version-independent.

    Ignore only the package and lock root version, plus lock packages[''].version.
    All other JSON remains significant. This never changes ordinary fingerprint
    behavior or decides whether a check may ignore release metadata.
    """
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("explicit nonempty package paths required")
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project root must be a directory")
    documents, snapshots = {}, {}
    for relative in paths:
        path = _local(root, relative)
        if path.name not in {"package.json", "package-lock.json"}:
            raise ValueError("dependency identity supports only package.json and package-lock.json")
        before = path.stat()  # Missing files fail closed, unlike generic scope hashes.
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("package inputs must be regular files")
        raw = path.read_bytes()
        _local(root, relative)
        if _identity(before) != _identity(path.stat()):
            raise ValueError("package input changed during dependency identity")
        document = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        if path.name == "package.json":
            _package_object(document)
        else:
            if not isinstance(document, dict):
                raise ValueError("package lock must be an object")
            for key in ("name", "version"):
                if key in document and (not isinstance(document[key], str) or not document[key].strip()):
                    raise ValueError(f"lock {key} must be a nonempty string")
            if "lockfileVersion" in document and (type(document["lockfileVersion"]) is not int or document["lockfileVersion"] < 1):
                raise ValueError("lockfileVersion must be a positive integer")
            if "packages" in document:
                if not isinstance(document["packages"], dict):
                    raise ValueError("lock packages must be an object")
                for package in document["packages"].values():
                    _package_object(package)
                if "" in document["packages"]:
                    document["packages"][""].pop("version", None)
            if "dependencies" in document:
                _legacy_lock_dependencies(document["dependencies"])
        document.pop("version", None)
        key = path.relative_to(root).as_posix()
        documents[key] = document
        snapshots[path] = before
    # Detect a previously read input changing while a later input is read.
    for path, before in snapshots.items():
        _local(root, path.relative_to(root).as_posix())
        if _identity(before) != _identity(path.stat()):
            raise ValueError("package input changed during dependency identity")
    encoded = json.dumps(documents, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
