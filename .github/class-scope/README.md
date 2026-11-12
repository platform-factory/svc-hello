# class-scope

`class-scope` is a required check that decides whether a pull request into
`main` is one on-class Go-module bump. It is the second of the two locks that
hold the change class of ADR-0024; the ruleset's two required-reviewer entries
are the first. This directory holds the verifier (`class_scope.py`), its
offline tests, and this README. The workflow that runs it is
`.github/workflows/class-scope.yml`.

## Why it is built this way

The class is "a Go-module bump: `go.mod` and `go.sum` at the root, one queue
item, other modules moved only as far as the target forces" (ADR-0024 §1). A
parser of the diff would have to know what each module forces and every way a
`go.mod` can be bent. So the check recomputes the change instead: it makes the
correct pull request itself and requires the real one to equal it byte for
byte. That one comparison catches a downgrade seen only as a second changed
line, an unforced second module, a `// indirect` flip, appended rather than
replaced `go.sum` lines, a tampered hash, a target other than the queue's, a
pseudo-version and a `replace` line, with no list of special cases.

It is author-blind. Every pull request into `main` must be the queue's next
item, or carry a listed human's clearance, whoever opened it. Who opened a
pull request is never read, because an App with `contents` write can push to
any branch.

It runs on `pull_request_target`, so the workflow file and the verifier come
from the default branch and a pull request cannot edit the check that judges
it. That event is safe only if the job never checks out or runs the pull
request's code. This job does not: it reads the pull request's two files at
the event's head commit through the API, as bytes.

## What one run does

Every trigger (`opened`, `synchronize`, `reopened`, `labeled`, `unlabeled`)
recomputes the whole verdict. There is no job-level `if:` and no path filter,
because a skipped job reports success.

1. **Inputs.** Read the class file `classes/go-module-bump.yaml` from
   `platform-factory/factory-controls` at `main`'s tip, and record that commit.
   Read the tip of `svc-hello`'s `main` at run time; the event's base sha is not
   used. Read the pull request's issue events, the push time of its head commit
   (repository activity), and the controls repo's `records` branch.
2. **Next item.** From main's `go.mod`, the next item is the first queue item
   whose version is above main's version of that module and that no near-miss
   closed (readiness note, "The frozen numbers"). A near-miss is a queue-item
   pull request, other than a seed, closed unmerged. This check reads it from
   GitHub's state: the pull request numbers in the run records
   (`records:runs/*.json`, written only by the agent's `open` job), minus the
   seeds (`records:seeds/<n>.json`, written when a seed is closed), that are
   closed and not merged now, or that are open again after a `closed` event
   (a close is never a free retry). A reverted item is queued again because
   main's version drops below it. If the `records` branch does not exist yet,
   no queue-item pull request has been opened and there is no near-miss.
3. **Shape.** Compare `main`'s tip with the head (`GET /compare/{main}...{head}`).
   With strict status checks the head must contain main's tip; a head that is
   behind or has diverged fails. Any changed path other than root `go.mod` and
   `go.sum` fails (a nested `x/go.mod` included), as does a class file that is
   added, removed, renamed, not a regular file, or changed in mode only.
4. **Person's passes.** Two passes exist for pull requests a person opens:
   - `human-cleared` in force and neither class file changed: pass
     (`human-cleared-nonclass`). The wall entry still needs the listed human's
     approval.
   - `escape:revert` in force and the head's `go.mod` and `go.sum` equal those
     at the parent of the latest class merge: pass (`escape-revert`). The
     latest class merge is the merged queue-item pull request (from the run
     records) with the latest `merged_at`.
5. **Recompute.** Copy main's `go.mod`, `go.sum`, every `.go` file (test files
   too, because `go mod tidy` counts test imports) and any nested `go.mod`
   into a scratch directory. In the builder image named by the class file's
   `agent.builder_image` (pinned by digest; the Dockerfile's
   `golang:1.27.1-alpine` by default), with `GOTOOLCHAIN=local`,
   `GOPROXY=https://proxy.golang.org`, `GOSUMDB=sum.golang.org` and
   `GOFLAGS` empty, run `go get <module>@<version>` and then `go mod tidy`.
   `go get` downloads module sources and checks them against the sum
   database; it does not run them. The recompute is tried three times.
6. **Compare.** The head's two files must equal the result byte for byte.
   Otherwise the verdict is fail, with reasons that say why in words
   (`diagnose`): the verdict is the comparison; the reasons explain it.
