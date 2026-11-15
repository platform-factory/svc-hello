"""class-scope against the real recompute: DockerRecompute in the pinned
builder image, with the module proxy reached from the container. GitHub is
still FakeGitHub. Downloads are cached in one named volume across cases.

On a laptop whose Docker daemon runs in a VM, set CLASS_SCOPE_WORKDIR to a
directory the VM shares (for example one under your home directory).
Skipped, visibly, when no `docker` is on PATH.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

from fakes import class_scope
from helpers import QUEUE, Scenario, sources, state
import sweep

CACHE = sweep.CACHE_VOLUME
IMAGE = sweep.IMAGE


def real_recompute(**kw):
    kw.setdefault("cache_volume", CACHE)
    kw.setdefault("backoff", 0.5)
    return class_scope.DockerRecompute(IMAGE, **kw)


def go(files, script, *args):
    rc, out, mods = sweep.go_in_image(files, script, *args)
    if rc != 0:
        raise AssertionError(f"making the test head failed: {out}")
    return mods


def codes(d):
    return set(d["reason_codes"])


@unittest.skipUnless(shutil.which("docker"), "docker is not on PATH")
class DockerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = os.path.realpath(tempfile.mkdtemp(dir=class_scope.work_base()))
        try:
            open(os.path.join(work, "probe"), "w").close()
            p = subprocess.run(["docker", "run", "--rm", "-v", f"{work}:/w", IMAGE, "test", "-f", "/w/probe"],
                               capture_output=True)
            if p.returncode != 0:
                raise AssertionError(
                    "the Docker daemon cannot see the temporary directory; set CLASS_SCOPE_WORKDIR "
                    f"to a directory it shares ({p.stderr.decode()[-200:]})")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class TestSweepInImage(DockerCase):
    def test_recorded_states_reproduce(self):
        # The agent's procedure, run again: same bytes as data/sweep/.
        got = sweep.states()
        rec = sweep.recorded()
        self.assertEqual([l for l, _ in got], [l for l, _ in rec])
        for (label, g), (_, r) in zip(got, rec):
            with self.subTest(state=label):
                self.assertEqual(g["go.mod"], r["go.mod"])
                self.assertEqual(g["go.sum"], r["go.sum"])

    def test_all_25_items_pass_through_the_real_recompute(self):
        rc = real_recompute()
        for i, (m, v) in enumerate(QUEUE, start=1):
            with self.subTest(item=i, module=m, version=v):
                d = Scenario(state(i - 1), state(i), recompute=rc).decide()
                self.assertEqual((d["verdict"], d["pass_kind"]), ("pass", "class"), d["reasons"])
                self.assertEqual(d["queue_position"], i)
                self.assertTrue(d["recompute"]["ok"])


class TestOffClassInImage(DockerCase):
    """C-15's off-meaning list, each head made with real go commands from the
    go 1.26.0 baseline, whose next queue item is golang.org/x/sync v0.18.0."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base = sources(state(0))
        cls.rc = real_recompute()

    def check(self, head_mods, expect, extra=None):
        d = Scenario(state(0), head_mods, extra_head=extra, recompute=self.rc).decide()
        self.assertEqual(d["verdict"], "fail", d["reasons"])
        self.assertIn(expect, codes(d), d["reasons"])
        return d

    def test_replace_line(self):
        h = go(self.base, 'go get golang.org/x/sync@v0.18.0; '
                          'go mod edit -replace=golang.org/x/text=golang.org/x/text@v0.30.0; go mod tidy')
        self.check(h, "gomod-directive")

    def test_go_directive_change(self):
        h = go(self.base, "go get golang.org/x/sync@v0.18.0; go mod edit -go=1.27.0; go mod tidy")
        self.check(h, "go-directive")

    def test_unforced_second_module(self):
        h = go(self.base, "go get golang.org/x/sync@v0.18.0 github.com/jackc/puddle/v2@v2.2.3; go mod tidy")
        d = self.check(h, "unforced-module")
        self.assertIn("puddle", " ".join(d["reasons"]))

    def test_downgrade(self):
        h = go(self.base, "go get golang.org/x/sync@v0.16.0; go mod tidy")
        self.check(h, "downgrade")

    def test_target_other_than_the_queues(self):
        h = go(self.base, "go get golang.org/x/text@v0.30.0; go mod tidy")
        self.check(h, "wrong-target")

    def test_pseudo_version(self):
        # 1a583072c11b: a golang.org/x/sync commit between v0.19.0 and v0.20.0
        # that carries no tag (read from the GitHub mirror, 2026-11-11).
        h = go(self.base, "go get golang.org/x/sync@1a583072c11b; go mod tidy")
        d = self.check(h, "pseudo-version")
        self.assertRegex(" ".join(d["reasons"]), r"v0\.19\.1-0\.\d{14}-1a583072c11b")

    def test_one_gosum_hash_altered(self):
        h = dict(state(1))
        lines = h["go.sum"].split(b"\n")
        k = next(i for i, l in enumerate(lines) if l.startswith(b"golang.org/x/sync v0.18.0/go.mod"))
        lines[k] = lines[k][:-5] + (b"AAAA=" if not lines[k].endswith(b"AAAA=") else b"BBBB=")
        h["go.sum"] = b"\n".join(lines)
        self.check(h, "gosum-hash")

    def test_nested_go_mod(self):
        self.check(state(1), "path-outside-class", extra={"x/go.mod": b"module example.com/x\n\ngo 1.26.0\n"})

    def test_extra_file(self):
        self.check(state(1), "path-outside-class", extra={"README.md": b"# svc-hello, edited\n"})

    def test_go_get_without_tidy(self):
        h = go(self.base, "go get golang.org/x/sync@v0.18.0")
        if h == state(1):
            self.skipTest("go get alone already gave the tidy result")
        self.check(h, "gosum-extra")

    def test_test_imports_count(self):
        # main carries a test file importing testify; a head made from
        # main.go alone (without the test's import) differs from the
        # recompute, which takes every .go file.
        files = dict(self.base)
        files["main_test.go"] = (b'package main\n\nimport (\n\t"testing"\n\n\t"github.com/stretchr/testify/assert"\n)\n\n'
                                 b'func TestX(t *testing.T) { assert.True(t, true) }\n')
        main_mods = go(files, "go mod tidy")
        right = go({**files, **main_mods}, "go get golang.org/x/sync@v0.18.0; go mod tidy")
        no_test = {k: v for k, v in files.items() if k != "main_test.go"}
        wrong = go({**no_test, **main_mods}, "go get golang.org/x/sync@v0.18.0; go mod tidy")
        self.assertNotEqual(right, wrong)
        res = self.rc({**files, **main_mods}, "golang.org/x/sync", "v0.18.0")
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.go_mod, res.go_sum), (right["go.mod"], right["go.sum"]))


