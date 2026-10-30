"""Unit tests for the handoff core library (standard library only)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from handofflib import config, locks, tasks as tasklib, util, yamlio  # noqa: E402


class YamlSubsetTests(unittest.TestCase):
    def test_roundtrip_document(self):
        data = {
            "task_id": "combat-0142",
            "title": "实现轻攻击三段连击",
            "status": "in_progress",
            "commit": "a84f921",
            "empty": None,
            "empty_list": [],
            "empty_map": {},
            "goal": "第一行：实现 Combo\n第二行 # 带井号",
            "trailing_newline": "ends with newline\n",
            "list": ["ComboState 已建立", "带: 冒号", "- 横线开头", "a # b"],
            "verification": {"build": "pass", "tests": "partial"},
            "commands": [
                {"name": "rules", "command": "pwsh -NoProfile -File scripts/Test.ps1", "required": "true"},
                {"name": "empty", "command": "echo 1", "required": "false"},
            ],
            "numbers_as_strings": ["1234", "1.5"],
        }
        text = yamlio.dumps(data)
        self.assertEqual(yamlio.loads(text), data)
        # every scalar stays a string: no YAML type surprises
        self.assertIsInstance(yamlio.loads(text)["commit"], str)

    def test_multiline_is_a_block_scalar(self):
        text = yamlio.dumps({"goal": "line one\nline two"})
        self.assertIn("goal: |-", text)
        self.assertEqual(yamlio.loads(text)["goal"], "line one\nline two")

    def test_trailing_newline_uses_clip(self):
        text = yamlio.dumps({"notes": "text\n"})
        self.assertIn("notes: |", text)
        self.assertEqual(yamlio.loads(text)["notes"], "text\n")

    def test_hand_written_yaml(self):
        text = """
# a comment
task_id: 'combat-0142'   # trailing comment
title: "带引号的标题"
dependencies: [combat-0130, combat-0131]
verification:
  build: pass
  tests: pass
nested:
  list:
    - one
    - two
