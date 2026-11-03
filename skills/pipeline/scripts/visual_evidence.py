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


def _at_task(state, started):
    result = {**state, "tasks": []}
    tasks = T._tasks(state)
    if started is None and tasks:
        result["_visual_interval_end"] = tasks[0]["started_at"]
    if started is not None:
        for index, task in enumerate(T._tasks(state)):
            if task["started_at"] == started:
                result["tasks"] = state["tasks"][:index + 1]
                break
        else:
            raise ValueError("visual source task is unavailable")
    return result


def _context(root, state, requirement):
    if requirement in state["artifacts"]:
        payload = E.read_report(root, state["run_id"], requirement, kind="visual_requirement")["payload"]
        return _at_task(state, payload["task_started_at"])
    import delivery as D
    for inherited in state.get("inherited_visual", []):
        binding = inherited["binding"]
        selected = next((ref for ref in binding["reports"] if ref["path"] == requirement
                         and "/artifacts/visual_requirement/" in ref["path"]), None)
        if selected is not None:
            packet = D._receipt_metadata(root, inherited["receipt"])
            if binding not in packet.get("visual", []):
                raise ValueError("visual inheritance is not accepted by the predecessor")
            for ref in binding["reports"]:
                D._read_reference(root, ref, E.read_report(root, ref["run_id"], ref["path"])["kind"])
            source = R.status(root, selected["run_id"])["state"]
            if source["status"] != "completed":
                raise ValueError("visual source run is not completed")
            payload = E.read_report(root, selected["run_id"], requirement, kind="visual_requirement")["payload"]
            return _at_task(source, payload["task_started_at"])
    raise ValueError("visual requirement is not owned or explicitly inherited")


def _requirements(root, state):
    refs = [ref for ref in state["artifacts"] if "/artifacts/visual_requirement/" in ref]
    refs += [ref["path"] for item in state.get("inherited_visual", []) for ref in item["binding"]["reports"]
             if "/artifacts/visual_requirement/" in ref["path"]]
    requirements = {}
    for ref in dict.fromkeys(refs):
        context = _context(root, state, ref)
        requirements[ref] = _read(root, context, ref, "visual_requirement")
    replaced = set()
    for ref, required in requirements.items():
        previous = required.get("supersedes")
        if previous is None:
            continue
        if previous == ref or previous not in requirements or previous in replaced:
            raise ValueError("visual supersession must be an owned, non-branching requirement chain")
        old = requirements[previous]
        if (old["target"] != required["target"] or not set(old["scope"]) <= set(required["scope"])
                or P._digest(root, previous) != required["supersedes_sha256"]
                or old.get("review", "self") == "independent" and required.get("review", "self") != "independent"):
            raise ValueError("visual supersession cannot drop target, source scope or independent review")
        replaced.add(previous)
        seen = {ref}
        while previous:
            if previous in seen:
                raise ValueError("visual supersession cycle")
            seen.add(previous)
            previous = requirements[previous].get("supersedes")
    return requirements, replaced


def _ancestor_review(root, state, requirement, seen=None):
    seen = set() if seen is None else seen
    if requirement in seen:
        raise ValueError("visual supersession cycle")
    seen.add(requirement)
    required = _read(root, _context(root, state, requirement), requirement, "visual_requirement")
    previous = required.get("supersedes")
    if previous is None:
        return None, None
    context = _context(root, state, previous)
    history = _reviews(root, context, previous)
    return history[-1] if history else _ancestor_review(root, state, previous, seen)


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


def declare(root, run_id, owner, name, *, scope, target, viewport, state, version, review="self", supersedes=None):
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    surface = _surface(dict(target=target, viewport=viewport, state=state, version=version))
    if review not in {"self", "independent"}:
        raise ValueError("visual review must be self or independent")
    E.fingerprint(root, scope)
    if E.write_paths_conflict(root, scope, ["tmp/pipeline"]):
        raise ValueError("visual source scope must exclude pipeline outputs")
    payload = {"task_started_at": _task(owned), "scope": scope, "review": review, **surface}
    if supersedes is not None:
        previous = _read(root, _context(root, owned, supersedes), supersedes, "visual_requirement")
        requirements, replaced = _requirements(root, owned)
        if (supersedes in replaced or previous["target"] != target or not set(previous["scope"]) <= set(scope)
                or previous.get("review", "self") == "independent" and review != "independent"):
            raise ValueError("visual supersession cannot branch or drop target/scope/review")
        payload.update(supersedes=supersedes, supersedes_sha256=P._digest(root, supersedes))
    return E.write_report(root, run_id, owner, "visual_requirement", name, payload)


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
                           "image": info, "observed": observed, "origin": "caller_observed_host",
                           "candidate_id": owned.get("release_plan", {}).get("candidate", {}).get("id")})


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


