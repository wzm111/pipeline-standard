"""Allocation is a bounded declaration; fixtures simulate dispatch observations."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import run_state as R
import work_allocation as A


def work(key="core", executor="dev-1", paths=None, deps=None):
    return {"id": key, "executor_id": executor, "write_paths": paths or ["src/core.py"], "depends_on": deps or []}


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.start = R.start(self.root)
        self.args = self.root, self.start["run_id"], self.start["owner_token"]
        self.edit(lambda state: A.initialize(state, "run-start", "run_start"))

    def state(self):
        return R.status(self.root)["state"]

    def edit(self, change):
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, *self.args[1:])
            change(state)
            R._atomic(path, state)

    def decide(self, items=None, mode="serial", reason="small_scope"):
        return A.decide(*self.args, mode, reason, [work()] if items is None else items)

    def dispatch(self, key="core", status="dispatching", **extra):
        binding = A.require_dispatch(self.state(), key)
        self.edit(lambda state: state.setdefault("role_dispatches", []).append(
            {"role": "developer", "status": status, **binding, **extra}))
        return binding

    def unchanged_reject(self, call):
        before = self.state()
        with self.assertRaises(ValueError):
            call()
        self.assertEqual(before, self.state())

    def test_pending_blocks_dispatch_but_legacy_and_unknown_are_read_only(self):
        self.assertEqual(A.summary(None)["status"], "unknown")
        state = {"role_dispatches": []}
        before = deepcopy(state)
        self.assertEqual(A.require_dispatch(state), {})
        self.assertEqual(A.summary(state)["status"], "unknown")
        self.assertEqual(state, before)
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "core"))

    def test_decision_and_trigger_idempotency_do_not_write(self):
        self.decide()
        with patch.object(R, "_atomic", side_effect=AssertionError("unexpected write")):
            self.decide()
        A.scope_change(*self.args, "new-scope")
        self.decide()
        with patch.object(R, "_atomic", side_effect=AssertionError("unexpected write")):
            A.scope_change(*self.args, "new-scope")
        self.assertEqual(A.summary(self.state())["status"], "decided")

    def test_initialize_mutates_only_supplied_state_and_old_ids_never_reopen(self):
        state = self.state()
        A.initialize(state, "scope-a", "scope_change")
        A.initialize(state, "scope-b", "scope_change")
        before = deepcopy(state)
        A.initialize(state, "scope-a", "scope_change")
        self.assertEqual(state, before)
        self.assertNotEqual(state, self.state())
        state["tasks"] = [{"task_id": "task-two", "status": "running"}]
        A.initialize(state, "run-start", "run_start")
        self.assertEqual(state["work_allocation"]["task_id"], "task-two")

    def test_task_start_reopens_review_preserving_dispatches_and_decisions(self):
        self.decide()
        binding = self.dispatch()
        self.edit(lambda state: (state.update(tasks=[{"task_id": "next-task", "status": "running"}]),
                                 A.initialize(state, "next-task")))
        state = self.state()
        self.assertEqual(state["work_allocation"]["status"], "pending")
        self.assertEqual(len(state["work_allocation"]["decisions"]), 1)
        self.assertEqual(state["role_dispatches"][0]["allocation_revision"], binding["allocation_revision"])
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "core"))

    def test_all_modes_and_no_forced_three(self):
        for mode in ("direct", "verification"):
            self.decide([], mode)
            self.unchanged_reject(lambda: A.require_dispatch(self.state()))
        self.decide([work("one"), work("two", paths=["src/two.py"])])
        self.assertEqual(len(A.summary(self.state())["items"]), 2)
        self.decide([work(), work("ui", "dev-2", ["src/ui.py"])], "parallel", "independent_work")
        self.assertEqual(A.require_dispatch(self.state(), "ui", "dev-2")["planned_executor_id"], "dev-2")

    def test_invalid_modes_reasons_assignments_and_owner_are_atomic(self):
        owner = self.args[2]
        cases = [("unknown", "small_scope", []), ("direct", "customer-text", []),
                 ("direct", "small_scope", [work()]), ("verification", "small_scope", [work()]),
                 ("serial", "small_scope", []), ("parallel", "independent_work", [work()]),
                 ("serial", "small_scope", [work(executor=owner)]),
                 ("serial", "small_scope", [{**work(), "goal": "raw prose"}]),
                 ("serial", "small_scope", [work(key=owner)])]
        for mode, reason, items in cases:
            with self.subTest(mode=mode, items=items):
                self.unchanged_reject(lambda: A.decide(*self.args, mode, reason, items))

    def test_path_overlap_and_unsafe_lexical_paths_are_atomic(self):
        for path in ("/tmp/file", "../a", "src/../a", "src//a", "src/./a", "src/", ".git/config", "a\\b", "C:/x", "src/*"):
            self.unchanged_reject(lambda: self.decide([work(paths=[path])]))
        for path in ("src", "src/core.py", "src/core.py/child"):
            self.unchanged_reject(lambda: self.decide([work(), work("other", "dev-2", [path])], "parallel"))
        self.decide([work(), work("same-owner", paths=["src"])])

    def test_dag_duplicate_unknown_self_cycle_and_limit(self):
        cases = [[work(), work()], [work(deps=["missing"])], [work(deps=["core"])],
                 [work(deps=["next"]), work("next", paths=["next.py"], deps=["core"])],
                 [work(deps=["next", "next"]), work("next", paths=["next.py"])] ]
        for items in cases:
            self.unchanged_reject(lambda: self.decide(items))
        items = [work(f"part-{i}", f"dev-{i}", [f"src/{i}.py"]) for i in range(4)]
        self.unchanged_reject(lambda: self.decide(items, "parallel"))
        self.decide(items[:3], "parallel")
        self.assertEqual(len(A.summary(self.state())["items"]), 3)

    def test_dependency_completion_and_duplicate_binding(self):
        self.decide([work(), work("second", paths=["second.py"], deps=["core"])])
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "second"))
        self.dispatch(status="completed")
        self.assertEqual(A.require_dispatch(self.state(), "second")["work_item"], "second")
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "core"))
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "second", "wrong"))
        self.unchanged_reject(lambda: A.require_dispatch(self.state()))

    def test_truth_stages_require_matching_dispatch_not_role_events_or_other_revision(self):
        self.decide()
        self.assertEqual(A.summary(self.state())["items"][0]["status"], "planned")
        self.edit(lambda state: state.update(role_events=[{"role": "developer", "event": "created"}],
                    role_dispatches=[{"role": "developer", "work_item": "core", "allocation_revision": 99,
                                      "status": "working", "host_executor_id": "raw-host"}]))
        self.assertEqual(A.summary(self.state())["items"][0]["status"], "planned")
        self.dispatch(host_executor_id="raw-host", owner_token="raw-owner")
        for dispatch_status, expected in (("dispatching", "requested"), ("working", "host_accepted"),
                                          ("completed", "completed"), ("failed", "failed"), ("interrupted", "failed")):
            self.edit(lambda state: state["role_dispatches"][-1].update(status=dispatch_status))
            snapshot = self.state()
            result = A.summary(snapshot)
            self.assertEqual(result["items"][0]["status"], expected)
            self.assertNotIn("raw-host", str(result))
            self.assertNotIn("raw-owner", str(result))
            self.assertEqual(snapshot, self.state())

    def test_scope_redecision_preserves_jobs_and_prevents_live_overlap_or_rewrite(self):
        self.decide()
        binding = self.dispatch()
        A.scope_change(*self.args, "added-ui")
        self.unchanged_reject(lambda: self.decide([work(executor="dev-2")]))
        self.unchanged_reject(lambda: self.decide([work("other", "dev-2", ["src"])]))
        self.decide([work("ui", "dev-2", ["ui.js"])])
        result = A.summary(self.state())
        self.assertEqual([(x["work_item"], x["status"]) for x in result["items"]],
                         [("core", "requested"), ("ui", "planned")])
        self.assertEqual(result["items"][0]["allocation_revision"], binding["allocation_revision"])
        self.assertEqual(A.require_dispatch(self.state(), "ui")["allocation_revision"], 2)
        self.assertEqual(len(self.state()["role_dispatches"]), 1)

    def test_pending_scope_supersedes_unrequested_plan_only(self):
        self.decide([work(), work("later", paths=["later.py"])])
        self.dispatch()
        A.scope_change(*self.args, "reconsider")
        result = A.summary(self.state())
        self.assertEqual(result["status"], "pending")
        self.assertEqual([(x["work_item"], x["status"]) for x in result["items"]], [("core", "requested")])

    def test_completed_paths_reassignable_but_dispatched_identity_immutable(self):
        self.decide()
        self.dispatch(status="completed")
        A.scope_change(*self.args, "next")
        self.decide([work("followup", "dev-2")])
        self.assertEqual(A.require_dispatch(self.state(), "followup")["planned_executor_id"], "dev-2")
        self.unchanged_reject(lambda: self.decide([work(executor="dev-2")]))

    def test_owned_running_and_policy_required(self):
        self.unchanged_reject(lambda: A.scope_change(self.root, self.args[1], "wrong", "changed"))
        for call in (lambda: self.decide(), lambda: A.scope_change(*self.args, "changed")):
            with patch.object(A.run_policy, "require", side_effect=ValueError("policy acknowledgement")):
                self.unchanged_reject(call)
        self.edit(lambda state: state.update(status="paused"))
        self.unchanged_reject(lambda: self.decide())
        self.unchanged_reject(lambda: A.scope_change(*self.args, "changed"))

    def test_new_task_can_reuse_work_ids_without_old_roles_in_projection(self):
        self.edit(lambda state: (state.update(tasks=[{"task_id": "task-one", "status": "running"}]),
                                 A.initialize(state, "task-one")))
        self.decide()
        self.dispatch(status="completed")
        self.edit(lambda state: (state["tasks"][-1].update(status="execution_ended"),
                                 state["tasks"].append({"task_id": "task-two", "status": "running"}),
                                 A.initialize(state, "task-two")))
        self.assertEqual(A.summary(self.state())["items"], [])
        self.decide([work(executor="dev-2")])
        binding = A.require_dispatch(self.state(), "core")
        self.assertEqual(binding["planned_executor_id"], "dev-2")
        self.assertEqual([(x["work_item"], x["status"]) for x in A.summary(self.state())["items"]],
                         [("core", "planned")])
        self.assertEqual(len(self.state()["work_allocation"]["decisions"]), 2)

    def test_prior_task_live_ownership_survives_repeated_work_id(self):
        self.edit(lambda state: (state.update(tasks=[{"task_id": "task-one", "status": "running"}]),
                                 A.initialize(state, "task-one")))
        self.decide()
        self.dispatch(status="working")
        self.edit(lambda state: (state["tasks"][-1].update(status="execution_ended"),
                                 state["tasks"].append({"task_id": "task-two", "status": "running"}),
                                 A.initialize(state, "task-two")))
        self.assertEqual(A.summary(self.state())["items"], [])
        self.unchanged_reject(lambda: self.decide([work(executor="dev-2")]))
        self.edit(lambda state: state["role_dispatches"][-1].update(status="completed"))
        self.decide([work(executor="dev-2")])
        self.assertEqual(A.require_dispatch(self.state(), "core")["planned_executor_id"], "dev-2")

    def test_task_end_freezes_projection_and_blocks_new_work(self):
        self.edit(lambda state: (state.update(tasks=[{"task_id": "task-one", "status": "running"}]),
                                 A.initialize(state, "task-one")))
        self.decide()
        before = A.summary(self.state())
        self.edit(lambda state: state["tasks"][-1].update(status="execution_ended"))
        self.assertEqual(A.summary(self.state()), before)
        self.unchanged_reject(lambda: self.decide())
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "core"))
        self.unchanged_reject(lambda: A.scope_change(*self.args, "late"))

    def test_failed_or_interrupted_request_allows_same_planned_retry(self):
        self.decide()
        original = self.dispatch(status="failed")
        retried = self.dispatch(status="interrupted")
        self.assertEqual(retried, original)
        self.assertEqual(len(self.state()["work_allocation"]["decisions"]), 1)
        self.assertEqual(len(self.state()["role_dispatches"]), 2)
        self.assertEqual(A.summary(self.state())["items"][0]["status"], "failed")
        self.dispatch(status="working")
        self.unchanged_reject(lambda: A.require_dispatch(self.state(), "core"))
        self.assertEqual(A.summary(self.state())["items"][0]["status"], "host_accepted")

    def test_unicode_and_spaced_paths_are_lexical_and_preserve_overlap_checks(self):
        paths = ["产品 文档/需求说明.md", "src/My Component.vue", "assets/café.png"]
        self.decide([work(paths=paths)])
        self.assertEqual(self.state()["work_allocation"]["decisions"][-1]["assignments"][0]["write_paths"], sorted(paths))
        self.unchanged_reject(lambda: self.decide(
            [work(paths=["产品 文档"]), work("other", "dev-2", ["产品 文档/需求说明.md"])], "parallel"))
        for path in ("资料/../内容.md", "/资料/内容.md", "Z:资料.md", "资料\\内容.md", "资料/内容\n.md",
                     "资料/内容\x7f.md", "资料/内容\u0085.md", "资料/内容\u200b.md", "资料/[内容].md", "资料/.git/config"):
            self.unchanged_reject(lambda: self.decide([work(paths=[path])]))

    def test_summary_collapses_revisions_to_latest_actual_attempt(self):
        self.decide()
        self.dispatch(status="failed")
        A.scope_change(*self.args, "retry-review")
        self.decide()
        # A fresh plan does not conceal an actual failed attempt from an earlier revision.
        result = A.summary(self.state())
        self.assertEqual(result["total_items"], 1)
        self.assertEqual(result["items"][0]["allocation_revision"], 1)
        self.assertEqual(result["items"][0]["status"], "failed")
        live_snapshot = self.state()
        live_snapshot["role_dispatches"][-1]["status"] = "working"
        live_result = A.summary(live_snapshot)
        self.assertEqual(live_result["items"][0]["status"], "host_accepted")
        self.assertEqual(live_result["items"][0]["allocation_revision"], 1)
        self.dispatch(status="working")
        result = A.summary(self.state())
        self.assertEqual(result["total_items"], 1)
        self.assertEqual(result["items"][0]["allocation_revision"], 2)
        self.assertEqual(result["items"][0]["status"], "host_accepted")
        self.edit(lambda state: state["role_dispatches"][-1].update(status="failed"))
        self.assertEqual(A.summary(self.state())["counts"], {"failed": 1})
        self.assertEqual(len(self.state()["role_dispatches"]), 2)

    def test_large_history_projection_is_bounded_and_keeps_actual_live_before_plans(self):
        state = self.state()
        allocation = state["work_allocation"]
        allocation.update(status="decided", revision=128)
        state["role_dispatches"] = []
        for revision in range(1, 129):
            assignments = [work(f"work-{revision}-{i}", paths=[f"src/{revision}-{i}.py"]) for i in range(32)]
            allocation["decisions"].append({"revision": revision, "mode": "serial", "reason": "small_scope",
                                            "run_id": state["run_id"], "task_id": None, "assignments": assignments})
            if revision < 128:
                state["role_dispatches"].extend({"role": "developer", "allocation_revision": revision,
                    "work_item": item["id"], "status": "working" if revision == 1 and i == 0 else "completed"}
                    for i, item in enumerate(assignments))
        before = deepcopy(state)
        result = A.summary(state)
        self.assertEqual(result["total_items"], 4096)
        self.assertEqual(len(result["items"]), 32)
        self.assertEqual(result["omitted_items"], 4064)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["items"][0]["work_item"], "work-1-0")
        self.assertEqual(result["items"][0]["status"], "host_accepted")
        self.assertEqual(result["counts"], {"host_accepted": 1, "completed": 4063, "planned": 32})
        self.assertEqual(state, before)
        allocation["status"] = "pending"
        result = A.summary(state)
        self.assertEqual(result["total_items"], 4064)
        self.assertNotIn("planned", result["counts"])

    def test_repeated_work_item_across_128_revisions_has_single_summary_item(self):
        state = self.state()
        allocation = state["work_allocation"]
        allocation.update(status="decided", revision=128)
        state["role_dispatches"] = []
        for revision in range(1, 129):
            allocation["decisions"].append({"revision": revision, "mode": "serial", "reason": "small_scope",
                "run_id": state["run_id"], "task_id": None, "assignments": [work()]})
            state["role_dispatches"].append({"role": "developer", "allocation_revision": revision,
                "work_item": "core", "status": "working" if revision == 128 else "failed"})
        result = A.summary(state)
        self.assertEqual(result["total_items"], 1)
        self.assertEqual(result["counts"], {"host_accepted": 1})
        self.assertEqual(result["items"][0]["allocation_revision"], 128)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["omitted_items"], 0)

    def test_bounded_state_rejects_without_pruning(self):
        self.decide([], "direct")
        with patch.object(A, "MAX_DECISIONS", 1):
            self.unchanged_reject(lambda: self.decide([], "verification"))
        with patch.object(A, "MAX_REVIEWS", 1):
            self.unchanged_reject(lambda: A.scope_change(*self.args, "new"))


if __name__ == "__main__":
    unittest.main()