"""
        data = yamlio.loads(text)
        self.assertEqual(data["task_id"], "combat-0142")
        self.assertEqual(data["title"], "带引号的标题")
        self.assertEqual(data["dependencies"], ["combat-0130", "combat-0131"])
        self.assertEqual(data["verification"]["tests"], "pass")
        self.assertEqual(data["nested"]["list"], ["one", "two"])

    def test_folded_scalar(self):
        data = yamlio.loads("goal: >\n  one\n  two\n")
        self.assertEqual(data["goal"], "one two\n")

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(yamlio.YamlError):
            yamlio.loads("a: 1\na: 2\n")

    def test_rejects_tabs(self):
        with self.assertRaises(yamlio.YamlError):
            yamlio.loads("a:\n\tb: 1\n")

    def test_rejects_anchors(self):
        with self.assertRaises(yamlio.YamlError):
            yamlio.loads("a: &anchor 1\n")

    def test_rejects_list_of_multiline_scalars(self):
        with self.assertRaises(TypeError):
            yamlio.dumps({"items": ["a\nb"]})

    def test_rejects_non_string_scalars(self):
        with self.assertRaises(TypeError):
            yamlio.dumps({"count": 3})


class TaskSchemaTests(unittest.TestCase):
    def _base(self, **overrides):
        data = {
            "task_id": "combat-0142",
            "title": "三段连击",
            "goal": "实现三段轻攻击",
            "owner": "codex",
            "status": "in_progress",
            "created_at": util.iso(),
            "updated_at": util.iso(),
        }
        data.update(overrides)
        return data

    def test_valid_task(self):
        task = tasklib.Task(self._base())
        self.assertEqual(task.validate(), [])

    def test_invalid_id(self):
        task = tasklib.Task(self._base(task_id="Combat 0142"))
        self.assertTrue(any("task_id" in problem for problem in task.validate()))

    def test_invalid_status(self):
        task = tasklib.Task(self._base(status="doing"))
        self.assertTrue(any("status" in problem for problem in task.validate()))

    def test_missing_goal(self):
        task = tasklib.Task(self._base(goal=""))
        self.assertTrue(any("goal" in problem for problem in task.validate()))

    def test_invalid_commit(self):
        task = tasklib.Task(self._base(commit="not-a-sha"))
        self.assertTrue(any("commit" in problem for problem in task.validate()))

    def test_drop_todo_by_unique_prefix(self):
        task = tasklib.Task(self._base(remaining=["第三段攻击", "stamina 消耗", "hitbox 测试"]))
        remaining, hits = task.drop_matching("remaining", "hitbox")
        self.assertEqual(hits, 1)
        self.assertEqual(remaining, ["第三段攻击", "stamina 消耗"])
        remaining, hits = task.drop_matching("remaining", "攻")
        self.assertEqual(hits, 0)
        self.assertEqual(len(remaining), 2)

    def test_yaml_roundtrip_preserves_unknown_fields(self):
        task = tasklib.Task(self._base(custom_field="keep me"))
        text = task.to_yaml()
        self.assertIn("custom_field", text)
        reloaded = tasklib.Task(yamlio.loads(text))
        self.assertEqual(reloaded.get("custom_field"), "keep me")

    def test_decide_mode_clean(self):
        evidence = {
            "problems": [],
            "lock": {"state": "none"},
            "tree_diff": False,
            "head_drift": False,
            "has_commit": True,
        }
        self.assertEqual(tasklib.decide_mode(evidence)[0], "CLEAN")

    def test_decide_mode_recovery(self):
        evidence = {
            "problems": [],
            "lock": {"state": "stale"},
            "tree_diff": True,
            "recovery_signals": ["8 paths changed after the checkpoint"],
            "has_commit": True,
        }
        mode, reasons = tasklib.decide_mode(evidence)
        self.assertEqual(mode, "RECOVERY")
        self.assertIn("8 paths", reasons[0])

    def test_decide_mode_blocked(self):
        evidence = {
            "problems": [],
            "lock": {"state": "live_foreign", "agent": "codex", "age_human": "2m"},
            "tree_diff": True,
            "has_commit": True,
        }
        self.assertEqual(tasklib.decide_mode(evidence)[0], "BLOCKED")

    def test_decide_mode_own_live_lock_is_not_blocked(self):
        evidence = {
            "problems": [],
            "lock": {"state": "live_same", "agent": "tester"},
            "tree_diff": False,
            "head_drift": False,
            "has_commit": True,
        }
        self.assertEqual(tasklib.decide_mode(evidence)[0], "CLEAN")

    def test_decide_mode_diverged(self):
        evidence = {
            "problems": [],
            "lock": {"state": "none"},
            "checkpoint_commit_exists": False,
            "has_commit": True,
        }
        self.assertEqual(tasklib.decide_mode(evidence)[0], "DIVERGED")

    def test_decide_mode_corrupt(self):
        evidence = {"problems": ["status is invalid"], "lock": {"state": "none"}}
        self.assertEqual(tasklib.decide_mode(evidence)[0], "CORRUPT")


class TempProject:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="handoff-unit-")
        os.makedirs(os.path.join(self.root, ".agent-state", "locks"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        util.write_text(os.path.join(self.root, "src", "a.txt"), "one\n")
        self.context = config.Context(
            self.root, dict(config.DEFAULT_CONFIG), None, "tester", "argument"
        )

    def cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


class LockTests(unittest.TestCase):
    def test_finished_command_pid_does_not_expire_foreign_lock(self):
        project = TempProject()
        try:
            path = os.path.join(project.context.lock_dir(), "combat-0001.lock")
            yamlio.dump({"task_id": "combat-0001", "agent": "codex", "machine": project.context.machine,
                         "pid": "999999", "started_at": util.iso(), "heartbeat": util.iso()}, path)
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "live_foreign")
            self.assertFalse(locks.can_take(project.context, state, False)[0])
        finally:
            project.cleanup()

    def test_missing_heartbeat_is_not_automatically_takeable(self):
        project = TempProject()
        try:
            path = os.path.join(project.context.lock_dir(), "combat-0001.lock")
            yamlio.dump({"task_id": "combat-0001", "agent": "codex", "machine": "other"}, path)
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "corrupt")
            self.assertFalse(locks.can_take(project.context, state, False)[0])
        finally:
            project.cleanup()

    def test_missing_lock(self):
        project = TempProject()
        try:
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "none")
        finally:
            project.cleanup()

    def test_live_foreign_lock(self):
        project = TempProject()
        try:
            locks.write(project.context, {"task_id": "combat-0001", "branch": "feature/x"}, "codex")
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "live_foreign")
            self.assertEqual(state["agent"], "codex")
        finally:
            project.cleanup()

    def test_stale_by_heartbeat(self):
        project = TempProject()
        try:
            path = os.path.join(project.context.lock_dir(), "combat-0001.lock")
            yamlio.dump(
                {
                    "task_id": "combat-0001",
                    "agent": "codex",
                    "machine": util.hostname(),
                    "pid": "999999",
                    "started_at": "2020-01-01T00:00:00Z",
                    "heartbeat": "2020-01-01T00:00:00Z",
                    "branch": "feature/x",
                },
                path,
            )
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "stale")
            self.assertTrue(any("ttl" in reason or "old" in reason for reason in state["reasons"]))
            allowed, reason = locks.can_take(project.context, state, force=False)
            self.assertTrue(allowed, reason)
        finally:
            project.cleanup()

    def test_corrupt_lock_requires_force(self):
        project = TempProject()
        try:
            path = os.path.join(project.context.lock_dir(), "combat-0001.lock")
            util.write_text(path, "this: [is not: valid\n")
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "corrupt")
            allowed, _ = locks.can_take(project.context, state, force=False)
            self.assertFalse(allowed)
            allowed, _ = locks.can_take(project.context, state, force=True)
            self.assertTrue(allowed)
        finally:
            project.cleanup()

    def test_own_lock_is_live_same(self):
        project = TempProject()
        try:
            locks.write(project.context, {"task_id": "combat-0001", "branch": "feature/x"}, "tester")
            state = locks.analyze(project.context, "combat-0001")
            self.assertEqual(state["state"], "live_same")
            refreshed, reason = locks.refresh(project.context, "combat-0001")
            self.assertTrue(refreshed, reason)
        finally:
            project.cleanup()

    def test_foreign_lock_is_not_refreshed(self):
        project = TempProject()
        try:
            locks.write(project.context, {"task_id": "combat-0001", "branch": "feature/x"}, "codex")
            refreshed, reason = locks.refresh(project.context, "combat-0001", agent="tester")
            self.assertFalse(refreshed)
            self.assertIn("codex", reason)
        finally:
            project.cleanup()


class DiscoveryTests(unittest.TestCase):
    """Project discovery must never escape the surrounding git work tree."""

    def setUp(self):
        self.outer = tempfile.mkdtemp(prefix="handoff-discovery-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.outer, ignore_errors=True)

    def test_walks_up_inside_a_work_tree(self):
        os.makedirs(os.path.join(self.outer, ".agent-state"))
        os.makedirs(os.path.join(self.outer, ".git"))
        deep = os.path.join(self.outer, "src", "deep")
        os.makedirs(deep)
        self.assertEqual(
            os.path.realpath(config.find_project_root(deep)),
            os.path.realpath(self.outer),
        )

    def test_nested_repo_does_not_inherit_parent_state(self):
        os.makedirs(os.path.join(self.outer, ".agent-state"))
        inner = os.path.join(self.outer, "inner")
        os.makedirs(os.path.join(inner, ".git"))
        self.assertIsNone(
            config.find_project_root(inner),
            "a nested work tree without .agent-state must not resolve to its parent",
        )
        os.makedirs(os.path.join(inner, ".agent-state"))
        self.assertEqual(
            os.path.realpath(config.find_project_root(inner)),
            os.path.realpath(inner),
        )

    def test_missing_state_is_reported_clearly(self):
        empty = os.path.join(self.outer, "empty")
        os.makedirs(empty)
        with self.assertRaises(config.ConfigError) as caught:
            config.make_context(start=empty, agent="tester", require_git=False)
        self.assertIn("agent-handoff", str(caught.exception))

    def test_protected_branches_are_parsed(self):
        context = config.Context(
            self.outer, dict(config.DEFAULT_CONFIG), None, "tester", "argument"
        )
        self.assertEqual(context.protected_branches(), ["main", "master", "trunk"])
        custom = dict(config.DEFAULT_CONFIG)
        custom["git"] = {"protected_branches": "release/*, main  trunk"}
        context = config.Context(self.outer, custom, None, "tester", "argument")
        self.assertEqual(context.protected_branches(), ["release/*", "main", "trunk"])


class UtilTests(unittest.TestCase):
    def test_iso_roundtrip(self):
        stamp = util.iso()
        self.assertIsNotNone(util.parse_iso(stamp))
        self.assertLess(util.age_seconds(stamp), 5)

    def test_as_bool(self):
        self.assertTrue(util.as_bool("true"))
        self.assertTrue(util.as_bool("YES"))
        self.assertFalse(util.as_bool("false"))
        self.assertFalse(util.as_bool(None))

    def test_split_list(self):
        self.assertEqual(util.split_list("a, b;c"), ["a", "b", "c"])

    def test_dedupe(self):
        self.assertEqual(util.dedupe(["a", "b", "a"]), ["a", "b"])

    def test_pid_alive_for_self_and_bogus(self):
        self.assertTrue(util.pid_alive(os.getpid()))
        self.assertIn(util.pid_alive(999999), (False, None))


if __name__ == "__main__":
    unittest.main()