def _reviewer(owned, dispatch_id):
    if dispatch_id is None:
        return
    import role_dispatch as D
    item = D._ticket(owned, dispatch_id)
    start = _task(owned)
    end = owned.get("_visual_interval_end") or (T._tasks(owned)[-1]["ended_at"] if T._tasks(owned) else None)
    if (item["role"] != "tester" or item["status"] not in {"working", "completed"}
            or not item.get("accepted_at") or not item.get("executor_id")
            or start and R._timestamp(item["accepted_at"]) < R._timestamp(start)
            or end and R._timestamp(item["accepted_at"]) > R._timestamp(end)):
        raise ValueError("independent visual review requires an accepted current-task tester")
    for developer in owned.get("role_dispatches", []):
        if (developer["role"] == "developer" and developer.get("accepted_at")
                and (not start or R._timestamp(developer["accepted_at"]) >= R._timestamp(start)) and (
                not end or R._timestamp(developer["accepted_at"]) <= R._timestamp(end)) and (
                developer.get("executor_id") == item["executor_id"] or
                item.get("host_executor_id") and developer.get("host_executor_id") == item["host_executor_id"])):
            raise ValueError("visual reviewer must differ from the implementation executor")


def view(root, run_id, owner, capture_ref, name, *, viewed=False, reviewer_dispatch=None):
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    _capture(root, owned, capture_ref)
    _reviewer(owned, reviewer_dispatch)
    if viewed is not True:
        raise ValueError("record viewing only after actual image inspection")
    return E.write_report(root, run_id, owner, "visual_view", name,
                          {"task_started_at": _task(owned), "capture": capture_ref,
                           "capture_sha256": P._digest(root, capture_ref), "viewed": True,
                           "reviewer_dispatch": reviewer_dispatch,
                           "basis": "caller_declared", "native_proof": False})


def _latest_capture(root, owned, requirement):
    run_id = owned["run_id"]
    captures = [ref for ref in owned["artifacts"] if "/artifacts/visual_capture/" in ref
                and E.read_report(root, run_id, ref, kind="visual_capture")["payload"].get("requirement") == requirement]
    attempts = [ticket for ticket in owned.get("observed_checks", {}).values()
                if ticket["declaration"]["command"].startswith(f"visual screenshot {requirement} ")]
    latest = max(attempts, key=lambda ticket: ticket["started_at"], default=None)
    if not captures or latest and (latest["status"] != "completed" or
            E.read_report(root, run_id, captures[-1], kind="visual_capture")["payload"]["observation"] != latest.get("report")):
        return None
    return captures[-1]


def _reviews(root, owned, requirement):
    return [(ref, _read(root, owned, ref, "visual_review")) for ref in owned["artifacts"]
            if "/artifacts/visual_review/" in ref and
            E.read_report(root, owned["run_id"], ref, kind="visual_review")["payload"].get("requirement") == requirement]


def _view(root, owned, view_ref, capture_ref, reviewer_dispatch):
    viewed = _read(root, owned, view_ref, "visual_view")
    if (viewed["capture"] != capture_ref or viewed["capture_sha256"] != P._digest(root, capture_ref)
            or viewed["viewed"] is not True or viewed.get("reviewer_dispatch") != reviewer_dispatch):
        raise ValueError("visual review requires this reviewer's actual current image-view declaration")
    _reviewer(owned, reviewer_dispatch)