class TestInconclusiveInImage(DockerCase):
    def test_young_release_is_inconclusive_clearable(self):
        d = Scenario(state(24), state(25), recompute=real_recompute(),
                     now=dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc),
                     pushed="2026-11-12T10:00:00Z").decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "clearable"), d["reasons"])
        self.assertIn("release-young", codes(d))
        self.assertTrue(d["recompute"]["ok"])

    def test_proxy_failure_is_inconclusive_rerun(self):
        # A fresh, empty module cache and a proxy address nothing listens on.
        vol = f"class-scope-test-empty-{uuid.uuid4().hex[:8]}"
        try:
            rc = real_recompute(cache_volume=vol, goproxy="http://127.0.0.1:9", retries=2, backoff=0)
            d = Scenario(state(0), state(1), recompute=rc).decide()
        finally:
            subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"), d["reasons"])
        self.assertIn("proxy-unreachable", codes(d))
        self.assertEqual(d["recompute"]["attempts"], 2)

    def test_sum_database_failure_is_inconclusive_rerun(self):
        vol = f"class-scope-test-empty-{uuid.uuid4().hex[:8]}"
        try:
            rc = real_recompute(cache_volume=vol, gosumdb="sum.golang.org https://127.0.0.1:9", retries=2, backoff=0)
            d = Scenario(state(0), state(1), recompute=rc).decide()
        finally:
            subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"), d["reasons"])
        self.assertIn("proxy-unreachable", codes(d))


if __name__ == "__main__":
    unittest.main()
