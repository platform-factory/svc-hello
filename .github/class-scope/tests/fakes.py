"""In-memory stand-ins for GitHub and the module proxy.

FakeGitHub serves the handful of REST paths class_scope.py reads, in the
response shapes recorded from the public API on 2026-11-11 (reduced to the
fields the verifier reads; see data/recorded/ and test_recorded_shapes.py).
Repositories are built from commits holding whole file sets, so trees,
blobs and compares are derived rather than written by hand.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import class_scope  # noqa: E402

DATA = os.path.join(HERE, "data")


def git_sha(kind: str, payload: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(payload)}\0".encode() + payload).hexdigest()


class FakeRepo:
    def __init__(self, name: str):
        self.name = name
        self.commits: dict[str, dict] = {}  # sha -> {"files": {path: bytes}, "parents": [sha], "modes": {}}
        self.branches: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.pulls: list[dict] = []
        self.events: dict[int, list[dict]] = {}
        self.activity: list[dict] = []
        self._n = 0

    def commit(self, files: dict[str, bytes], parents: list[str] | None = None, branch: str | None = None,
               modes: dict[str, str] | None = None) -> str:
        self._n += 1
        body = json.dumps({"n": self._n, "parents": parents or [],
                           "files": {p: hashlib.sha1(b).hexdigest() for p, b in sorted(files.items())}}).encode()
        sha = git_sha("commit", body)
        for b in files.values():
            self.blobs[git_sha("blob", b)] = b
        self.commits[sha] = {"files": dict(files), "parents": list(parents or []), "modes": dict(modes or {})}
        if branch:
            self.branches[branch] = sha
        return sha

    def ancestors(self, sha: str) -> list[str]:
        seen, stack, order = set(), [sha], []
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            order.append(s)
            stack.extend(self.commits[s]["parents"])
        return order

    def tree(self, sha: str, recursive: bool) -> dict:
        c = self.commits[sha]
        entries, dirs = [], set()
        for path, data in sorted(c["files"].items()):
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            if recursive or len(parts) == 1:
                mode = c["modes"].get(path, "100644")
                entries.append({"path": path, "mode": mode, "type": "blob", "sha": git_sha("blob", data)})
        for d in sorted(dirs):
            if recursive or "/" not in d:
                entries.append({"path": d, "mode": "040000", "type": "tree", "sha": git_sha("tree", d.encode())})
        return {"sha": "tree-" + sha, "truncated": False, "tree": entries}

    def compare(self, base: str, head: str) -> dict:
        anc = self.ancestors(head)
        if base == head:
            status, ahead, behind = "identical", 0, 0
        elif base in anc:
            status, ahead, behind = "ahead", anc.index(base), 0
        else:
            status, ahead, behind = "diverged", 1, 1
        bf, hf = self.commits[base]["files"], self.commits[head]["files"]
        files = []
        for p in sorted(set(bf) | set(hf)):
            if p not in bf:
                files.append({"filename": p, "status": "added"})
            elif p not in hf:
                files.append({"filename": p, "status": "removed"})
            elif bf[p] != hf[p]:
                files.append({"filename": p, "status": "modified"})
            elif self.commits[head]["modes"].get(p) != self.commits[base]["modes"].get(p):
                files.append({"filename": p, "status": "changed"})
        return {"status": status, "ahead_by": ahead, "behind_by": behind, "files": files,
                "base_commit": {"sha": base}, "merge_base_commit": {"sha": base if base in anc else None}}


class FakeGitHub:
    """get / paginate / post / delete over a set of FakeRepos. Records every
    write; can be told to fail any path with a status."""

    def __init__(self, *repos: FakeRepo):
        self.repos = {r.name: r for r in repos}
        self.writes: list[tuple[str, str, object]] = []
        self.fail: dict[str, int] = {}   # path prefix -> status to raise
        self.calls: list[str] = []

    def _repo(self, path: str) -> tuple[FakeRepo, str]:
        parts = path.split("/")
        name = parts[2] + "/" + parts[3]
        if name not in self.repos:
            raise class_scope.ApiError(404, {"message": "Not Found"}, path)
        return self.repos[name], "/" + "/".join(parts[4:])

    def _check_fail(self, path: str):
        for prefix, status in self.fail.items():
            if path.startswith(prefix):
                raise class_scope.ApiError(status, {"message": "injected"}, path)

    def get(self, path: str, **params):
        self.calls.append(path)
        self._check_fail(path)
        repo, rest = self._repo(path)
        nf = class_scope.ApiError(404, {"message": "Not Found"}, path)
        if rest.startswith("/git/ref/heads/"):
            b = rest[len("/git/ref/heads/"):]
            if b not in repo.branches:
                raise nf
            return {"ref": f"refs/heads/{b}", "object": {"sha": repo.branches[b], "type": "commit"}}
        if rest.startswith("/git/commits/"):
            sha = rest[len("/git/commits/"):]
            if sha not in repo.commits:
                raise nf
            return {"sha": sha, "tree": {"sha": "tree-" + sha},
                    "parents": [{"sha": p} for p in repo.commits[sha]["parents"]]}
        if rest.startswith("/git/trees/"):
            t = rest[len("/git/trees/"):]
            sha = t[len("tree-"):] if t.startswith("tree-") else repo.branches.get(t, t)
            if sha not in repo.commits:
                raise nf
            return repo.tree(sha, params.get("recursive") == "1")
        if rest.startswith("/git/blobs/"):
            sha = rest[len("/git/blobs/"):]
            if sha not in repo.blobs:
                raise nf
            data = repo.blobs[sha]
            return {"sha": sha, "size": len(data), "encoding": "base64",
                    "content": base64.encodebytes(data).decode()}
        if rest.startswith("/contents/"):
            p = urllib.parse.unquote(rest[len("/contents/"):])
            ref = params.get("ref")
            sha = repo.branches.get(ref, ref) if ref else repo.branches["main"]
            files = repo.commits[sha]["files"]
            if p not in files:
                raise nf
            return {"type": "file", "path": p, "encoding": "base64",
                    "content": base64.encodebytes(files[p]).decode()}
        if rest.startswith("/compare/"):
            base, head = rest[len("/compare/"):].split("...")
            if base not in repo.commits or head not in repo.commits:
                raise nf
            return repo.compare(base, head)
        if rest == "/activity":
            ref = params.get("ref", "")
            return [a for a in repo.activity if not ref or a["ref"] == ref]
        raise nf

    def paginate(self, path: str, **params):
        self.calls.append(path)
        self._check_fail(path)
        repo, rest = self._repo(path)
        if rest.startswith("/issues/") and rest.endswith("/events"):
            return list(repo.events.get(int(rest.split("/")[2]), []))
        if rest == "/pulls":
            state = params.get("state", "open")
            return [p for p in repo.pulls if state == "all" or p["state"] == state]
        raise class_scope.ApiError(404, {"message": "Not Found"}, path)

    def post(self, path: str, body):
        self._check_fail(path)
        self.writes.append(("POST", path, body))
        return [{"name": n} for n in body.get("labels", [])]

    def delete(self, path: str):
        self._check_fail(path)
        self.writes.append(("DELETE", path, None))
        return None


class FakeProxy:
    """.info answers from data/proxy-info (recorded 2026-11-11)."""

    def __init__(self, unreachable: bool = False):
        self.unreachable = unreachable
        self.dir = os.path.join(DATA, "proxy-info")

    def info(self, module: str, version: str) -> dict:
        if self.unreachable:
            raise class_scope.ProxyUnreachable("injected: proxy.golang.org unreachable")
        name = module.replace("/", "_") + "@" + version + ".json"
        with open(os.path.join(self.dir, name)) as f:
            return json.load(f)


def label_event(eid: int, event: str, label: str, login: str, at: str, app: dict | None = None) -> dict:
    """An issue event in the shape of the REST schema `labeled-issue-event`."""
    return {"id": eid, "event": event, "label": {"name": label, "color": "ededed"},
            "actor": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "created_at": at, "performed_via_github_app": app}


AGENT_APP = {"id": 999001, "slug": "platform-factory-agent", "name": "platform-factory-agent"}
HUMAN = "listed-human-test"