def review(root, run_id, owner, capture_ref, view_ref, name, *, gate, findings,
           resolved=None, reviewer_dispatch=None):
    """Record an actual judgment; never calculate aesthetic quality or scores."""
    owned = P._owned(root, run_id, owner)
    _name(name, owner)
    capture = _capture(root, owned, capture_ref)
    requirement = capture["requirement"]
    required = _read(root, owned, requirement, "visual_requirement")
    if capture_ref != _latest_capture(root, owned, requirement):
        raise ValueError("review requires the latest completed visual capture")
    _view(root, owned, view_ref, capture_ref, reviewer_dispatch)
    if required.get("review", "self") == "independent" and reviewer_dispatch is None:
        raise ValueError("this UI requirement needs independent tester review")
    if gate not in {"PASS", "CONCERNS", "FAIL"} or not isinstance(findings, list) or len(findings) > 64:
        raise ValueError("visual review requires gate and bounded actual findings")
    ids = []
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"id", "summary"}
                or not isinstance(finding["summary"], str) or not finding["summary"].strip()
                or len(finding["summary"]) > 1000):
            raise ValueError("visual findings require id and short non-secret summary")
        ids.append(E._label(finding["id"]))
    if len(set(ids)) != len(ids) or (gate == "PASS") != (not ids):
        raise ValueError("PASS requires no unresolved findings; FAIL/CONCERNS retain actual findings")
    resolved = E.coverage_labels(resolved)
    previous = _reviews(root, owned, requirement)
    prior_ref, prior = previous[-1] if previous else _ancestor_review(root, owned, requirement)
    prior_ids = {item["id"] for item in prior["findings"]} if prior else set()
    if not set(resolved) <= prior_ids or not prior_ids - set(resolved) <= set(ids) or set(resolved) & set(ids):
        raise ValueError("retain or explicitly resolve previous visual findings")
    if resolved and (capture_ref == prior["capture"] or view_ref == prior["view"]):
        raise ValueError("resolved visual findings require a new screenshot and new viewing")
    return E.write_report(root, run_id, owner, "visual_review", name,
                          {"task_started_at": _task(owned), "requirement": requirement,
                           "capture": capture_ref, "capture_sha256": P._digest(root, capture_ref),
                           "view": view_ref, "view_sha256": P._digest(root, view_ref),
                           "reviewer_dispatch": reviewer_dispatch, "gate": gate, "findings": findings,
                           "previous_review": prior_ref,
                           "previous_sha256": P._digest(root, prior_ref) if prior_ref else None,
                           "resolved": resolved, "basis": "caller_declared", "native_proof": False})


def _valid_review(root, owned, ref, payload):
    capture = _capture(root, owned, payload["capture"])
    required = _read(root, owned, payload["requirement"], "visual_requirement")
    if (capture["requirement"] != payload["requirement"] or
            required.get("review", "self") == "independent" and payload["reviewer_dispatch"] is None):
        raise ValueError("visual review requirement/reviewer changed")
    _view(root, owned, payload["view"], payload["capture"], payload["reviewer_dispatch"])
    if (payload["capture"] != _latest_capture(root, owned, payload["requirement"])
            or P._digest(root, payload["capture"]) != payload["capture_sha256"]
            or P._digest(root, payload["view"]) != payload["view_sha256"]):
        raise ValueError("visual review source/image/view changed")
    previous, _ = _ancestor_review(root, owned, payload["requirement"])
    for history_ref, history in _reviews(root, owned, payload["requirement"]):
        if (history["previous_review"] != previous or
                history["previous_sha256"] != (P._digest(root, previous) if previous else None)):
            raise ValueError("previous visual review changed")
        previous = history_ref
        if history_ref == ref:
            break


def inspect(root, run_id, requirement):
    """Safe read-only completeness; no command, image content, owner or PASS."""
    before = R.status(root, run_id)["state"]
    if before is None:
        raise ValueError("visual inspection requires an existing run")
    _read(root, before, requirement, "visual_requirement")
    result = {"status": "capture_pending", "viewed": False, "native_proof": False, "review": "pending"}
    ref = _latest_capture(root, before, requirement)
    if ref:
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
            reviews = _reviews(root, before, requirement)
            if reviews:
                review_ref, reviewed = reviews[-1]
                if reviewed["capture"] == ref:
                    _valid_review(root, before, review_ref, reviewed)
                    result.update(status="review_declared", review=reviewed["gate"])
        except (OSError, ValueError, KeyError, TypeError):
            result["status"] = "stale_or_unconfirmed"
    if R.status(root, run_id)["state"] != before:
        raise ValueError("run changed during visual inspection")
    return {**result, "notice": "Evidence binding only; caller-declared capture/viewing is not native proof or aesthetic acceptance."}


def _reference(root, path):
    import delivery as D
    parts = Path(path).parts
    if parts[:3] != ("tmp", "pipeline", "runs") or len(parts) < 7:
        raise ValueError("visual reference must be a run report")
    E.read_report(root, parts[3], path)
    return D._reference(root, parts[3], path)


