import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import subprocess
import worktrees

spec = importlib.util.spec_from_file_location("review", Path(__file__).with_name("review.py"))
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git/objects").mkdir(parents=True)
        (self.repo / ".git/refs/heads").mkdir(parents=True)
        (self.repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
        (self.repo / ".git/config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")
        self.original = {"story.txt": b"old\nkeep\n", "old name.txt": b"rename me\n",
                         "removed.txt": b"bye\n", "binary.bin": b"\0old"}
        self.base = self.commit_fixture(self.original)
        (self.repo / ".git/refs/heads/main").write_text(self.base + "\n")
        for name, data in self.original.items():
            (self.repo / name).write_bytes(data)
        review.git(self.repo, "read-tree", "HEAD")
        self.out = self.root / "review"

    def object_fixture(self, kind, data):
        raw = f"{kind} {len(data)}\0".encode() + data
        oid = hashlib.sha1(raw).hexdigest()
        path = self.repo / ".git/objects" / oid[:2] / oid[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(raw))
        return oid

    def commit_fixture(self, files, parent=None):
        tree = b"".join(b"100644 " + name.encode() + b"\0" + bytes.fromhex(self.object_fixture("blob", data))
                        for name, data in sorted(files.items()))
        tree_oid = self.object_fixture("tree", tree)
        raw = f"tree {tree_oid}\n" + (f"parent {parent}\n" if parent else "")
        raw += "author Fixture <fixture@example.test> 1 +0000\ncommitter Fixture <fixture@example.test> 1 +0000\n\nFixture\n"
        return self.object_fixture("commit", raw.encode())

    def capture(self, mode="all", **extra):
        args = dict(repo=str(self.repo), output=str(self.out), pr=None, branch=None, base=None, mode=mode)
        args.update(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            review.capture(argparse.Namespace(**args))
        return json.loads((self.out / "snapshot.json").read_text())

    def guide(self, snapshot):
        return {"snapshot": snapshot["fingerprint"], "sections": [{"id": "story", "title": "Story",
                "files": [f["path"] for f in snapshot["files"]], "blocks": [
                    {"type": "diff", "path": "story.txt", "start": 1, "end": 2,
                     "annotations": [{"line": 1, "text": "Changed meaning"}]}]}]}

    def test_working_modes_keep_index_and_worktree_distinct(self):
        (self.repo / "story.txt").write_text("staged\nkeep\n")
        review.git(self.repo, "add", "story.txt")
        (self.repo / "story.txt").write_text("unstaged\nkeep\n")
        (self.repo / "new file.txt").write_text("new without newline")
        all_changes = self.capture()
        staged = self.capture("staged")
        unstaged = self.capture("unstaged")
        find = lambda data, path: next(f for f in data["files"] if f["path"] == path)
        self.assertEqual(find(all_changes, "story.txt")["base"]["text"], "old\nkeep\n")
        self.assertEqual(find(all_changes, "story.txt")["head"]["text"], "unstaged\nkeep\n")
        self.assertEqual(find(staged, "story.txt")["head"]["text"], "staged\nkeep\n")
        self.assertEqual(find(unstaged, "story.txt")["base"]["text"], "staged\nkeep\n")
        self.assertEqual(find(unstaged, "new file.txt")["hunks"][0]["rows"][0]["text"], "new without newline")
        self.assertEqual([f["path"] for f in staged["files"]], ["story.txt"])

    def test_renames_deletions_and_binary_are_not_lost(self):
        (self.repo / "old name.txt").rename(self.repo / "new name.txt")
        (self.repo / "removed.txt").unlink()
        (self.repo / "binary.bin").write_bytes(b"\0new")
        review.git(self.repo, "add", "-A")
        data = self.capture()
        by_path = {f["path"]: f for f in data["files"]}
        self.assertEqual(by_path["new name.txt"]["oldPath"], "old name.txt")
        self.assertEqual(by_path["new name.txt"]["status"], "R100")
        self.assertFalse(by_path["removed.txt"]["head"]["exists"])
        self.assertIsNone(by_path["binary.bin"]["head"]["text"])
        self.assertIn("Binary files", by_path["binary.bin"]["diff"])

    def test_branch_uses_explicit_parent_not_current_checkout(self):
        changed = dict(self.original, **{"story.txt": b"branch\nkeep\n"})
        head = self.commit_fixture(changed, parent=self.base)
        (self.repo / ".git/refs/heads/topic").write_text(head + "\n")
        data = self.capture(branch="topic", base="main")
        self.assertEqual(data["base"], self.base)
        self.assertEqual(data["head"], head)
        self.assertEqual(data["files"][0]["head"]["text"], "branch\nkeep\n")
        self.assertEqual((self.repo / "story.txt").read_text(), "old\nkeep\n")

    def test_editor_targets_match_exact_side_and_detect_later_edits(self):
        (self.repo / "story.txt").write_text("new\nkeep\n")
        data = self.capture()
        target = review.editor_target(data, self.out, "story.txt", "head", 2)
        self.assertTrue(target["checkout"])
        self.assertTrue(target["url"].endswith("/story.txt:2:1"))
        (self.repo / "story.txt").write_text("different\nkeep\n")
        target = review.editor_target(data, self.out, "story.txt", "head", 2)
        self.assertFalse(target["checkout"])
        self.assertIn("sources/head/story.txt", target["path"])
        self.assertEqual(Path(target["path"]).read_text(), "new\nkeep\n")
        base = review.editor_target(data, self.out, "story.txt", "base", 1)
        self.assertEqual(Path(base["path"]).read_text(), "old\nkeep\n")
        with self.assertRaisesRegex(ValueError, "Line outside"):
            review.editor_target(data, self.out, "story.txt", "head", 999)

    def test_guide_rejects_wrong_revision_unknown_files_and_invalid_lines(self):
        (self.repo / "story.txt").write_text("new\nkeep\n")
        snapshot = self.capture()
        guide = self.guide(snapshot)
        review.validate_guide(snapshot, guide)
        invalid = copy.deepcopy(guide)
        invalid["snapshot"] = "other"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            review.validate_guide(snapshot, invalid)
        invalid = copy.deepcopy(guide)
        invalid["sections"][0]["blocks"][0]["end"] = 900
        with self.assertRaisesRegex(ValueError, "outside snapshot"):
            review.validate_guide(snapshot, invalid)
        invalid = copy.deepcopy(guide)
        invalid["sections"][0]["files"] = []
        with self.assertRaisesRegex(ValueError, "absent from section map"):
            review.validate_guide(snapshot, invalid)

    def test_regeneration_retains_notes_and_invalidates_changed_section(self):
        (self.repo / "story.txt").write_text("new\nkeep\n")
        snapshot = self.capture()
        guide = self.guide(snapshot)
        review.write_json(self.out / "guide.json", guide)
        _, loaded = review.load_review(self.out)
        fingerprint = loaded["sections"][0]["fingerprint"]
        saved = {"sections": {"story": {"seen": True, "fingerprint": fingerprint, "notes": "Return here"}}}
        review.write_json(self.out / "state.json", saved)
        (self.repo / "story.txt").write_text("changed again\nkeep\n")
        updated = self.capture()
        guide["snapshot"] = updated["fingerprint"]
        review.write_json(self.out / "guide.json", guide)
        _, loaded = review.load_review(self.out)
        self.assertNotEqual(loaded["sections"][0]["fingerprint"], fingerprint)
        self.assertEqual(json.loads((self.out / "state.json").read_text()), saved)

    def test_snapshot_paths_cannot_escape_output(self):
        for path in ("../private", "/absolute", "a/../../b"):
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                review.safe_path(path)

    def prepare_pr(self, extra=None):
        changed = {**self.original, "story.txt": b"reviewed\nkeep\n", **(extra or {})}
        head = self.commit_fixture(changed, parent=self.base)
        (self.repo / ".git/refs/heads/topic").write_text(head + "\n")
        snapshot = self.capture(branch="topic", base="main")
        snapshot["mode"] = "pr"
        review.write_json(self.out / "snapshot.json", snapshot)
        return snapshot

    def test_pr_worktree_is_detached_pinned_and_cleanup_preserves_review_data(self):
        snapshot = self.prepare_pr()
        entry = worktrees.ensure_checkout(snapshot, self.out)
        checkout = Path(entry["path"])
        self.assertTrue(entry["owned"])
        self.assertEqual(worktrees.git(checkout, "rev-parse", "HEAD").decode().strip(), snapshot["head"])
        self.assertEqual((checkout / "story.txt").read_text(), "reviewed\nkeep\n")
        self.assertEqual((self.repo / "story.txt").read_text(), "old\nkeep\n")
        self.assertIsNotNone(review.editor_target(snapshot, self.out, "story.txt", "head", 1))
        self.assertIn(entry["path"], review.editor_target(snapshot, self.out, "story.txt", "head", 1)["path"])
        self.assertIn("sources/base", review.editor_target(snapshot, self.out, "story.txt", "base", 1)["path"])
        self.assertEqual(worktrees.ensure_checkout(snapshot, self.out)["path"], entry["path"])
        review.write_json(self.out / "state.json", {"notes": "keep my notes"})
        plan = worktrees.cleanup_review(self.out)
        self.assertTrue(plan[0]["eligible"])
        self.assertTrue(checkout.exists())
        result = worktrees.cleanup_review(self.out, apply=True)
        self.assertTrue(result[0]["removed"], result)
        self.assertFalse(checkout.exists())
        self.assertEqual(json.loads((self.out / "state.json").read_text()), {"notes": "keep my notes"})
        self.assertTrue((self.out / "snapshot.json").exists())
        self.assertTrue((self.out / "sources/head/story.txt").exists())
        self.assertIn("sources/head", review.editor_target(snapshot, self.out, "story.txt", "head", 1)["path"])
        reopened = worktrees.ensure_checkout(snapshot, self.out)
        self.assertTrue(Path(reopened["path"]).exists())

    def test_existing_clean_worktree_is_reused_but_never_removed(self):
        snapshot = self.prepare_pr()
        personal = self.root / "personal"
        review.git(self.repo, "worktree", "add", "--detach", str(personal), snapshot["head"])
        entry = worktrees.ensure_checkout(snapshot, self.out)
        self.assertFalse(entry["owned"])
        self.assertEqual(Path(entry["path"]), personal.resolve())
        result = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(result[0]["removed"])
        self.assertIn("never removes", result[0]["reason"])
        self.assertTrue(personal.exists())

    def test_dirty_existing_worktree_is_left_unchanged(self):
        snapshot = self.prepare_pr()
        personal = self.root / "personal"
        review.git(self.repo, "worktree", "add", "--detach", str(personal), snapshot["head"])
        (personal / "story.txt").write_text("my local work\n")
        entry = worktrees.ensure_checkout(snapshot, self.out)
        self.assertTrue(entry["owned"])
        self.assertNotEqual(Path(entry["path"]), personal.resolve())
        self.assertEqual((personal / "story.txt").read_text(), "my local work\n")

    def test_local_review_does_not_create_a_worktree(self):
        (self.repo / "story.txt").write_text("edit\n")
        snapshot = self.capture()
        entry = worktrees.ensure_checkout(snapshot, self.out)
        self.assertEqual(entry["kind"], "local")
        self.assertEqual(len(worktrees.worktree_list(self.repo)), 1)
        self.assertFalse((self.out / "worktrees.json").exists())

    def test_active_session_blocks_cleanup_until_released(self):
        snapshot = self.prepare_pr()
        entry = worktrees.ensure_checkout(snapshot, self.out)
        session = worktrees.Session(self.out)
        self.addCleanup(session.close)
        blocked = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(blocked[0]["removed"])
        self.assertIn("active", blocked[0]["reason"])
        session.close()
        self.assertTrue(worktrees.cleanup_review(self.out)[0]["eligible"])
        self.assertTrue(Path(entry["path"]).exists())

    def test_cleanup_protects_tracked_untracked_and_ignored_files(self):
        snapshot = self.prepare_pr()
        checkout = Path(worktrees.ensure_checkout(snapshot, self.out)["path"])
        (self.repo / ".git/info").mkdir(exist_ok=True)
        (self.repo / ".git/info/exclude").write_text("private-notes\n")
        for name, contents in (("story.txt", "modified\n"), ("scratch.txt", "untracked"), ("private-notes", "ignored")):
            with self.subTest(name=name):
                path = checkout / name
                previous = path.read_bytes() if path.exists() else None
                path.write_text(contents)
                if name == "private-notes":
                    self.assertEqual(worktrees.status(checkout), b"")
                result = worktrees.cleanup_review(self.out, apply=True)
                self.assertFalse(result[0]["removed"])
                self.assertIn("files", result[0]["reason"])
                self.assertEqual(path.read_text(), contents)
                if previous is None:
                    path.unlink()
                else:
                    path.write_bytes(previous)

    def test_cleanup_protects_local_commit_history_even_after_resetting_head(self):
        snapshot = self.prepare_pr()
        checkout = Path(worktrees.ensure_checkout(snapshot, self.out)["path"])
        other = self.commit_fixture({**self.original, "story.txt": b"local commit\n"}, parent=snapshot["head"])
        review.git(checkout, "update-ref", "HEAD", other)
        moved = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(moved[0]["removed"])
        review.git(checkout, "update-ref", "HEAD", snapshot["head"])
        reset = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(reset[0]["removed"])
        self.assertIn("history", reset[0]["reason"])
        self.assertTrue(checkout.exists())

    def test_cleanup_refuses_missing_ownership_marker(self):
        snapshot = self.prepare_pr()
        checkout = Path(worktrees.ensure_checkout(snapshot, self.out)["path"])
        (worktrees.git_dir(checkout) / worktrees.OWNER_FILE).unlink()
        result = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(result[0]["removed"])
        self.assertTrue(checkout.exists())

    def test_bulk_cleanup_requires_14_days_and_leaves_recent_checkout(self):
        snapshot = self.prepare_pr()
        first = worktrees.ensure_checkout(snapshot, self.out)
        other_out = (self.root / "other-review").resolve()
        other = worktrees.ensure_checkout(snapshot, other_out)
        self.assertNotEqual(first["path"], other["path"])
        registry = worktrees.read_registry(self.out)
        registry["entries"][0]["lastOpenedAt"] = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        worktrees.write_registry(self.out, registry)
        planned = worktrees.cleanup_unused(self.root)
        self.assertEqual(sum(item["eligible"] for item in planned), 1)
        self.assertTrue(Path(first["path"]).exists())
        result = worktrees.cleanup_unused(self.root, apply=True)
        self.assertEqual(sum(item["removed"] for item in result), 1)
        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(other["path"]).exists())

    def test_bazel_cleanup_runs_before_git_removal_and_failure_keeps_worktree(self):
        snapshot = self.prepare_pr({"MODULE.bazel": b"module(name = 'fixture')\n"})
        checkout = Path(worktrees.ensure_checkout(snapshot, self.out)["path"])
        real_run = subprocess.run
        events = []
        def execute(args, **kwargs):
            if args[0] == "bazel":
                events.append("bazel")
                self.assertEqual(args, ["bazel", "clean", "--expunge", "--async"])
                self.assertEqual(Path(kwargs["cwd"]), checkout)
                return subprocess.CompletedProcess(args, 1, b"", b"fixture failure")
            return real_run(args, **kwargs)
        with patch.object(worktrees.subprocess, "run", side_effect=execute):
            failed = worktrees.cleanup_review(self.out, apply=True)
        self.assertFalse(failed[0]["removed"])
        self.assertIn("Bazel cleanup failed", failed[0]["reason"])
        self.assertTrue(checkout.exists())
        def succeed(args, **kwargs):
            if args[0] == "bazel":
                events.append("bazel-success")
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if "remove" in args:
                self.assertEqual(events[-1], "bazel-success")
                self.assertNotIn("--force", args)
            return real_run(args, **kwargs)
        with patch.object(worktrees.subprocess, "run", side_effect=succeed):
            removed = worktrees.cleanup_review(self.out, apply=True)
        self.assertTrue(removed[0]["removed"], removed)
        self.assertFalse(checkout.exists())


if __name__ == "__main__":
    unittest.main()
