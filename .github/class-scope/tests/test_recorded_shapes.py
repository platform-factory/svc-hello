"""The readers accept GitHub's real response shapes.

data/recorded/ holds responses read from the public API on 2026-11-11
(svc-hello at 312b18d, PR #25, the repository activity log), reduced to the
fields class_scope.py reads, with logins replaced. A replay object serves
them by path, so these tests exercise the same reader functions the workflow
uses, against what GitHub actually returned.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest

from fakes import DATA, class_scope

REC = os.path.join(DATA, "recorded")
REPO = "platform-factory/svc-hello"
MAIN = "312b18dd0ef06d1b3e04824171177e696a518063"
TREE = "d0f70de3c1b6c60df45e5ce693d3e40a8cebaf52"


def load(name):
    with open(os.path.join(REC, name)) as f:
        return json.load(f)


class Replay:
    def __init__(self):
        self.paths = {
            f"/repos/{REPO}/git/ref/heads/main": load("git-ref-heads-main.json"),
            f"/repos/{REPO}/git/commits/{MAIN}": load("git-commit-312b18d.json"),
            f"/repos/{REPO}/git/trees/{TREE}": load("git-tree-root-312b18d.json"),
            f"/repos/{REPO}/compare/76a6294...312b18d": load("compare-76a6294...312b18d.json"),
            f"/repos/{REPO}/activity": load("activity.json"),
            f"/repos/{REPO}/issues/25/events": load("issue-events-pr25.json"),
            f"/repos/{REPO}/pulls": load("pulls-closed.json"),
        }
        for name in ("git-blob-gomod-312b18d.json", "git-blob-gosum-312b18d.json"):
            blob = load(name)
            self.paths[f"/repos/{REPO}/git/blobs/{blob['sha']}"] = blob
        self.recursive = load("git-tree-recursive-312b18d.json")

    def get(self, path, **params):
        if path == f"/repos/{REPO}/git/trees/{TREE}" and params.get("recursive") == "1":
            return self.recursive
        if path not in self.paths:
            raise class_scope.ApiError(404, "not recorded", path)
        return self.paths[path]

    def paginate(self, path, **params):
        return self.get(path)


class TestRecordedShapes(unittest.TestCase):
    def setUp(self):
        self.api = Replay()

    def test_branch_commit_tree_blob(self):
        self.assertEqual(class_scope.branch_tip(self.api, REPO, "main"), MAIN)
        info = class_scope.commit_info(self.api, REPO, MAIN)
        self.assertEqual(info["tree"], TREE)
        self.assertEqual(len(info["parents"]), 2)
        files, modes = class_scope.class_files_at(self.api, REPO, MAIN)
        self.assertEqual(modes["go.mod"], "100644 blob")
        # The recorded blob is the go.mod this worktree has at 312b18d.
        here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "go.mod"))
        with open(here, "rb") as f:
            self.assertEqual(files["go.mod"], f.read())
        with open(here[:-len("go.mod")] + "go.sum", "rb") as f:
            self.assertEqual(files["go.sum"], f.read())

    def test_recursive_tree_lists_go_sources(self):
        entries = class_scope.tree_entries(self.api, REPO, TREE, recursive=True)
        paths = {e["path"] for e in entries if e["type"] == "blob"}
        self.assertTrue({"go.mod", "go.sum", "main.go"} <= paths)

    def test_compare_fields(self):
        c = self.api.get(f"/repos/{REPO}/compare/76a6294...312b18d")
        self.assertEqual((c["status"], c["behind_by"]), ("ahead", 0))
        self.assertEqual([(f["filename"], f["status"]) for f in c["files"]], [("claims.yaml", "modified")])

    def test_activity_gives_push_time(self):
        t = class_scope.head_pushed_at(self.api, REPO, "m2b-cleanup-second",
                                       "09541bd2a543f0bf05bf8778373b455e997fdb5d")
        self.assertEqual(t, dt.datetime(2026, 10, 29, 6, 19, 38, tzinfo=dt.timezone.utc))
        self.assertIsNone(class_scope.head_pushed_at(self.api, REPO, "x", "0" * 40))

    def test_issue_events_and_closed_pulls(self):
        events = self.api.paginate(f"/repos/{REPO}/issues/25/events")
        state, unknown = class_scope.labels_in_force(events, {"maintainer-account"}, None)
        self.assertEqual(state, {"human-cleared": None, "escape:revert": None})
        self.assertEqual(unknown, [])
        # A merged pull request has a `closed` event too; near_miss_positions
        # therefore reads merged_at from the pull list for closed ones.
        self.assertIn("closed", [e["event"] for e in events])
        pulls = self.api.paginate(f"/repos/{REPO}/pulls")
        self.assertTrue(all("merged_at" in p and "merge_commit_sha" in p for p in pulls))
        runs = [{"pr_number": p["number"], "queue_position": i + 1} for i, p in enumerate(pulls)]
        self.assertEqual(class_scope.near_miss_positions(runs, set(), pulls, lambda n: False),
                         {i + 1: p["number"] for i, p in enumerate(pulls) if not p["merged_at"]})


if __name__ == "__main__":
    unittest.main()