def _binding(root, state, requirement):
    import delivery as D
    for inherited in state.get("inherited_visual", []):
        if inherited["binding"]["requirement"]["path"] == requirement:
            # Metadata can authorize a fresh explicit replacement; old PASS reuse
            # still requires the unchanged predecessor context.
            D.validated_receipt(root, inherited["receipt"], refresh_coverage=False)
    context = _context(root, state, requirement)
    capture_ref = _latest_capture(root, context, requirement)
    if capture_ref is None:
        raise ValueError("visual capture is pending")
    capture = _capture(root, context, capture_ref)
    candidate = state.get("release_plan", {}).get("candidate", {}).get("id")
    if candidate is not None and capture.get("candidate_id") != candidate:
        raise ValueError("visual release candidate changed; current capture required")
    history = _reviews(root, context, requirement)
    if not history:
        raise ValueError("visual review is pending")
    review_ref, reviewed = history[-1]
    _valid_review(root, context, review_ref, reviewed)
    if reviewed["gate"] != "PASS" or reviewed["findings"]:
        raise ValueError("visual review has unresolved findings")
    refs = {requirement, capture_ref, reviewed["view"], capture["observation"], review_ref}
    previous = reviewed["previous_review"]
    while previous:
        if previous in refs:
            raise ValueError("visual review history cycle")
        refs.add(previous)
        reference = _reference(root, previous)
        previous = E.read_report(root, reference["run_id"], previous, kind="visual_review")["payload"]["previous_review"]
    previous = _read(root, context, requirement, "visual_requirement").get("supersedes")
    while previous:
        if previous in refs:
            raise ValueError("visual requirement history cycle")
        refs.add(previous)
        previous_context = _context(root, state, previous)
        previous = _read(root, previous_context, previous, "visual_requirement").get("supersedes")
    return {"requirement": _reference(root, requirement), "task_started_at": _task(context),
            "capture": _reference(root, capture_ref), "view": _reference(root, reviewed["view"]),
            "review": _reference(root, review_ref), "reports": [_reference(root, ref) for ref in sorted(refs)]}


def judgment(root, state):
    """Current explicit UI and carried gaps only; no inference from source names."""
    if not state.get("inherited_visual") and not any("/artifacts/visual_requirement/" in ref for ref in state["artifacts"]):
        return None
    try:
        requirements, replaced = _requirements(root, state)
    except (OSError, ValueError, KeyError, TypeError):
        return {"complete": False, "requirements": [{"status": "unconfirmed_visual_source"}],
                "notice": "Explicit visual source obligations are pending; invalid inheritance cannot erase them."}
    inherited = {item["binding"]["requirement"]["path"] for item in state.get("inherited_visual", [])}
    entries = []
    for ref, required in requirements.items():
        if ref in replaced:
            continue
        context = _context(root, state, ref)
        current = context["run_id"] == state["run_id"] and required["task_started_at"] == _task(state)
        try:
            history = _reviews(root, context, ref)
        except (OSError, ValueError, KeyError, TypeError):
            history = []
        if not current and ref not in inherited and history and history[-1][1]["gate"] == "PASS":
            continue  # Completed older UI tasks do not invent requirements for unrelated work.
        entry = {"requirement": _reference(root, ref),
                 "scope": "current_task" if current else "inherited" if ref in inherited else "unresolved_prior_task",
                 "status": "pending_or_stale"}
        try:
            entry.update(status="review_declared", binding=_binding(root, state, ref))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        entries.append(entry)
    if not entries:
        return None
    return {"complete": all(item["status"] == "review_declared" for item in entries), "requirements": entries,
            "notice": "Binding completeness only; visual PASS is the actual observer's declared review, not machine aesthetic proof."}


def apply(root, state, payload):
    visual = judgment(root, state)
    if visual is None:
        return payload
    result = {**payload, "visual": visual}
    if not visual["complete"] and result.get("gate") != "FAIL":
        result["gate"] = "CONCERNS"
    return result


def delivery_bindings(root, state):
    visual = judgment(root, state)
    if visual is None:
        return []
    if not visual["complete"]:
        raise ValueError("explicit UI visual acceptance is incomplete")
    return [entry["binding"] for entry in visual["requirements"]]


def delivery_preview(root, state):
    """Safe local paths for user-visible delivery; never upload image contents."""
    result = []
    for binding in delivery_bindings(root, state):
        capture = E.read_report(root, binding["capture"]["run_id"], binding["capture"]["path"], kind="visual_capture")["payload"]
        result.append({"image_path": str(E._local(Path(root).resolve(), capture["image_path"])),
                       "target": capture["observed"]["target"], "state": capture["observed"]["state"],
                       "version": capture["observed"]["version"], "viewport": capture["observed"]["viewport"],
                       "review_basis": "caller_declared", "native_proof": False})
    return result
