"""Builds one class-scope run against fake GitHub state.

A scenario is: main at some go.mod/go.sum (plus the repository's main.go and
a stand-in test file), a pull request head that is main plus edits, the
controls repo's class file and records, issue events and push activity.
"""

from __future__ import annotations

import datetime as dt
import json
import os

from fakes import DATA, FakeGitHub, FakeProxy, FakeRepo, HUMAN, class_scope
import sweep

SVC = "platform-factory/svc-hello"
CONTROLS = "platform-factory/factory-controls"

with open(os.path.join(DATA, "classfile.yaml")) as _f:
    CLASS_TEXT = _f.read()

with open(os.path.join(sweep.REPO_ROOT, "main.go"), "rb") as _f:
    MAIN_GO = _f.read()

STATES = sweep.recorded()           # [(label, {"go.mod":..., "go.sum":...})], baseline first
QUEUE = sweep.QUEUE                 # [(module, version)] x 25

PUSHED = "2026-11-19T10:00:00Z"     # when the head commit was pushed
NOW = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)  # every item older than 7 days


def state(i: int) -> dict[str, bytes]:
    """Recorded end state after queue item i (0 = the go 1.26.0 baseline)."""
    return dict(STATES[i][1])


def sources(mods: dict[str, bytes]) -> dict[str, bytes]:
    return {"go.mod": mods["go.mod"], "go.sum": mods["go.sum"], "main.go": MAIN_GO,
            "main_test.go": sweep.STUB_TEST, "README.md": b"# svc-hello\n"}


class TableRecompute:
    """Fake recompute: (main go.mod, module@version) -> files, from a table.
    Anything not in the table is the check's own failure, so a test cannot
    pass by accident."""

    def __init__(self, table=None, network_fail=False):
        self.table = table or {}
        self.network_fail = network_fail
        self.calls = []

    @staticmethod
    def sweep_table():
        t = {}
        for i, (m, v) in enumerate(QUEUE, start=1):
            t[(state(i - 1)["go.mod"], f"{m}@{v}")] = state(i)
        return t

    def __call__(self, files, module, version):
        self.calls.append((module, version))
        if self.network_fail:
            return class_scope.RecomputeResult(False, network=True, attempts=3,
                                               error="go: golang.org/x/sync@v0.18.0: Get \"https://proxy.golang.org/...\": dial tcp: i/o timeout")
        key = (files["go.mod"], f"{module}@{version}")
        if key not in self.table:
            return class_scope.RecomputeResult(False, attempts=1, error=f"no table entry for {module}@{version}")
        r = self.table[key]
        return class_scope.RecomputeResult(True, r["go.mod"], r["go.sum"], attempts=1)


class Scenario:
    def __init__(self, main_mods: dict[str, bytes], head_mods: dict[str, bytes] | None = None, *,
                 pr: int = 101, action: str = "opened", extra_head: dict[str, bytes] | None = None,
                 drop_head: tuple[str, ...] = (), head_modes: dict[str, str] | None = None,
                 behind: bool = False, class_text: str = CLASS_TEXT, runs=(), seeds=(), pulls=(),
                 events=(), pushed: str | None = PUSHED, base_ref: str = "main",
                 payload_labels=(), now: dt.datetime = NOW, recompute=None, proxy=None, force: str = "",
                 main_parent_mods: dict[str, bytes] | None = None):
        self.svc = FakeRepo(SVC)
        self.controls = FakeRepo(CONTROLS)
        main_files = sources(main_mods)
        self.parent_of_main = self.svc.commit(sources(main_parent_mods or state(0)), branch=None)
        self.main = self.svc.commit(main_files, parents=[self.parent_of_main], branch="main")
        head_files = dict(main_files)
        if head_mods:
            head_files.update(head_mods)
        head_files.update(extra_head or {})
        for p in drop_head:
            head_files.pop(p, None)
        head_parent = self.main
        if behind:
            # main moves on after the head was branched
            head_parent = self.main
            moved = dict(main_files)
            moved["README.md"] = b"# svc-hello\nmoved on\n"
            self.main = self.svc.commit(moved, parents=[head_parent], branch="main")
        self.head = self.svc.commit(head_files, parents=[head_parent], modes=head_modes)
        self.svc.pulls = list(pulls)
        self.svc.events[pr] = list(events)
        if pushed:
            self.svc.activity.append({"id": 1, "activity_type": "push", "ref": "refs/heads/agent/branch",
                                      "before": head_parent, "after": self.head, "timestamp": pushed,
                                      "actor": {"login": "platform-factory-agent[bot]", "type": "Bot"}})
        self.controls.commit({"classes/go-module-bump.yaml": class_text.encode()}, branch="main")
        rec = {}
        for i, r in enumerate(runs):
            rec[f"runs/{1000 + i}-1.json"] = json.dumps(r).encode()
        for n in seeds:
            rec[f"seeds/{n}.json"] = json.dumps({"pr_number": n}).encode()
        if rec:
            self.controls.commit(rec, branch="records")
        self.api = FakeGitHub(self.svc, self.controls)
        self.recompute = recompute or TableRecompute(TableRecompute.sweep_table())
        self.proxy = proxy or FakeProxy()
        self.event = class_scope.Event(repo=SVC, action=action, pr=pr, head_sha=self.head,
                                       head_ref="agent/branch", head_repo=SVC, base_ref=base_ref)
        self.now = now
        self.force = force
        self.payload_labels = list(payload_labels)

    def deps(self):
        return class_scope.Deps(api=self.api, proxy=self.proxy,
                                recompute_factory=lambda image: self.recompute, now=lambda: self.now)

    def decide(self):
        return class_scope.decide(self.event, self.deps(), self.force)

    def run(self):
        return class_scope.run(self.event, self.deps(), self.force, self.payload_labels)


def run_record(pr: int, position: int) -> dict:
    m, v = QUEUE[position - 1]
    return {"schema": 1, "class": "go-module-bump", "queue_position": position, "module": m,
            "version": v, "pr_number": pr, "head_sha": "0" * 40, "body_sha256": "0" * 64,
            "controls_run_id": 7000 + pr, "controls_run_attempt": 1, "job": "open",
            "opened_at": "2026-11-19T09:00:00Z", "repo": SVC, "branch": "agent/branch"}


def pull(number: int, merged: bool, merge_sha: str | None = None, merged_at: str = "2026-11-19T08:00:00Z") -> dict:
    return {"number": number, "state": "closed", "merged_at": merged_at if merged else None,
            "closed_at": merged_at, "merge_commit_sha": merge_sha, "head": {"sha": "1" * 40, "ref": "agent/branch"},
            "base": {"ref": "main"}}


def replace_line(text: bytes, old_prefix: bytes, new_line: bytes) -> bytes:
    out = []
    hit = False
    for line in text.split(b"\n"):
        if line.startswith(old_prefix) and not hit:
            out.append(new_line)
            hit = True
        else:
            out.append(line)
    assert hit, old_prefix
    return b"\n".join(out)