7. **May it pass now?** A matching change is still inconclusive, clearable, if
   the target release is younger than `min_release_age_days` by the proxy's
   `.info` time, or if `M4_FORCE_INCONCLUSIVE` names this item (reason
   `forced (M4, C-20)`). A listed human's `human-cleared` for this head turns
   that into a pass. A clearance never passes a change that did not match.

## Outcomes

| Verdict | `reason_class` | When |
|---|---|---|
| `pass` | null | `pass_kind` is `class`, `human-cleared-nonclass` or `escape-revert` |
| `fail` | null | off-class, with reasons |
| `inconclusive` | `clearable` | release younger than the minimum age; forced for C-20 |
| `inconclusive` | `rerun` | proxy or sum database unreachable after retries; this check's own failure (GitHub unreadable, class file unreadable or invalid, the recompute failing for another reason, a failed label removal on `synchronize`) |

An inconclusive is a failing check plus the `needs-human` label, never
`neutral` or `skipped`. A re-run settles the re-run class; a person settles the
clearable class by applying `human-cleared`, whose label event re-runs the
check. Any bug in the verifier is caught and reported as an inconclusive of
the re-run class; no code path turns an exception into a pass.

The verdict is written three ways, all by the workflow's own token, none of
which the agent App can write:

1. **Step names.** `decide` runs the verifier and exits 0 whenever it reached a
   verdict. Then `upload verdict`, then `verdict-inconclusive` (fails iff
   inconclusive) and `verdict-fail` (fails iff fail). A failure in any other
   step means no verdict, which readers treat as an inconclusive to re-run.
   Readers find the failed step through
   `GET /repos/{o}/{r}/actions/runs/{id}/jobs`.
