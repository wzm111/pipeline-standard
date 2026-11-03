"""Local screenshot bindings and caller-declared viewing, never aesthetic PASS.

The host captures/views images. This module checks bounded image structure and
current evidence bindings; it cannot prove arbitrary host activity or image taste.
"""
import hashlib
from pathlib import Path
import re
import struct
import zlib

import check_plan as P
import observed_checks as H
import run_evidence as E
import run_state as R
import run_tasks as T

MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 64 * 1024 * 1024


def _task(state):
    tasks = T._tasks(state)
    return tasks[-1]["started_at"] if tasks else None


def _name(name, owner):
    E._label(name)
    if owner in name:
        raise ValueError("owner credential cannot appear in visual names")


def _read(root, state, ref, kind):
    if ref not in state["artifacts"]:
        raise ValueError("visual evidence must be registered in this run")
    report = E.read_report(root, state["run_id"], ref, kind=kind)
    if report["payload"].get("task_started_at") != _task(state):
        raise ValueError("visual evidence belongs to another task")
    return report["payload"]


def _surface(value):
    if not isinstance(value, dict) or set(value) != {"target", "viewport", "state", "version"}:
        raise ValueError("visual surface requires target/viewport/state/version only")
    for key in ("target", "state"):
        E._label(value[key])
    if not isinstance(value["version"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value["version"]):
        raise ValueError("visual version requires a bounded non-secret identifier")
    viewport = value["viewport"]
    if (not isinstance(viewport, dict) or set(viewport) != {"width", "height"}
            or any(type(n) is not int or not 0 < n <= 16384 for n in viewport.values())):
        raise ValueError("visual viewport requires bounded integer width/height")
    return value


def declare(root, run_id, owner, name, *, scope, target, viewport, state, version):
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    surface = _surface(dict(target=target, viewport=viewport, state=state, version=version))
    E.fingerprint(root, scope)
    if E.write_paths_conflict(root, scope, ["tmp/pipeline"]):
        raise ValueError("visual source scope must exclude pipeline outputs")
    return E.write_report(root, run_id, owner, "visual_requirement", name,
                          {"task_started_at": _task(owned), "scope": scope, **surface})


def _image_path(run_id, name, image_format):
    E._label(name)
    if image_format not in {"jpeg", "png"}:
        raise ValueError("visual capture format must be jpeg or png")
    return f"tmp/pipeline/runs/{run_id}/images/{name}.{'jpg' if image_format == 'jpeg' else 'png'}"


def _command(ref, digest, image_format):
    return f"visual screenshot {ref} {digest} {image_format}"


def begin(root, run_id, owner, requirement, name, *, image_format="jpeg"):
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    required = _read(root, owned, requirement, "visual_requirement")
    image = _image_path(run_id, name, image_format)
    if name not in owned.get("observed_checks", {}) and E._local(Path(root).resolve(), image).exists():
        raise ValueError("capture output already exists; use a new capture name")
    result = H.begin(root, run_id, owner, name,
                     command=_command(requirement, P._digest(root, requirement), image_format),
                     scope=required["scope"], environment={"scope": "visual-capture"},
                     write_paths=[image], intent="visual", event_view=True)
    return {**result, "image_path": image,
            "notice": "Capture once only when created=true; host capture remains caller-observed."}


def image_info(path):
    """Bounded structural validation, not full JPEG/PNG pixel decoding."""
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("visual image missing or exceeds bounded size")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("visual image exceeds bounded size")
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, offset, seen_data, ended = "image/png", 8, False, False
        while offset < len(data):
            if offset + 12 > len(data):
                raise ValueError("truncated PNG chunk")
            size = int.from_bytes(data[offset:offset + 4], "big")
            tag = data[offset + 4:offset + 8]
            end = offset + 12 + size
            if end > len(data) or zlib.crc32(data[offset + 4:end - 4]) != int.from_bytes(data[end - 4:end], "big"):
                raise ValueError("invalid PNG length or CRC")
            if offset == 8:
                if tag != b"IHDR" or size != 13:
                    raise ValueError("PNG requires IHDR")
                width, height = struct.unpack(">II", data[offset + 8:offset + 16])
            elif tag == b"IHDR":
                raise ValueError("duplicate PNG IHDR")
            seen_data = seen_data or tag == b"IDAT" and size > 0
            offset = end
            if tag == b"IEND":
                if size or offset != len(data) or not seen_data:
                    raise ValueError("invalid PNG end")
                ended = True
                break
        if not ended:
            raise ValueError("PNG end missing")
    elif data.startswith(b"\xff\xd8"):
        kind, offset, scanned, ended = "image/jpeg", 2, False, False
        while offset < len(data):
            if data[offset] != 255:
                raise ValueError("invalid JPEG marker")
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset == len(data):
                raise ValueError("truncated JPEG marker")
            marker = data[offset]
            offset += 1
            if marker == 0xD9:
                ended = scanned and offset == len(data)
                break
            if marker in {0, 0xD8} or 0xD0 <= marker <= 0xD7:
                raise ValueError("unexpected JPEG marker")
            if offset + 2 > len(data):
                raise ValueError("truncated JPEG segment")
            size = int.from_bytes(data[offset:offset + 2], "big")
            end = offset + size
            if size < 2 or end > len(data):
                raise ValueError("invalid JPEG segment length")
            if marker in {0xC0, 0xC1, 0xC2}:
                if size < 8 or size != 8 + 3 * data[offset + 7]:
                    raise ValueError("invalid JPEG frame")
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            offset = end
            if marker == 0xDA:
                scanned = True
                while offset < len(data):
                    if data[offset] == 255:
                        if offset + 1 == len(data):
                            raise ValueError("truncated JPEG scan")
                        following = data[offset + 1]
                        if following == 0 or 0xD0 <= following <= 0xD7:
                            offset += 2
                            continue
                        if following == 255:
                            offset += 1
                            continue
                        break
                    offset += 1
        if not ended:
            raise ValueError("JPEG scan/end missing or trailing data")
    else:
        raise ValueError("visual image must be JPEG or PNG bytes")
    if not width or not height or max(width, height) > 16384 or width * height > MAX_PIXELS:
        raise ValueError("visual dimensions missing or exceed bounded size")
    return {"mime": kind, "width": width, "height": height,
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def finish(root, run_id, owner, requirement, name, *, observed, host_stopped=False, stop_evidence=None):
    owned = P._owned(root, run_id, owner)
    required = _read(root, owned, requirement, "visual_requirement")
    if _surface(observed) != {k: required[k] for k in observed}:
        raise ValueError("observed visual surface does not match declaration")
    ticket = owned.get("observed_checks", {}).get(name)
    if ticket is None:
        raise ValueError("begin visual capture before host invocation")
    declaration = ticket["declaration"]
    image_format = declaration["command"].split(" ")[-1]
    image = _image_path(run_id, name, image_format)
    digest = P._digest(root, requirement)
    if (declaration["command"] != _command(requirement, digest, image_format)
            or declaration["scope"] != required["scope"] or declaration["intent"] != "visual"
            or declaration["write_paths"] != [image]):
        raise ValueError("capture ticket does not match visual declaration")
    info = image_info(E._local(Path(root).resolve(), image))
    if info["mime"] != "image/" + image_format:
        raise ValueError("capture image bytes do not match declared format")
    observation = H.finish(root, run_id, owner, name, exit_code=0, log_path=image,
                           host_stopped=host_stopped, stop_evidence=stop_evidence)
    proof = E.read_report(root, run_id, observation, kind="check")["payload"]
    if not proof["inputs_stable"] or proof["log_sha256"] != info["sha256"]:
        raise ValueError("visual inputs/image changed during capture; new capture required")
    return E.write_report(root, run_id, owner, "visual_capture", name,
                          {"task_started_at": _task(owned), "requirement": requirement,
                           "requirement_sha256": digest, "observation": observation,
                           "source_fingerprint": proof["fingerprint"], "image_path": image,
                           "image": info, "observed": observed, "origin": "caller_observed_host"})


def _capture(root, owned, ref):
    capture = _read(root, owned, ref, "visual_capture")
    required = _read(root, owned, capture["requirement"], "visual_requirement")
    proof_ref = capture["observation"]
    observation = E.read_report(root, owned["run_id"], proof_ref, kind="check")
    H._validate_timing(observation)
    proof = observation["payload"]
    name = proof["observation_id"]
    image_format = capture["image"]["mime"].removeprefix("image/")
    ticket = owned.get("observed_checks", {}).get(name)
    if (not ticket or ticket.get("status") != "completed" or ticket.get("report") != proof_ref
            or any(proof.get(k) != v for k, v in ticket["declaration"].items())
            or proof["command"] != _command(capture["requirement"], capture["requirement_sha256"], image_format)
            or proof["scope"] != required["scope"]
            or proof["log_path"] != capture["image_path"]
            or capture["observed"] != {k: required[k] for k in _surface(capture["observed"])}
            or capture["image_path"] != _image_path(owned["run_id"], name, image_format)):
        raise ValueError("visual capture binding is unconfirmed")
    if (P._digest(root, capture["requirement"]) != capture["requirement_sha256"]
            or E.fingerprint(root, required["scope"]) != capture["source_fingerprint"]
            or image_info(E._local(Path(root).resolve(), capture["image_path"])) != capture["image"]):
        raise ValueError("visual source/declaration/image changed")
    if (capture["observation"] not in owned["artifacts"] or not proof["inputs_stable"]
            or proof["exit_code"] != 0 or proof["intent"] != "visual"
            or proof["log_sha256"] != capture["image"]["sha256"]
            or proof["fingerprint"] != capture["source_fingerprint"]):
        raise ValueError("visual observation is unconfirmed")
    return capture


def view(root, run_id, owner, capture_ref, name, *, viewed=False):
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    _capture(root, owned, capture_ref)
    if viewed is not True:
        raise ValueError("record viewing only after actual image inspection")
    return E.write_report(root, run_id, owner, "visual_view", name,
                          {"task_started_at": _task(owned), "capture": capture_ref,
                           "capture_sha256": P._digest(root, capture_ref), "viewed": True,
                           "basis": "caller_declared", "native_proof": False})


def inspect(root, run_id, requirement):
    """Safe read-only completeness; no command, image content, owner or PASS."""
    before = R.status(root, run_id)["state"]
    if before is None:
        raise ValueError("visual inspection requires an existing run")
    _read(root, before, requirement, "visual_requirement")
    captures = [ref for ref in before["artifacts"] if "/artifacts/visual_capture/" in ref
                and E.read_report(root, run_id, ref, kind="visual_capture")["payload"].get("requirement") == requirement]
    result = {"status": "capture_pending", "viewed": False, "native_proof": False}
    attempts = [ticket for ticket in before.get("observed_checks", {}).values()
                if ticket["declaration"]["command"].startswith(f"visual screenshot {requirement} ")]
    latest = max(attempts, key=lambda ticket: ticket["started_at"], default=None)
    if latest and (latest["status"] != "completed" or not captures or
            E.read_report(root, run_id, captures[-1], kind="visual_capture")["payload"]["observation"] != latest.get("report")):
        captures = []  # A pending/failed later attempt cannot revive an older image.
    if captures:
        ref = captures[-1]
        try:
            capture = _capture(root, before, ref)
            result.update(status="view_pending", image={k: capture["image"][k] for k in ("mime", "width", "height")})
            for view_ref in reversed(before["artifacts"]):
                if "/artifacts/visual_view/" not in view_ref:
                    continue
                viewed = E.read_report(root, run_id, view_ref, kind="visual_view")["payload"]
                if viewed["capture"] == ref and viewed["capture_sha256"] == P._digest(root, ref) and viewed["viewed"] is True:
                    _read(root, before, view_ref, "visual_view")
                    result.update(status="view_declared", viewed=True)
                    break
        except (OSError, ValueError, KeyError, TypeError):
            result["status"] = "stale_or_unconfirmed"
    if R.status(root, run_id)["state"] != before:
        raise ValueError("run changed during visual inspection")
    return {**result, "notice": "Evidence binding only; caller-declared capture/viewing is not native proof or aesthetic acceptance."}
