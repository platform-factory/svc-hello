"""Offline tests of class-scope's decision. No GitHub, no network, no Docker:
GitHub is FakeGitHub, the proxy answers from recorded .info files, and the
recompute returns the recorded end states in data/sweep/ (which
test_docker.py re-derives in the builder image)."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import AGENT_APP, DATA, HUMAN, FakeGitHub, FakeProxy, FakeRepo, class_scope, label_event
from helpers import (NOW, PUSHED, QUEUE, SVC, Scenario, TableRecompute, pull, replace_line,
                     run_record, state)

BEFORE_PUSH = "2026-11-19T09:00:00Z"
AFTER_PUSH = "2026-11-19T11:00:00Z"
LATER = "2026-11-19T12:00:00Z"


def read_text(path):
    with open(path) as f:
        return f.read()


def codes(d):
    return set(d["reason_codes"])


class TestQueueSweep(unittest.TestCase):
    def test_every_queue_item_in_order_passes(self):
        for i, (m, v) in enumerate(QUEUE, start=1):
            with self.subTest(item=i, module=m, version=v):
                d = Scenario(state(i - 1), state(i)).decide()
                self.assertEqual(d["verdict"], "pass", d["reasons"])
                self.assertEqual(d["pass_kind"], "class")
                self.assertEqual((d["queue_position"], d["module"], d["new"]), (i, m, v))
                self.assertEqual(d["reasons"], [])

    def test_forced_modules_are_reported(self):
        d = Scenario(state(23), state(24)).decide()
        self.assertEqual(d["forced"], [{"module": "github.com/jackc/puddle/v2", "old": "v2.2.2", "new": "v2.2.3"}])
        self.assertEqual(d["old"], "v5.11.0")
        self.assertEqual(len(d["gosum_change"]["removed"]) + len(d["gosum_change"]["added"]), 8)

    def test_queue_done(self):
        d = Scenario(state(25), state(24)).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("queue-exhausted", codes(d))


class TestOffClassWithoutRecompute(unittest.TestCase):
    """Cases decided by path or history alone, and text edits of the correct
    bump. The Docker tests make the go-produced cases for real."""

    def test_extra_file(self):
        d = Scenario(state(0), state(1), extra_head={"README.md": b"# changed\n"}).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("path-outside-class", codes(d))
        self.assertIn("README.md", " ".join(d["reasons"]))

    def test_nested_go_mod(self):
        d = Scenario(state(0), state(1), extra_head={"x/go.mod": b"module x\n\ngo 1.26.0\n"}).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("path-outside-class", codes(d))
        self.assertIn("x/go.mod", " ".join(d["reasons"]))

    def test_replace_line(self):
        h = state(1)
        h["go.mod"] += b"\nreplace golang.org/x/text => golang.org/x/text v0.30.0\n"
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("gomod-directive", codes(d))
        self.assertIn("replace", " ".join(d["reasons"]))

    def test_go_directive_change(self):
        h = state(1)
        h["go.mod"] = h["go.mod"].replace(b"\ngo 1.26.0\n", b"\ngo 1.27.0\n")
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("go-directive", codes(d))

    def test_one_gosum_hash_altered(self):
        h = state(1)
        lines = h["go.sum"].split(b"\n")
        k = next(i for i, l in enumerate(lines) if l.startswith(b"golang.org/x/sync v0.18.0 h1:"))
        mod, ver, hsh = lines[k].split(b" ")
        lines[k] = b" ".join([mod, ver, b"h1:" + (b"A" if hsh[3:4] != b"A" else b"B") + hsh[4:]])
        h["go.sum"] = b"\n".join(lines)
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("gosum-hash", codes(d))

    def test_appended_not_replaced_gosum_lines(self):
        h = state(1)
        old = [l for l in state(0)["go.sum"].split(b"\n") if l.startswith(b"golang.org/x/sync v0.17.0")]
        h["go.sum"] = h["go.sum"] + b"\n".join(old) + b"\n"
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("gosum-extra", codes(d))

    def test_indirect_flip(self):
        h = state(1)
        h["go.mod"] = h["go.mod"].replace(b"golang.org/x/sync v0.18.0 // indirect", b"golang.org/x/sync v0.18.0")
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("indirect-flip", codes(d))

    def test_crlf(self):
        h = state(1)
        h["go.mod"] = h["go.mod"].replace(b"\n", b"\r\n")
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("line-endings", codes(d))

    def test_target_other_than_the_queues(self):
        # x/text v0.30.0 from the baseline, by hand from the recorded state
        # that made the same move later in the queue.
        h = state(0)
        h["go.mod"] = h["go.mod"].replace(b"golang.org/x/text v0.29.0", b"golang.org/x/text v0.30.0")
        s9 = [l for l in state(9)["go.sum"].split(b"\n") if l.startswith(b"golang.org/x/text v0.30.0")]
        lines = [l for l in h["go.sum"].split(b"\n") if not l.startswith(b"golang.org/x/text v0.29.0")]
        h["go.sum"] = b"\n".join(sorted([l for l in lines if l] + s9)) + b"\n"
        d = Scenario(state(0), h).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("wrong-target", codes(d))

    def test_head_behind_main(self):
        d = Scenario(state(0), state(1), behind=True).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertEqual(d["reason_codes"], ["head-behind-main"])

    def test_go_sum_deleted(self):
        d = Scenario(state(0), state(1), drop_head=("go.sum",)).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("class-file-status", codes(d))

    def test_go_mod_symlink(self):
        d = Scenario(state(0), state(1), head_modes={"go.mod": "120000"}).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertTrue(codes(d) & {"class-file-status", "class-file-mode"})

    def test_base_not_main(self):
        d = Scenario(state(0), state(1), base_ref="agent/other").decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("base-not-main", codes(d))

    def test_clearance_never_passes_a_mismatch(self):
        h = state(1)
        h["go.mod"] += b"\nreplace golang.org/x/text => golang.org/x/text v0.30.0\n"
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        d = Scenario(state(0), h, events=ev, force="golang.org/x/sync@v0.18.0").decide()
        self.assertEqual(d["verdict"], "fail")


class TestInconclusive(unittest.TestCase):
    def young(self, **kw):
        # pgx v5.13.0 was released 2026-11-11T18:26:18Z
        return Scenario(state(24), state(25), now=dt.datetime(2026, 11, 12, 12, tzinfo=dt.timezone.utc),
                        pushed="2026-11-12T10:00:00Z", **kw)

    def test_young_release_is_clearable(self):
        d = self.young().decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "clearable"))
        self.assertIn("release-young", codes(d))
        self.assertEqual(d["release_time"], "2026-11-11T18:26:18Z")
        self.assertLess(d["release_age_days"], 7)

    def test_young_release_cleared_by_listed_human_passes(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, "2026-11-12T11:00:00Z")]
        d = self.young(events=ev).decide()
        self.assertEqual((d["verdict"], d["pass_kind"]), ("pass", "class"))
        self.assertEqual(d["labels_in_force"]["human-cleared"]["actor"], HUMAN)

    def test_eligible_on_day_seven(self):
        d = Scenario(state(24), state(25), now=dt.datetime(2026, 11, 18, 18, 27, tzinfo=dt.timezone.utc)).decide()
        self.assertEqual(d["verdict"], "pass")

    def test_forced(self):
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0").decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "clearable"))
        self.assertIn("forced (M4, C-20)", d["reasons"])

    def test_forced_for_another_item_does_nothing(self):
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.19.0").decide()
        self.assertEqual(d["verdict"], "pass")

    def test_forced_then_cleared(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual(d["verdict"], "pass")

    def test_c20_agent_app_label_acts_change_nothing(self):
        # The fixtures workflow, with the agent App's token: applies
        # human-cleared, applies an unrelated label, removes needs-human.
        ev = [label_event(1, "labeled", "needs-human", "github-actions[bot]", "2026-11-19T10:05:00Z"),
              label_event(2, "labeled", "human-cleared", "platform-factory-agent[bot]", AFTER_PUSH, AGENT_APP),
              label_event(3, "labeled", "unrelated", "platform-factory-agent[bot]", AFTER_PUSH, AGENT_APP),
              label_event(4, "unlabeled", "needs-human", "platform-factory-agent[bot]", AFTER_PUSH, AGENT_APP)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "clearable"))
        self.assertIsNone(d["labels_in_force"]["human-cleared"])
        self.assertEqual([u["event_id"] for u in d["unknown_label_events"]], [2])

    def test_listed_human_label_applied_through_an_app_does_not_count(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH, AGENT_APP)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual(d["verdict"], "inconclusive")

    def test_app_removal_of_the_human_label_is_an_unknown_not_a_removal(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH),
              label_event(2, "unlabeled", "human-cleared", "platform-factory-agent[bot]", LATER, AGENT_APP)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual(d["verdict"], "pass")
        self.assertEqual(len(d["unknown_label_events"]), 1)
        self.assertEqual(d["unknown_label_events"][0]["effect"], "unknown: removal not counted")

    def test_clearance_applied_before_the_head_was_pushed_does_not_count(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, BEFORE_PUSH)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual(d["verdict"], "inconclusive")

    def test_listed_human_removal_ends_clearance(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH),
              label_event(2, "unlabeled", "human-cleared", HUMAN, LATER)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev).decide()
        self.assertEqual(d["verdict"], "inconclusive")

    def test_placeholder_login_names_nobody(self):
        text = read_text(os.path.join(DATA, "classfile.yaml")).replace(
            "human_logins: [listed-human-test]", "human_logins: [LISTED-HUMAN-LOGIN]")
        ev = [label_event(1, "labeled", "human-cleared", "LISTED-HUMAN-LOGIN", AFTER_PUSH)]
        d = Scenario(state(0), state(1), force="golang.org/x/sync@v0.18.0", events=ev, class_text=text).decide()
        self.assertEqual(d["verdict"], "inconclusive")

    def test_proxy_info_unreachable_is_rerun(self):
        d = Scenario(state(0), state(1), proxy=FakeProxy(unreachable=True)).decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))
        self.assertIn("proxy-unreachable", codes(d))

    def test_recompute_network_failure_is_rerun_and_not_clearable(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        d = Scenario(state(0), state(1), recompute=TableRecompute(network_fail=True), events=ev).decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))
        self.assertIn("proxy-unreachable", codes(d))

    def test_github_failure_is_own_failure(self):
        s = Scenario(state(0), state(1))
        s.api.fail["/repos/platform-factory/svc-hello/compare/"] = 502
        d = s.decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))
        self.assertIn("own-failure", codes(d))

    def test_class_file_unreadable_is_own_failure(self):
        s = Scenario(state(0), state(1))
        s.api.fail["/repos/platform-factory/factory-controls/contents/"] = 404
        d = s.decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))

    def test_class_file_with_unpinned_image_is_own_failure(self):
        text = read_text(os.path.join(DATA, "classfile.yaml")).replace(
            "golang:1.27.1-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414",
            "golang:1.27.1-alpine")
        d = Scenario(state(0), state(1), class_text=text).decide()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))

    def test_bug_never_passes(self):
        s = Scenario(state(0), state(1))
        with mock.patch.object(class_scope, "diagnose", side_effect=RuntimeError("boom")), \
             mock.patch.object(class_scope, "module_moves", side_effect=RuntimeError("boom")):
            d = s.decide()
        self.assertEqual(d["verdict"], "inconclusive")


class TestHumanPasses(unittest.TestCase):
    def test_nonclass_without_clearance_fails(self):
        d = Scenario(state(1), None, extra_head={"README.md": b"# docs\n"}).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("nonclass-not-cleared", codes(d))

    def test_nonclass_with_clearance_passes(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        d = Scenario(state(1), None, extra_head={".github/workflows/validate.yml": b"x: 1\n"}, events=ev).decide()
        self.assertEqual((d["verdict"], d["pass_kind"]), ("pass", "human-cleared-nonclass"))

    def test_clearance_does_not_extend_to_a_class_file(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        h = state(1)
        h["go.mod"] = h["go.mod"].replace(b"\ngo 1.26.0\n", b"\ngo 1.27.0\n")
        d = Scenario(state(0), h, extra_head={"README.md": b"# x\n"}, events=ev).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("path-outside-class", codes(d))

    def revert_scenario(self, events, head_mods):
        s = Scenario(state(2), head_mods, main_parent_mods=state(1), events=events,
                     runs=[run_record(60, 1), run_record(61, 2)])
        s.svc.pulls = [pull(60, True, "a" * 40, "2026-11-18T08:00:00Z"),
                       pull(61, True, s.main, "2026-11-19T08:00:00Z")]
        return s

    def test_escape_revert_passes_a_revert_of_the_latest_class_merge(self):
        ev = [label_event(1, "labeled", "escape:revert", HUMAN, AFTER_PUSH)]
        d = self.revert_scenario(ev, state(1)).decide()
        self.assertEqual((d["verdict"], d["pass_kind"]), ("pass", "escape-revert"))
        self.assertEqual(d["revert_of"]["pr"], 61)

    def test_revert_without_label_fails(self):
        d = self.revert_scenario([], state(1)).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("downgrade", codes(d))

    def test_escape_revert_with_other_files_fails(self):
        ev = [label_event(1, "labeled", "escape:revert", HUMAN, AFTER_PUSH)]
        d = self.revert_scenario(ev, state(0)).decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertIn("revert-mismatch", codes(d))


class TestSynchronize(unittest.TestCase):
    def test_synchronize_removes_head_bound_labels_and_ignores_them(self):
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, AFTER_PUSH)]
        s = Scenario(state(0), state(1), action="synchronize", events=ev, pushed=None,
                     force="golang.org/x/sync@v0.18.0", payload_labels=["human-cleared", "escape:revert", "reviewing"])
        d = s.run()
        self.assertEqual(d["verdict"], "inconclusive")
        deletes = [p for (m, p, _) in s.api.writes if m == "DELETE"]
        self.assertEqual(deletes, ["/repos/platform-factory/svc-hello/issues/101/labels/human-cleared",
                                   "/repos/platform-factory/svc-hello/issues/101/labels/escape%3Arevert"])
        self.assertIn(("POST", "/repos/platform-factory/svc-hello/issues/101/labels", {"labels": ["needs-human"]}),
                      s.api.writes)

    def test_own_removal_ends_force_on_a_later_run(self):
        # synchronize run removed it as github-actions[bot]; a later labeled
        # run (an unrelated label) must not see it in force.
        ev = [label_event(1, "labeled", "human-cleared", HUMAN, "2026-11-19T09:30:00Z"),
              label_event(2, "unlabeled", "human-cleared", "github-actions[bot]", "2026-11-19T10:01:00Z"),
              label_event(3, "labeled", "unrelated", HUMAN, LATER)]
        d = Scenario(state(0), state(1), action="labeled", events=ev, pushed=None,
                     force="golang.org/x/sync@v0.18.0").decide()
        self.assertEqual(d["verdict"], "inconclusive")
        self.assertEqual(d["unknown_label_events"], [])

    def test_failed_removal_is_rerun(self):
        s = Scenario(state(0), state(1), action="synchronize", payload_labels=["human-cleared"])
        s.api.fail["/repos/platform-factory/svc-hello/issues/101/labels/"] = 403
        d = s.run()
        self.assertEqual((d["verdict"], d["reason_class"]), ("inconclusive", "rerun"))


class TestLabelsOnRun(unittest.TestCase):
    def test_needs_human_only_on_inconclusive(self):
        for kw, expect in (({}, False), ({"force": "golang.org/x/sync@v0.18.0"}, True)):
            s = Scenario(state(0), state(1), **kw)
            s.run()
            posted = any(m == "POST" for m, _, _ in s.api.writes)
            self.assertEqual(posted, expect, kw)
        s = Scenario(state(0), state(1), extra_head={"README.md": b"x"})
        self.assertEqual(s.run()["verdict"], "fail")
        self.assertEqual(s.api.writes, [])


class TestNearMiss(unittest.TestCase):
    def table(self):
        t = TableRecompute.sweep_table()
        # from the baseline, item 2 alone (state 2 differs from state 1 only
        # by the x/sync lines, so it is what go get makes from the baseline)
        t[(state(0)["go.mod"], "golang.org/x/sync@v0.19.0")] = state(2)
        return t

    def test_near_miss_closes_its_item(self):
        s = Scenario(state(0), state(2), runs=[run_record(50, 1)], pulls=[pull(50, False)],
                     recompute=TableRecompute(self.table()))
        d = s.decide()
        self.assertEqual(d["verdict"], "pass", d["reasons"])
        self.assertEqual(d["queue_position"], 2)
        self.assertEqual(d["near_misses"], {"1": 50})

    def test_the_closed_item_itself_then_fails(self):
        s = Scenario(state(0), state(1), runs=[run_record(50, 1)], pulls=[pull(50, False)],
                     recompute=TableRecompute(self.table()))
        self.assertEqual(s.decide()["verdict"], "fail")

    def test_a_closed_seed_is_not_a_near_miss(self):
        s = Scenario(state(0), state(1), runs=[run_record(50, 1)], pulls=[pull(50, False)], seeds=[50])
        d = s.decide()
        self.assertEqual((d["verdict"], d["queue_position"]), ("pass", 1))

    def test_reopened_queue_item_is_still_a_near_miss(self):
        ev = [{"id": 9, "event": "closed", "created_at": "2026-11-19T09:10:00Z",
               "actor": {"login": "platform-factory-agent[bot]"}, "performed_via_github_app": AGENT_APP},
              {"id": 10, "event": "reopened", "created_at": "2026-11-19T09:20:00Z",
               "actor": {"login": "platform-factory-agent[bot]"}, "performed_via_github_app": AGENT_APP}]
        s = Scenario(state(0), state(1), pr=101, runs=[run_record(101, 1)], events=ev,
                     recompute=TableRecompute(self.table()))
        d = s.decide()
        self.assertEqual(d["verdict"], "fail")
        self.assertEqual(d["near_misses"], {"1": 101})

    def test_merged_queue_item_is_not_a_near_miss_and_names_the_opener(self):
        s = Scenario(state(1), state(2), pr=102, runs=[run_record(101, 1), run_record(102, 2)],
                     pulls=[pull(101, True, "b" * 40)])
        d = s.decide()
        self.assertEqual(d["verdict"], "pass")
        self.assertEqual(d["opened_by"]["controls_run_id"], 7102)

    def test_reverted_item_is_queued_again(self):
        # main back at state 1 after a revert of item 2: next is item 2 again
        s = Scenario(state(1), state(2), runs=[run_record(101, 1), run_record(102, 2)],
                     pulls=[pull(101, True, "b" * 40), pull(102, True, "c" * 40)])
        d = s.decide()
        self.assertEqual((d["verdict"], d["queue_position"]), ("pass", 2))


class TestUnits(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(class_scope.is_pseudo("v0.19.1-0.20260211230050-1a583072c11b"))
        self.assertTrue(class_scope.is_pseudo("v0.0.0-20240606120523-5a60cdf6a761"))
        self.assertFalse(class_scope.is_pseudo("v0.18.0"))
        self.assertFalse(class_scope.is_release("v1.0.0-rc.1"))
        self.assertTrue(class_scope.version_lt("v0.9.0", "v0.10.0"))
        self.assertTrue(class_scope.version_lt("v1.0.0-rc.1", "v1.0.0"))

    def test_parse_gomod_fails_closed(self):
        gm = class_scope.parse_gomod("module m\n\ngo 1.26.0\n\ntoolchain go1.27.1\nfrobnicate x\n"
                                     "replace (\n\ta => b v1.0.0\n)\nrequire \"q/r\" v1.0.0 // indirect; tagx:ignore\n")
        verbs = [v for v, _ in gm.directives]
        self.assertEqual(verbs, ["toolchain", "unknown", "replace"])
        self.assertTrue(gm.requires["q/r"].indirect)

    def test_class_settings_accepts_string_items(self):
        cf = {"queue": ["golang.org/x/sync@v0.18.0"], "min_release_age_days": 7, "human_logins": ["Someone"]}
        s = class_scope.class_settings(cf)
        self.assertEqual(s["queue"][0], {"position": 1, "module": "golang.org/x/sync", "version": "v0.18.0"})
        self.assertEqual(s["human_logins"], {"someone"})
        self.assertEqual(s["builder_image"], class_scope.DEFAULT_BUILDER_IMAGE)

    def test_class_settings_refuses_a_prerelease_in_the_queue(self):
        with self.assertRaises(class_scope.OwnFailure):
            class_scope.class_settings({"queue": ["a/b@v1.0.0-rc.1"], "min_release_age_days": 7, "human_logins": []})

    def test_logins_compare_case_insensitively(self):
        ev = [label_event(1, "labeled", "human-cleared", "Listed-Human-Test", AFTER_PUSH)]
        state_, _ = class_scope.labels_in_force(ev, {"listed-human-test"}, None)
        self.assertIsNotNone(state_["human-cleared"])

    def test_unlisted_person_label_is_ignored(self):
        ev = [label_event(1, "labeled", "escape:revert", "someone-else", AFTER_PUSH)]
        state_, unknown = class_scope.labels_in_force(ev, {HUMAN}, None)
        self.assertIsNone(state_["escape:revert"])
        self.assertEqual(unknown[0]["effect"], "ignored: not a listed human, or made through an App")

    def test_packet_has_the_sections(self):
        d = Scenario(state(23), state(24)).decide()
        p = class_scope.render_packet(d, SVC)
        for needle in ("## class-scope: PASS", "Queue position 24", "Forced modules: `github.com/jackc/puddle/v2`",
                       "go.sum lines changed: 4 removed, 4 added", "days old",
                       "https://github.com/jackc/pgx/compare/v5.11.0...v5.12.0", "govulncheck",
                       "Class file:", "Verifier:", "### Agent summary (unverified)"):
            self.assertIn(needle, p)


class TestCli(unittest.TestCase):
    def test_main_writes_verdict_summary_and_outputs(self):
        s = Scenario(state(0), state(1), force="")
        payload = {"action": "opened", "pull_request": {
            "number": 101, "html_url": "", "labels": [{"name": "reviewing"}],
            "head": {"sha": s.head, "ref": "agent/branch", "repo": {"full_name": SVC}},
            "base": {"ref": "main"}}}
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "event.json")
            with open(ev, "w") as f:
                json.dump(payload, f)
            out, summary = os.path.join(tmp, "gh_output"), os.path.join(tmp, "gh_summary")
            env = {"GITHUB_OUTPUT": out, "GITHUB_STEP_SUMMARY": summary, "M4_FORCE_INCONCLUSIVE": "",
                   "GITHUB_SHA": "f" * 40, "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(class_scope, "GitHub", lambda *a, **k: s.api), \
                 mock.patch.object(class_scope, "Proxy", lambda *a, **k: FakeProxy()), \
                 mock.patch.object(class_scope, "DockerRecompute", lambda *a, **k: s.recompute), \
                 mock.patch.object(class_scope, "utcnow", lambda: NOW), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = class_scope.main(["--event", ev, "--repo", SVC, "--out-dir", os.path.join(tmp, "o")])
            self.assertEqual(rc, 0)
            v = json.loads(read_text(os.path.join(tmp, "o", "verdict.json")))
            self.assertEqual(v["verdict"], "pass")
            self.assertEqual(v["schema"], 1)
            for k in ("verdict", "reason_class", "reasons", "pass_kind", "head_sha", "main_sha", "pr",
                      "queue_position", "module", "old", "new", "forced", "release_time", "release_age_days",
                      "class_file_commit", "verifier_version", "labels_in_force", "unknown_label_events"):
                self.assertIn(k, v)
            self.assertIn("verdict=pass\n", read_text(out))
            self.assertIn("Agent summary (unverified)", read_text(summary))


if __name__ == "__main__":
    unittest.main()
