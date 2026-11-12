"""Produce the queue's 25 end states in the builder image, the way the agent
will: from the `go 1.26.0` baseline (the human pull request before the L1
rung, ADR-0024 §2), `go get <module>@<version>` then `go mod tidy`, item by
item.

    python3 tests/sweep.py --write     # (re)record data/sweep/ with Docker
    python3 tests/sweep.py --check     # re-derive and compare with data/sweep/

The baseline is made the way the readiness note's prerequisite pull request
makes it: the dated comment above the `go` line removed, then
`go mod edit -go=1.26.0 && go mod tidy`.

The .go sources are the repository's main.go plus a stand-in test file that
imports what the real tests import (the standard library and pgx), because
`go mod tidy` counts test imports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SWEEP_DIR = os.path.join(HERE, "data", "sweep")
sys.path.insert(0, os.path.dirname(HERE))

import class_scope  # noqa: E402

IMAGE = class_scope.DEFAULT_BUILDER_IMAGE
CACHE_VOLUME = "class-scope-test-gomodcache"

STUB_TEST = b'''package main

import (
\t"testing"

\t"github.com/jackc/pgx/v5/pgconn"
\t"github.com/jackc/pgx/v5/pgxpool"
)

func TestStandIn(t *testing.T) {
\tif _, err := pgxpool.ParseConfig("host=x user=y"); err != nil {
\t\tt.Fatal(err)
\t}
\t_ = pgconn.PgError{}
}
'''

QUEUE = (
    [("golang.org/x/sync", f"v0.{n}.0") for n in range(18, 26)]
    + [("golang.org/x/text", f"v0.{n}.0") for n in range(30, 45)]
    + [("github.com/jackc/pgx/v5", "v5.12.0"), ("github.com/jackc/pgx/v5", "v5.13.0")]
)


def go_in_image(files: dict[str, bytes], script: str, *args: str, volume: str | None = CACHE_VOLUME,
                goproxy: str = class_scope.DEFAULT_GOPROXY) -> tuple[int, str, dict[str, bytes]]:
    """Run `sh -ec script args...` in the builder image on a copy of files.
    Returns the exit code, the output and go.mod/go.sum afterwards. This is
    the stand-in for the agent's job; class-scope's own recompute is
    class_scope.DockerRecompute."""
    work = os.path.realpath(tempfile.mkdtemp(prefix="sweep-", dir=class_scope.work_base()))
    try:
        for p, b in files.items():
            os.makedirs(os.path.dirname(os.path.join(work, p)), exist_ok=True)
            with open(os.path.join(work, p), "wb") as f:
                f.write(b)
        cmd = ["docker", "run", "--rm", "-v", f"{work}:/w", "-w", "/w",
               "-e", "GOTOOLCHAIN=local", "-e", "GOFLAGS=", "-e", f"GOPROXY={goproxy}"]
        if volume:
            cmd += ["-v", f"{volume}:/go/pkg/mod"]
        cmd += [IMAGE, "sh", "-ec", script, "sh", *args]
        p = subprocess.run(cmd, capture_output=True)
        out = {}
        for name in ("go.mod", "go.sum"):
            fp = os.path.join(work, name)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    out[name] = f.read()
        return p.returncode, (p.stdout + p.stderr).decode(errors="replace"), out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def repo_sources() -> dict[str, bytes]:
    out = {}
    for name in ("go.mod", "go.sum", "main.go"):
        with open(os.path.join(REPO_ROOT, name), "rb") as f:
            out[name] = f.read()
    out["main_test.go"] = STUB_TEST
    return out


def remove_dated_comment(go_mod: bytes) -> bytes:
    """Drop the `//` comment lines directly above the `go` directive."""
    lines = go_mod.decode().split("\n")
    i = next(k for k, l in enumerate(lines) if re.match(r"^go\s", l))
    j = i
    while j > 0 and lines[j - 1].startswith("//"):
        j -= 1
    return "\n".join(lines[:j] + lines[i:]).encode()


def baseline(src: dict[str, bytes]) -> dict[str, bytes]:
    files = dict(src)
    if b"\ngo 1.26.0" not in files["go.mod"]:
        files["go.mod"] = remove_dated_comment(files["go.mod"])
        rc, out, mods = go_in_image(files, "go mod edit -go=1.26.0; go mod tidy")
        if rc != 0:
            raise RuntimeError(out)
        files.update(mods)
    return files


def bump(files: dict[str, bytes], module: str, version: str) -> dict[str, bytes]:
    rc, out, mods = go_in_image(files, 'go get "$1"; go mod tidy', f"{module}@{version}")
    if rc != 0:
        raise RuntimeError(f"{module}@{version}: {out}")
    new = dict(files)
    new.update(mods)
    return new


def states() -> list[tuple[str, dict[str, bytes]]]:
    """[(label, files)] for the baseline and the 25 items, in order."""
    cur = baseline(repo_sources())
    out = [("00-baseline", cur)]
    for i, (m, v) in enumerate(QUEUE, start=1):
        cur = bump(cur, m, v)
        out.append((f"{i:02d}-{m.replace('/', '_')}@{v}", cur))
    return out


def recorded() -> list[tuple[str, dict[str, bytes]]]:
    out = []
    for name in sorted(os.listdir(SWEEP_DIR)):
        d = os.path.join(SWEEP_DIR, name)
        if not os.path.isdir(d):
            continue
        files = {}
        for f in ("go.mod", "go.sum"):
            with open(os.path.join(d, f), "rb") as fh:
                files[f] = fh.read()
        out.append((name, files))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args()
    # Name the real problem before `go` names a misleading one ("go.mod file
    # not found" when the Docker daemon cannot see the work folder).
    problem = class_scope.DockerRecompute(IMAGE).mount_visible()
    if problem:
        print(f"sweep: {problem}", file=sys.stderr)
        return 2
    got = states()
    if a.write:
        if os.path.isdir(SWEEP_DIR):
            shutil.rmtree(SWEEP_DIR)
        manifest = {"image": IMAGE, "states": []}
        for label, files in got:
            d = os.path.join(SWEEP_DIR, label)
            os.makedirs(d)
            for f in ("go.mod", "go.sum"):
                with open(os.path.join(d, f), "wb") as fh:
                    fh.write(files[f])
            manifest["states"].append({"label": label, **{f: hashlib.sha256(files[f]).hexdigest() for f in ("go.mod", "go.sum")}})
        with open(os.path.join(SWEEP_DIR, "manifest.json"), "w") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        print(f"wrote {len(got)} states to {SWEEP_DIR}")
        return 0
    rec = dict(recorded())
    bad = [label for label, files in got if rec.get(label) != {k: files[k] for k in ("go.mod", "go.sum")}]
    print("all states reproduce" if not bad else f"differ: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