2. **Artifact** `class-scope-verdict`, holding `verdict.json` (schema 1). The
   fields the build contract fixes: `schema`, `verdict`, `reason_class`,
   `reasons`, `pass_kind`, `head_sha`, `main_sha`, `pr`, `queue_position`,
   `module`, `old`, `new`, `forced`, `release_time`, `release_age_days`,
   `class_file_commit`, `verifier_version`, `labels_in_force`,
   `unknown_label_events`. Added for readers: `reason_codes` (one stable code
   per reason, same order), `event_action`, `builder_image`,
   `head_pushed_at`, `records_commit`, `near_misses`, `changed_files`,
   `gomod_change`, `gosum_change`, `expected_sha256`, `head_sha256`,
   `opened_by` (the run record's controls run and job), `recompute`,
   `revert_of`, `label_writes`, `decided_at`, `run`.
3. **Job summary**, which is the packet (ADR-0024 §12): queue position and
   item, target old → new, forced modules, the `go.mod` change in words, the
   `go.sum` lines changed, release age, upstream links, where `go-test`'s
   govulncheck findings are, the verdict with its reasons, the class file's
   commit, the verifier's version and image, the controls run and job that
   opened the pull request, labels in force and unknown label events. The
   agent's prose is linked under "Agent summary (unverified)" and never
   copied.

Reason codes: `head-behind-main`, `base-not-main`, `path-outside-class`,
`nonclass-not-cleared`, `class-file-status`, `class-file-mode`,
`class-file-missing`, `queue-exhausted`, `gomod-directive`, `go-directive`,
`module-path`, `module-added`, `module-removed`, `wrong-target`,
`target-version`, `unforced-module`, `forced-version`, `forced-missing`,
`downgrade`, `pseudo-version`, `pre-release`, `major-version`, `bad-version`,
`indirect-flip`, `require-block`, `gomod-duplicate`, `gomod-format`,
`gosum-hash`, `gosum-extra`, `gosum-missing`, `gosum-format`, `line-endings`,
`encoding`, `revert-mismatch`, `recompute-mismatch` (always present on a
recompute fail), `release-young`, `forced`, `cleared`, `proxy-unreachable`,
`recompute-failed`, `own-failure`.

## Labels

A person's label (`reviewing`, `human-cleared`, `escape:revert`) is in force
from a listed human login's `labeled` event that carries no
`performed_via_github_app`, until a listed human login's `unlabeled` event.
The actor is read from the pull request's issue events, never from the label,
because the agent App can apply labels. A label or removal by anyone else
changes nothing and is listed in `unknown_label_events`; in particular an
App's removal of a listed human's label is an unknown, not a removal.

`human-cleared` and `escape:revert` are also bound to the head commit:

- they count only when applied after the head commit was pushed, by the
  repository activity record (`GET /repos/{o}/{r}/activity`, GitHub's own
  push log). When that record has no entry for the head (a fork's branch),
  this guard is skipped and the next one carries it;
- on `synchronize` this check removes both labels and treats them as not in
  force. Its own removal is recorded as `github-actions[bot]`, a login no App
  can take, and ends the label's force like a listed human's removal. (The
  readiness note names only listed-human removals; without this reading the
  synchronize removal would have no effect on a later run.) If the removal
  fails, the run is an inconclusive to re-run.

`needs-human` is applied on every inconclusive and never removed here: the
ratchet App applies it too, on a demotion.

The listed logins are the class file's `human_logins`, compared without case.
The placeholder `LISTED-HUMAN-LOGIN` names nobody.

## Token permissions

```yaml
permissions:
  contents: read
  pull-requests: write
```

- `contents: read`: the checkout of this directory, and the reads of
  `svc-hello`'s refs, commits, trees, blobs, compare and activity.
- `pull-requests: write`: adding `needs-human` and removing `human-cleared` and
  `escape:revert` (`POST` / `DELETE /issues/{n}/labels`). GitHub's App
  permission data lists these label endpoints, and the issue-events read,
  under `pull_requests` as well as `issues`; `pull-requests` is the narrower
  choice for a pull request. Its write includes the read of the issue events.

Nothing else: no `issues`, `checks`, `statuses`, `actions` or `id-token`. The
artifact upload uses the runner's own artifact token, not `GITHUB_TOKEN`
scopes. The controls repository and the module proxy are public. No secret is
read; `svc-hello` holds none. The checkout keeps no credential
(`persist-credentials: false`), and the token reaches only the `decide` step.

## Inputs the verifier reads

- Class file keys: `queue` (list of `{module, version}` or
  `module@version`, in order; release tags only), `min_release_age_days`,
  `human_logins`, `paths` (if present, must be `go.mod` and `go.sum`),
  `agent.builder_image` (if present, must be pinned by `@sha256:`). JSON is
  read directly; YAML through PyYAML or `yq`, both on GitHub's runners. The
  controls repository's `factory/classfile.py` validates the whole file.
- Records: `runs/*.json` (`pr_number`, `queue_position`, and the controls run
  fields for the packet) and the names of `seeds/*.json`.
- Environment: `GITHUB_TOKEN`, `M4_FORCE_INCONCLUSIVE`, `GITHUB_EVENT_PATH`
  (through `--event`), and optionally `CLASS_SCOPE_GOPROXY`,
  `CLASS_SCOPE_WORKDIR`.

## Running the tests

Offline, from this directory:

```sh
python3 -m unittest discover -s tests
```

- `test_core.py`: the decision against a fake GitHub (`tests/fakes.py`), with
  the recompute answered from the recorded end states.
- `test_recorded_shapes.py`: the readers against responses recorded from the
  public API (`tests/data/recorded/`, reduced to the fields read, logins
  replaced).
- `test_docker.py`: the real recompute in the pinned image, with the module
  proxy reached from the container and downloads cached in one Docker
  volume. It re-derives the 25 recorded queue states, passes all 25 items in
  order through `decide`, fails each of C-15's off-meaning cases made with
  real `go` commands, and shows a young release as clearable and an
  unreachable proxy or sum database as re-run. It is skipped, visibly, when
  `docker` is not on the PATH. When the Docker daemon runs in a VM that does
  not share the system temporary directory, set `CLASS_SCOPE_WORKDIR` to a
  directory it does share.

`tests/sweep.py --write` re-records `tests/data/sweep/` (the `go 1.26.0`
baseline and the 25 end states) and `--check` compares. `tests/data/proxy-info/`
holds the module proxy's `.info` for the 25 items, read on 2026-11-11.

## Limits, stated

- Which commit a `pull_request_target` check run attaches to, whether a red
  one blocks a merge, and whether a later green run on the same head replaces
  it are probe P2's questions; nothing here settles them.
- That the repository activity record has the head's push when the label
  event's run reads it is not tried; when it does not, the synchronize removal
  is the guard.
- That the job token reads another public repository through the REST API is
  inferred from `validate.yml`'s `claims` job, which checks out other public
  repositories with it.
- A fork's head commit is read through the base repository's API, which
  GitHub serves for pull request heads; not tried, and M4 has no fork pull
  requests.
- A seed reads as a near-miss between its close and its seed record being
  written; the opening job writes the record before it opens the next item.
- Any change to what this check decides, after the L1 switch-on, is listed in
  M4's entry and resets the streak (readiness note).
