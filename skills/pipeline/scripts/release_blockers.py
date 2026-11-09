"""Run-wide rollout findings, separate from source-code QA."""
import run_evidence as E
import run_state as R


def _records(root, state):
    for ref in state.get("artifacts", []):
        if "/artifacts/release_blocker/" in ref:
            yield ref, E.read_report(root, state["run_id"], ref, kind="release_blocker")


def validate(root, state, payload):
    if not isinstance(payload, dict):
        raise ValueError("release blocker requires structured evidence")
    action = payload.get("action")
    if action == "open" and set(payload) == {"action", "coverage", "reason"}:
        E._label(payload["coverage"])
        if isinstance(payload["reason"], str) and payload["reason"].strip():
            return
    if action == "resolve" and set(payload) == {"action", "blocker_ref", "check_ref"}:
        records = dict(_records(root, state))
        blocker = records.get(payload["blocker_ref"]) if isinstance(payload["blocker_ref"], str) else None
        if blocker and blocker["payload"].get("action") == "open":
            _validate_retest(root, state, blocker, payload["check_ref"])
            return
    raise ValueError("invalid release blocker record")


def _validate_retest(root, state, blocker, check_ref):
    import check_plan as P
    import managed_qa as Q

    report = Q._read(root, state, check_ref, "check")
    proof = report["payload"]
    _, latest = Q._checks(root, state)
    if (proof.get("inherited_from") or latest.get(P._identity(proof)) != check_ref
            or R._timestamp(proof.get("started_at", report["recorded_at"])) <= R._timestamp(blocker["recorded_at"])
            or blocker["payload"]["coverage"] not in proof.get("coverage", [])):
        raise ValueError("release blocker requires a fresh retest of its declared coverage")
    Q._validate_ref(root, state, check_ref)


def pending(root, state, *, revalidate=False):
    opened, resolutions = {}, {}
    for ref, report in _records(root, state):
        payload = report["payload"]
        if payload["action"] == "open":
            opened[ref] = report
        else:
            resolutions[payload["blocker_ref"]] = payload["check_ref"]
    unresolved = []
    for ref, blocker in opened.items():
        if ref not in resolutions:
            unresolved.append(ref)
        elif revalidate:
            try:
                _validate_retest(root, state, blocker, resolutions[ref])
            except (OSError, ValueError, KeyError, TypeError):
                unresolved.append(ref)
    return unresolved


def require_clear(root, state):
    unresolved = pending(root, state, revalidate=True)
    if unresolved:
        raise ValueError("unresolved release blockers: " + ", ".join(unresolved))
