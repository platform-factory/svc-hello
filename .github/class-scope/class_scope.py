#!/usr/bin/env python3
"""class-scope: decide whether a pull request into main is one on-class Go-module bump.

Why this exists (ADR-0024 §1, §5, §6): the Go-module bump class on svc-hello is
held by two locks. The ruleset is one; this check is the other. It decides
class membership by recomputing the change, never by parsing the pull
request's diff and never by trusting who opened it:

  1. Read the tip of main when the check runs (not the event's base sha) and
     take its go.mod, go.sum and every .go file.
  2. Take the queue's next item from the class file in the controls
     repository: the first queue item whose version is above main's and that
     no near-miss closed.
  3. In the digest-pinned builder image, with GOTOOLCHAIN=local, run
     `go get <module>@<version>` and `go mod tidy`.
  4. Require the pull request's go.mod and go.sum, read at the event's head
     commit through the API as data, to equal the result byte for byte, and
     require the pull request to change no other path.

The pull request's code is never checked out or run. The workflow runs this
file from the default branch (pull_request_target), with the repository's own
token, which the agent App cannot use.

There are three outcomes. `pass`; `fail` (off-class, with reasons); and
`inconclusive`, which blocks like a fail but is labelled `needs-human`. An
inconclusive is either *clearable* (the target release is younger than the
class file's minimum age, or the outcome was forced through
M4_FORCE_INCONCLUSIVE for C-20) or *re-run* (the module proxy or the sum
database could not be reached after retries, or this check failed in itself).
A listed human's `human-cleared` label turns a clearable inconclusive into a
pass, but only when the recompute itself matched.

The core (`decide`) takes its GitHub reads through an injected API object with
the same shape as the controls repository's `factory/gh.py` client
(get / paginate / post / delete), plus an injected module-proxy reader and an
injected recompute function, so it can be tested without GitHub, the network
or Docker. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

VERIFIER_VERSION = "1.0.0"
SCHEMA = 1

CLASS_FILES = ("go.mod", "go.sum")
CONTROLS_REPO = "platform-factory/factory-controls"
CLASS_FILE_PATH = "classes/go-module-bump.yaml"
RECORDS_BRANCH = "records"

# The Dockerfile's builder image, pinned by the digest of its multi-platform
# index (read with `docker buildx imagetools inspect` on 2026-11-11; the
# alpine3.24 tag go-test, the Dockerfile and the class file name resolves to
# the same digest). Used only when the class file does not name one; when it
# does, the class file's pin wins, so the agent's job and this check
# recompute in the same image.
DEFAULT_BUILDER_IMAGE = (
    "golang:1.27.1-alpine3.24"
    "@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414"
)
DEFAULT_GOPROXY = "https://proxy.golang.org"

LABEL_HUMAN_CLEARED = "human-cleared"
LABEL_ESCAPE_REVERT = "escape:revert"
LABEL_REVIEWING = "reviewing"
LABEL_NEEDS_HUMAN = "needs-human"
# Labels a person applies. Only these are read for clearance; App acts on
# them are reported as unknowns.
HUMAN_LABELS = (LABEL_REVIEWING, LABEL_HUMAN_CLEARED, LABEL_ESCAPE_REVERT)
# Human labels that are in force only for the head commit they were applied
# to, and that this check removes on `synchronize`.
HEAD_BOUND_LABELS = (LABEL_HUMAN_CLEARED, LABEL_ESCAPE_REVERT)
# The login GitHub records for actions taken with a workflow's GITHUB_TOKEN.
# No App can take this login. A removal by it is this check's own removal on
# `synchronize`, which ends a label's force like a listed human's removal.
ACTIONS_BOT_LOGIN = "github-actions[bot]"
PLACEHOLDER_LOGIN = "LISTED-HUMAN-LOGIN"

FORCED_REASON = "forced (M4, C-20)"

# The decide step writes the verdict's fixed fields as one check-run
# annotation with this title. Annotations are readable without a token,
# unlike the verdict artifact (whose download needs `actions: read` on this
# repository), so the controls repo's scorecard reads this.
VERDICT_ANNOTATION_TITLE = "class-scope-verdict"
ANNOTATION_FIELDS = ("schema", "verdict", "reason_class", "reason_codes", "pass_kind", "verifier_version",
                     "pr", "head_sha", "main_sha", "queue_position", "module", "new", "class_file_commit",
                     "decided_at", "tampers")
# The comment that links to the packet (ADR-0024 §12: it links to the job
# summary and is not the packet). One per pull request, found by this marker
# on its first line and the author github-actions[bot].
PACKET_COMMENT_MARKER = "<!-- class-scope:packet-link -->"

SEMVER_RE = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
# Go pseudo-versions: vX.0.0-yyyymmddhhmmss-abcdefabcdef,
# vX.Y.Z-pre.0.yyyymmddhhmmss-abcdef..., vX.Y.(Z+1)-0.yyyymmddhhmmss-abcdef...
PSEUDO_RE = re.compile(r"(^|[-.])(0\.)?\d{14}-[0-9a-f]{12}$")
MODULE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]*$")


# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """A non-2xx answer from GitHub (or a fake). Same shape as factory/gh.py."""

    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"GitHub API {status} on {path}: {str(body)[:300]}")
        self.status = status
        self.body = body
        self.path = path


class ProxyUnreachable(Exception):
    """The module proxy did not answer after retries."""


class OwnFailure(Exception):
    """This check could not do its own work. Inconclusive, re-run class."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s: str) -> dt.datetime:
    # GitHub and the proxy both use RFC 3339 with a Z suffix.
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def is_semver(v: str) -> bool:
    return bool(SEMVER_RE.match(v))


def is_pseudo(v: str) -> bool:
    m = SEMVER_RE.match(v)
    return bool(m and m.group(4) and PSEUDO_RE.search(m.group(4)))


def is_release(v: str) -> bool:
    """A release tag: semver with no pre-release and no build suffix."""
    m = SEMVER_RE.match(v)
    return bool(m and m.group(4) is None and m.group(5) is None)


def _prerelease_key(pre: str | None):
    # Semver 2.0 precedence: a version without pre-release ranks higher.
    if pre is None:
        return (1,)
    parts = []
    for p in pre.split("."):
        parts.append((0, int(p), "") if p.isdigit() else (1, 0, p))
    return (0, tuple(parts))


def version_key(v: str):
    m = SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"not a semantic version: {v!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), _prerelease_key(m.group(4)))


def version_lt(a: str, b: str) -> bool:
    return version_key(a) < version_key(b)


def major_of(v: str) -> int:
    return version_key(v)[0]


# ---------------------------------------------------------------------------
# go.mod and go.sum as data (never executed)
# ---------------------------------------------------------------------------


@dataclass
class Requirement:
    path: str
    version: str
    indirect: bool
    block: bool  # inside a `require ( ... )` block


@dataclass
class GoMod:
    module: str | None = None
    go: str | None = None
    requires: dict[str, Requirement] = field(default_factory=dict)
    # Directives the class forbids or this parser does not know, with their
    # text: replace, exclude, retract, toolchain, godebug, tool, ignore, other.
    directives: list[tuple[str, str]] = field(default_factory=list)
    duplicate_requires: list[str] = field(default_factory=list)


def _strip_comment(line: str) -> tuple[str, str]:
    i = line.find("//")
    if i < 0:
        return line.strip(), ""
    return line[:i].strip(), line[i + 2 :].strip()


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"`":
        return tok[1:-1]
    return tok


def parse_gomod(text: str) -> GoMod:
    """Parse go.mod lines into a GoMod. Fails closed: a line it does not know
    becomes a directive entry, which the class treats as off-class."""
    gm = GoMod()
    block: str | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        code, comment = _strip_comment(raw)
        if not code:
            continue
        if block is not None:
            if code == ")":
                block = None
                continue
            verb, rest = block, code
        else:
            parts = code.split(None, 1)
            verb = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
            if rest == "(":
                block = verb
                continue
        if verb == "module":
            gm.module = _unquote(rest)
        elif verb == "go":
            gm.go = rest
        elif verb == "require":
            toks = rest.split()
            if len(toks) != 2:
                gm.directives.append(("require-malformed", raw.strip()))
                continue
            path, version = _unquote(toks[0]), toks[1]
            indirect = comment == "indirect" or comment.startswith("indirect;")
            if path in gm.requires:
                gm.duplicate_requires.append(path)
            gm.requires[path] = Requirement(path, version, indirect, block == "require")
        elif verb in ("replace", "exclude", "retract", "toolchain", "godebug", "tool", "ignore"):
            gm.directives.append((verb, raw.strip()))
        else:
            gm.directives.append(("unknown", raw.strip()))
    if block is not None:
        gm.directives.append(("unterminated-block", block))
    return gm


def parse_gosum(text: str) -> dict[tuple[str, str], str]:
    """go.sum as {(module, version-or-version/go.mod): hash}. Malformed lines
    are kept under the key ("<malformed>", line)."""
    out: dict[tuple[str, str], str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        toks = line.split()
        if len(toks) != 3:
            out[("<malformed>", line)] = ""
            continue
        out[(toks[0], toks[1])] = toks[2]
    return out


def gomod_versions(gm: GoMod) -> dict[str, str]:
    return {p: r.version for p, r in gm.requires.items()}


# ---------------------------------------------------------------------------
# Class file
# ---------------------------------------------------------------------------


def load_class_text(text: str) -> dict:
    """The class file is YAML. JSON is valid YAML and is read without any
    library; otherwise PyYAML when installed, then `yq -o=json` (both are on
    GitHub's ubuntu runners). No `pip install` at run time."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except ImportError:
            yq = shutil.which("yq")
            if not yq:
                raise OwnFailure("class file is YAML and neither PyYAML nor yq is available")
            proc = subprocess.run([yq, "-o=json", "."], input=text.encode(), capture_output=True)
            if proc.returncode != 0:
                raise OwnFailure(f"yq could not parse the class file: {proc.stderr.decode()[:300]}")
            data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise OwnFailure("class file is not a mapping")
    return data


def _builder_image_from(cf: dict) -> str | None:
    agent = cf.get("agent")
    if not isinstance(agent, dict) or "builder_image" not in agent:
        return None
    bi = agent["builder_image"]
    if isinstance(bi, str):
        ref = bi
    elif isinstance(bi, dict):
        name = bi.get("image") or bi.get("name") or bi.get("ref")
        digest = bi.get("digest")
        if not isinstance(name, str):
            raise OwnFailure("class file agent.builder_image has no image name")
        ref = name if "@sha256:" in name or not digest else f"{name}@{digest}"
    else:
        raise OwnFailure("class file agent.builder_image is neither a string nor a mapping")
    if not re.search(r"@sha256:[0-9a-f]{64}$", ref):
        raise OwnFailure(f"class file builder image is not pinned by digest: {ref!r}")
    return ref


def class_settings(cf: dict) -> dict:
    """The few class-file values this check reads, validated. The controls
    repository's factory/classfile.py validates the whole file; this check
    validates what it relies on and fails closed on the rest."""
    queue_raw = cf.get("queue")
    if not isinstance(queue_raw, list) or not queue_raw:
        raise OwnFailure("class file has no queue")
    queue = []
    for i, item in enumerate(queue_raw, start=1):
        if isinstance(item, str) and "@" in item:
            module, version = item.rsplit("@", 1)
            pos = i
        elif isinstance(item, dict) and "module" in item and "version" in item:
            module, version = str(item["module"]), str(item["version"])
            pos = int(item.get("position", i))
        else:
            raise OwnFailure(f"class file queue item {i} is not module@version")
        if pos != i:
            raise OwnFailure(f"class file queue item {i} carries position {pos}")
        if not MODULE_PATH_RE.match(module) or not is_release(version):
            raise OwnFailure(f"class file queue item {i} is not a module path at a release tag: {module}@{version}")
        queue.append({"position": i, "module": module, "version": version})
    try:
        min_age = float(cf["min_release_age_days"])
    except (KeyError, TypeError, ValueError):
        raise OwnFailure("class file has no numeric min_release_age_days")
    logins = cf.get("human_logins")
    if not isinstance(logins, list) or not all(isinstance(x, str) for x in logins):
        raise OwnFailure("class file human_logins is not a list of logins")
    # The placeholder stands until the owner's step fills it; it names nobody.
    humans = {x.lower() for x in logins if x and x != PLACEHOLDER_LOGIN}
    paths = cf.get("paths")
    if paths is not None:
        norm = sorted(str(p).lstrip("/") for p in paths)
        if norm != sorted(CLASS_FILES):
            raise OwnFailure(f"class file paths {paths!r} are not go.mod and go.sum")
    return {
        "queue": queue,
        "min_release_age_days": min_age,
        "human_logins": humans,
        "builder_image": _builder_image_from(cf) or DEFAULT_BUILDER_IMAGE,
    }


# ---------------------------------------------------------------------------
# GitHub REST client (stdlib). Same shape as the controls repo's factory/gh.py.
# ---------------------------------------------------------------------------


class GitHub:
    def __init__(self, token: str | None, api: str = "https://api.github.com",
                 retries: int = 4, backoff: float = 2.0, timeout: float = 30.0):
        self.token = token
        self.api = api.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: Any = None) -> tuple[Any, dict]:
        url = path if path.startswith("http") else self.api + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "class-scope",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return (json.loads(raw) if raw else None), dict(resp.headers)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = raw.decode(errors="replace")
                # Retry only what a retry can fix: server errors and rate limits.
                if e.code >= 500 or e.code == 429 or (e.code == 403 and "rate limit" in str(payload).lower()):
                    last = ApiError(e.code, payload, path)
                else:
                    raise ApiError(e.code, payload, path) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = ApiError(0, str(e), path)
            time.sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    def get(self, path: str, **params) -> Any:
        return self._request("GET", path, params or None)[0]

    def paginate(self, path: str, **params) -> list:
        params.setdefault("per_page", 100)
        out: list = []
        url: str | None = path
        first = True
        while url:
            data, headers = self._request("GET", url, params if first else None)
            first = False
            if isinstance(data, dict):  # a few list endpoints wrap their items
                data = next((v for v in data.values() if isinstance(v, list)), [])
            out.extend(data or [])
            url = None
            for part in headers.get("Link", headers.get("link", "")).split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1 : part.find(">")]
        return out

    def post(self, path: str, body: Any) -> Any:
        return self._request("POST", path, None, body)[0]

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)[0]

    def patch(self, path: str, body: Any) -> Any:
        return self._request("PATCH", path, None, body)[0]


# ---------------------------------------------------------------------------
# Module proxy (.info) reader
# ---------------------------------------------------------------------------


def escape_module_path(path: str) -> str:
    # The proxy protocol writes an upper-case letter as "!" + its lower case.
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


class Proxy:
    def __init__(self, base: str = DEFAULT_GOPROXY, retries: int = 4, backoff: float = 2.0,
                 timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def info(self, module: str, version: str) -> dict:
        url = f"{self.base}/{escape_module_path(module)}/@v/{version}.info"
        last = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    # The proxy answered and does not know the version: that is
                    # not a network failure, but this check cannot go on.
                    raise OwnFailure(f"module proxy answered {e.code} for {module}@{version}")
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
                last = e
            time.sleep(self.backoff * (2 ** attempt))
        raise ProxyUnreachable(f"{url}: {last}")


# ---------------------------------------------------------------------------
# Recompute in the pinned builder image
# ---------------------------------------------------------------------------


@dataclass
class RecomputeResult:
    ok: bool
    go_mod: bytes = b""
    go_sum: bytes = b""
    network: bool = False  # failure looked like the proxy or sum database
    error: str = ""
    attempts: int = 0
    log: str = ""


NETWORK_PATTERNS = (
    "dial tcp", "i/o timeout", "connection refused", "no such host", "tls handshake",
    "server misbehaving", "connection reset", "network is unreachable", "proxyconnect",
    "context deadline exceeded", "unexpected eof", "502 bad gateway", "503 service unavailable",
    "504 gateway timeout", "temporary failure in name resolution", "lookup ",
    "reading https://", "verifying module", "sum.golang.org",
)


def looks_like_network(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in NETWORK_PATTERNS)


def work_base() -> str | None:
    """Where recompute copies live. The Docker daemon must be able to mount
    it: on a GitHub runner that is RUNNER_TEMP; on a laptop whose daemon runs
    in a VM, set CLASS_SCOPE_WORKDIR to a shared directory."""
    return os.environ.get("CLASS_SCOPE_WORKDIR") or os.environ.get("RUNNER_TEMP") or None


class DockerRecompute:
    """`go get <module>@<version>` then `go mod tidy` on a copy of main's
    module files and .go sources, in the pinned image, GOTOOLCHAIN=local.

    The module cache lives in a named Docker volume, so repeated recomputes
    in one job (and between local test cases) download each module once.
    `go get` downloads sources and checks them against the sum database; it
    does not run them."""

    # Each attempt is bounded, and so are all of them together: the job's
    # timeout is 30 minutes, and a run that times out writes no verdict, adds
    # no `needs-human` and uploads nothing. With 240 s per attempt and a
    # 15-minute budget, decide always reaches an inconclusive (re-run) in time.
    def __init__(self, image: str, goproxy: str = DEFAULT_GOPROXY,
                 cache_volume: str | None = "class-scope-gomodcache",
                 retries: int = 3, backoff: float = 5.0, docker: str = "docker",
                 timeout: float = 240.0, gosumdb: str = "sum.golang.org",
                 budget: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self.image = image
        self.goproxy = goproxy
        self.cache_volume = cache_volume
        self.retries = retries
        self.backoff = backoff
        self.docker = docker
        self.timeout = timeout
        self.gosumdb = gosumdb
        self.budget = budget
        self.clock = clock
        self._mount_ok: bool | None = None

    def mount_visible(self) -> str | None:
        """None when a file in the work folder is visible inside a container;
        otherwise why not. On a laptop whose Docker daemon runs in a VM, a
        folder the daemon does not share mounts as an empty directory, and
        `go` then fails with the misleading "go.mod file not found"."""
        if self._mount_ok:
            return None
        work = os.path.realpath(tempfile.mkdtemp(prefix="class-scope-probe-", dir=work_base()))
        try:
            with open(os.path.join(work, "probe"), "w") as f:
                f.write("probe\n")
            cmd = [self.docker, "run", "--rm", "-v", f"{work}:/w", "--entrypoint", "sh", self.image,
                   "-c", "test -f /w/probe"]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
            except FileNotFoundError:
                return "docker is not installed"
            except subprocess.TimeoutExpired:
                return "the work-folder check timed out"
            if proc.returncode != 0:
                return (f"the work folder {os.path.dirname(work)} is not visible inside a container (exit "
                        f"{proc.returncode}); set CLASS_SCOPE_WORKDIR to a folder the Docker daemon shares")
            self._mount_ok = True
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def __call__(self, files: dict[str, bytes], module: str, version: str) -> RecomputeResult:
        if not MODULE_PATH_RE.match(module) or not is_semver(version):
            return RecomputeResult(False, error=f"refusing to recompute {module}@{version}")
        problem = self.mount_visible()
        if problem:
            return RecomputeResult(False, error=problem)
        log = []
        last = RecomputeResult(False)
        start = self.clock()
        for attempt in range(1, self.retries + 1):
            if attempt > 1 and self.clock() - start + self.timeout > self.budget:
                log.append(f"attempt {attempt}: not started, the recompute budget of {self.budget:g} s would be exceeded")
                last.network = True  # out of time: a re-run settles it, as for the network
                break
            res = self._once(files, module, version)
            res.attempts = attempt
            log.append(f"attempt {attempt}: {'ok' if res.ok else res.error[-400:]}")
            if res.ok:
                res.log = "\n".join(log)
                return res
            last = res
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        last.log = "\n".join(log)
        return last

    def _once(self, files: dict[str, bytes], module: str, version: str) -> RecomputeResult:
        work = os.path.realpath(tempfile.mkdtemp(prefix="class-scope-", dir=work_base()))
        try:
            for path, data in files.items():
                dest = os.path.join(work, path)
                if os.path.commonpath([work, os.path.abspath(dest)]) != work:
                    return RecomputeResult(False, error=f"unsafe path in main's tree: {path}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(data)
            cmd = [self.docker, "run", "--rm", "-v", f"{work}:/w", "-w", "/w"]
            if self.cache_volume:
                cmd += ["-v", f"{self.cache_volume}:/go/pkg/mod"]
            for k, v in (("GOTOOLCHAIN", "local"), ("GOFLAGS", ""), ("GOWORK", "off"),
                         ("GOPROXY", self.goproxy), ("GOSUMDB", self.gosumdb),
                         ("GONOSUMDB", ""), ("GONOPROXY", ""), ("GOPRIVATE", ""),
                         ("GOINSECURE", ""), ("CGO_ENABLED", "0")):
                cmd += ["-e", f"{k}={v}"]
            # Arguments are passed positionally, never spliced into the script.
            cmd += [self.image, "sh", "-ec", 'go get "$1"; go mod tidy', "sh", f"{module}@{version}"]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
            except FileNotFoundError:
                return RecomputeResult(False, error="docker is not installed")
            except subprocess.TimeoutExpired:
                return RecomputeResult(False, network=True, error="recompute timed out")
            out = (proc.stdout + proc.stderr).decode(errors="replace")
            if proc.returncode != 0:
                # 125-127: Docker itself failed (image pull, daemon). An image
                # pull failure is the network too.
                net = looks_like_network(out) or (proc.returncode == 125 and "pull" in out.lower())
                return RecomputeResult(False, network=net, error=f"exit {proc.returncode}: {out.strip()}")
            with open(os.path.join(work, "go.mod"), "rb") as f:
                gm = f.read()
            gs = b""
            if os.path.exists(os.path.join(work, "go.sum")):
                with open(os.path.join(work, "go.sum"), "rb") as f:
                    gs = f.read()
            return RecomputeResult(True, gm, gs, log=out)
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Repository reads through the API
# ---------------------------------------------------------------------------


def _b64(blob: dict) -> bytes:
    if blob.get("encoding") != "base64":
        raise OwnFailure(f"blob {blob.get('sha')} is not base64")
    return base64.b64decode(blob["content"])


def branch_tip(api, repo: str, branch: str) -> str:
    ref = api.get(f"/repos/{repo}/git/ref/heads/{branch}")
    obj = ref["object"]
    if obj.get("type") != "commit":
        raise OwnFailure(f"{repo} {branch} does not point at a commit")
    return obj["sha"]


def commit_info(api, repo: str, sha: str) -> dict:
    c = api.get(f"/repos/{repo}/git/commits/{sha}")
    return {"sha": c["sha"], "tree": c["tree"]["sha"], "parents": [p["sha"] for p in c["parents"]]}


def tree_entries(api, repo: str, tree_sha: str, recursive: bool) -> list[dict]:
    t = api.get(f"/repos/{repo}/git/trees/{tree_sha}", **({"recursive": "1"} if recursive else {}))
    if t.get("truncated"):
        raise OwnFailure(f"tree {tree_sha} of {repo} is truncated")
    return t["tree"]


def blob(api, repo: str, sha: str) -> bytes:
    return _b64(api.get(f"/repos/{repo}/git/blobs/{sha}"))


def module_sources(api, repo: str, commit_sha: str) -> dict[str, bytes]:
    """go.mod, go.sum and every .go file at a commit, plus nested go.mod files
    so that `go mod tidy` sees the same module boundaries as on main. Test
    files are included because tidy counts test imports."""
    info = commit_info(api, repo, commit_sha)
    out: dict[str, bytes] = {}
    for e in tree_entries(api, repo, info["tree"], recursive=True):
        if e.get("type") != "blob":
            continue
        p = e["path"]
        if p in CLASS_FILES or p.endswith(".go") or p.endswith("/go.mod"):
            if e.get("mode") == "120000":
                raise OwnFailure(f"{p} on main is a symbolic link")
            out[p] = blob(api, repo, e["sha"])
    if "go.mod" not in out:
        raise OwnFailure(f"no go.mod at {commit_sha}")
    return out


def class_files_at(api, repo: str, commit_sha: str) -> tuple[dict[str, bytes | None], dict[str, str]]:
    """The two class files at a commit, as bytes (None when absent), and their
    tree modes. Read from the root tree, so a symlink or a submodule named
    go.mod is seen as what it is."""
    info = commit_info(api, repo, commit_sha)
    files: dict[str, bytes | None] = {p: None for p in CLASS_FILES}
    modes: dict[str, str] = {}
    for e in tree_entries(api, repo, info["tree"], recursive=False):
        if e["path"] in CLASS_FILES:
            modes[e["path"]] = f"{e.get('mode')} {e.get('type')}"
            if e.get("type") == "blob":
                files[e["path"]] = blob(api, repo, e["sha"])
    return files, modes


def read_class_file(api, controls_repo: str, path: str) -> tuple[str, str]:
    sha = branch_tip(api, controls_repo, "main")
    c = api.get(f"/repos/{controls_repo}/contents/{path}", ref=sha)
    if isinstance(c, list) or c.get("type") != "file":
        raise OwnFailure(f"{controls_repo}:{path} is not a file")
    return _b64(c).decode("utf-8"), sha


def read_records(api, controls_repo: str, repo: str | None = None, branch: str = RECORDS_BRANCH) -> dict:
    """Run records and seed records from the controls repo's `records` branch
    (written only by workflows; force-push and deletion refused). A missing
    branch means no queue-item pull request has been opened yet. With
    `repo`, a run record written for another repository (probe P4 runs the
    agent's workflow against the throwaway repository) is left out, so a
    colliding pull request number changes nothing here."""
    try:
        sha = branch_tip(api, controls_repo, branch)
    except ApiError as e:
        if e.status == 404:
            return {"commit": None, "runs": [], "seed_prs": set()}
        raise
    info = commit_info(api, controls_repo, sha)
    runs, seeds = [], set()
    for e in tree_entries(api, controls_repo, info["tree"], recursive=True):
        if e.get("type") != "blob":
            continue
        p = e["path"]
        if p.startswith("runs/") and p.endswith(".json"):
            rec = json.loads(blob(api, controls_repo, e["sha"]))
            if repo is None or (isinstance(rec, dict) and rec.get("repo") == repo):
                runs.append(rec)
        elif p.startswith("seeds/") and p.endswith(".json"):
            stem = p[len("seeds/") : -len(".json")]
            if stem.isdigit():
                seeds.add(int(stem))
    return {"commit": sha, "runs": runs, "seed_prs": seeds}


def head_pushed_at(api, repo: str, head_ref: str, head_sha: str) -> dt.datetime | None:
    """When the head commit arrived on the pull request's branch, from the
    repository activity record (GitHub's own push log; no author field the
    pusher controls). None when it cannot be found, e.g. a fork's branch."""
    try:
        acts = api.get(f"/repos/{repo}/activity", ref=f"refs/heads/{head_ref}", per_page=100)
    except ApiError:
        return None
    times = [parse_time(a["timestamp"]) for a in acts or [] if a.get("after") == head_sha]
    return max(times) if times else None


# ---------------------------------------------------------------------------
# Labels in force
# ---------------------------------------------------------------------------


def labels_in_force(events: list[dict], human_logins: set[str],
                    head_pushed: dt.datetime | None,
                    synchronize: bool = False, same_repo_head: bool = True) -> tuple[dict, list[dict]]:
    """The rule (ADR-0024 §6; readiness note C-17): a person's label is in
    force from a listed human login's `labeled` event that carries no
    `performed_via_github_app`, until a listed human login's `unlabeled`
    event. An App's (or anyone else's) label or removal changes nothing and
    is reported as an unknown. `human-cleared` and `escape:revert` are also
    bound to the head commit: they count only when applied after that commit
    was pushed, and this check's own removal on `synchronize` ends them."""
    state: dict[str, dict | None] = {l: None for l in HEAD_BOUND_LABELS}
    unknown: list[dict] = []
    evs = [e for e in events if e.get("event") in ("labeled", "unlabeled")]
    evs.sort(key=lambda e: (e.get("created_at", ""), e.get("id", 0)))
    for e in evs:
        name = (e.get("label") or {}).get("name")
        if name not in HUMAN_LABELS:
            continue
        actor = ((e.get("actor") or {}).get("login") or "")
        app = e.get("performed_via_github_app")
        human = actor.lower() in human_logins and not app
        at = e.get("created_at")
        if e["event"] == "labeled":
            if not human:
                unknown.append({"event_id": e.get("id"), "label": name, "event": "labeled",
                                "actor": actor, "at": at, "via_app": (app or {}).get("slug") if app else None,
                                "effect": "ignored: not a listed human, or made through an App"})
                continue
            if name in state:
                if head_pushed is not None and at and parse_time(at) < head_pushed:
                    state[name] = None  # applied to an earlier head
                else:
                    state[name] = {"actor": actor, "at": at, "event_id": e.get("id")}
        else:
            own = actor == ACTIONS_BOT_LOGIN and name in HEAD_BOUND_LABELS
            if human or own:
                if name in state:
                    state[name] = None
                continue
            unknown.append({"event_id": e.get("id"), "label": name, "event": "unlabeled",
                            "actor": actor, "at": at, "via_app": (app or {}).get("slug") if app else None,
                            "effect": "unknown: removal not counted"})
    if synchronize:
        # A new head: every head-bound label is for an earlier commit.
        state = {l: None for l in HEAD_BOUND_LABELS}
    if head_pushed is None and not same_repo_head:
        # A fork's head: its push time cannot be read here, so a head-bound
        # label cannot be tied to this head and does not count.
        state = {l: None for l in HEAD_BOUND_LABELS}
    return state, unknown


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def queue_runs(runs: list[dict], prs: dict[int, dict]) -> list[dict]:
    """The run records whose pull request is the one the open job made: the
    pull request exists and its head branch is the record's `branch`."""
    out = []
    for r in runs:
        n = r.get("pr_number")
        p = prs.get(n) if isinstance(n, int) else None
        if p is not None and ((p.get("head") or {}).get("ref") or None) == (r.get("branch") or None):
            out.append(r)
    return out


def near_miss_positions(runs: list[dict], seed_prs: set[int], pulls_closed: list[dict],
                        reopened_closed: Callable[[int], bool],
                        retargeted: Iterable[int] = ()) -> dict[int, int]:
    """{queue position: pull request number} for every non-seed queue-item
    pull request closed unmerged. Read from GitHub's state: a pull request
    that is closed and not merged now, one that is open again after a close
    (a close is never a free retry), or one whose base was changed away from
    main (it no longer reaches main). A queue-item pull request is one whose
    number is in a run record; a seed is one with a seed record."""
    closed = {p["number"]: p for p in pulls_closed}
    moved = set(retargeted)
    out: dict[int, int] = {}
    for r in runs:
        n, pos = r.get("pr_number"), r.get("queue_position")
        if not isinstance(n, int) or not isinstance(pos, int) or n in seed_prs:
            continue
        p = closed.get(n)
        if n in moved and not (p or {}).get("merged_at"):
            out.setdefault(pos, n)
        elif p is not None:
            if not p.get("merged_at"):
                out.setdefault(pos, n)
        elif reopened_closed(n):
            out.setdefault(pos, n)
    return out


def next_item(queue: list[dict], main_versions: dict[str, str],
              near_misses: dict[int, int]) -> dict | None:
    """The first queue item whose version is above main's and that no
    near-miss closed (readiness note, the frozen numbers). A reverted item is
    queued again because main's version drops below it."""
    for item in queue:
        cur = main_versions.get(item["module"])
        if cur is None:
            raise OwnFailure(f"queue module {item['module']} is not required by main's go.mod")
        if not version_lt(cur, item["version"]):
            continue
        if item["position"] in near_misses:
            continue
        return item
    return None


# ---------------------------------------------------------------------------
# Diagnosis: why two go.mod/go.sum pairs differ. The verdict is the byte
# comparison; these are its reasons, in words a reviewer can check.
# ---------------------------------------------------------------------------


def diagnose(main_mod: bytes, main_sum: bytes, head_mod: bytes | None, head_sum: bytes | None,
             exp_mod: bytes, exp_sum: bytes, target: dict) -> list[tuple[str, str]]:
    r: list[tuple[str, str]] = []
    if head_mod is None:
        return [("class-file-missing", "go.mod is absent at the head commit")]
    if head_sum is None:
        r.append(("class-file-missing", "go.sum is absent at the head commit"))
        head_sum = b""
    for name, data in (("go.mod", head_mod), ("go.sum", head_sum)):
        if b"\r\n" in data:
            r.append(("line-endings", f"{name} has CRLF line endings"))
    try:
        hm_t, mm_t, em_t = head_mod.decode(), main_mod.decode(), exp_mod.decode()
        hs_t, es_t = head_sum.decode(), exp_sum.decode()
    except UnicodeDecodeError:
        return r + [("encoding", "a class file is not UTF-8 text")]
    hm, mm, em = parse_gomod(hm_t), parse_gomod(mm_t), parse_gomod(em_t)

    for verb, text in hm.directives:
        if (verb, text) not in mm.directives:
            r.append(("gomod-directive", f"go.mod has a {verb} line the class forbids: {text!r}"))
    for verb, text in mm.directives:
        if (verb, text) not in hm.directives:
            r.append(("gomod-directive", f"go.mod drops main's {verb} line: {text!r}"))
    if hm.module != mm.module:
        r.append(("module-path", f"module path changed: {mm.module} -> {hm.module}"))
    if hm.go != mm.go:
        r.append(("go-directive", f"go directive changed: {mm.go} -> {hm.go}"))
    for p in hm.duplicate_requires:
        r.append(("gomod-duplicate", f"{p} is required twice"))

    hv, mv, ev = gomod_versions(hm), gomod_versions(mm), gomod_versions(em)
    for p in sorted(set(hv) - set(mv)):
        r.append(("module-added", f"module added: {p} {hv[p]}"))
    for p in sorted(set(mv) - set(hv)):
        r.append(("module-removed", f"module removed: {p} (was {mv[p]})"))

    tmod, tver = target["module"], target["version"]
    head_target = hv.get(tmod)
    moved_others = [p for p in hv if p in mv and p != tmod and hv[p] != mv[p]]
    if head_target == mv.get(tmod) and moved_others:
        r.append(("wrong-target",
                  f"the queue's next item is {tmod} {tver}; the head leaves {tmod} at {mv.get(tmod)} "
                  f"and moves {', '.join(f'{p} {mv[p]} -> {hv[p]}' for p in moved_others)}"))
    for p in sorted(p for p in hv if p in mv and hv[p] != mv[p]):
        old, new = mv[p], hv[p]
        if not is_semver(new):
            r.append(("bad-version", f"{p} {new} is not a semantic version"))
            continue
        if is_pseudo(new):
            r.append(("pseudo-version", f"{p} moves to a pseudo-version: {old} -> {new}"))
        elif not is_release(new):
            r.append(("pre-release", f"{p} moves to a pre-release: {old} -> {new}"))
        if is_semver(old) and version_lt(new, old):
            r.append(("downgrade", f"{p} is downgraded: {old} -> {new}"))
        if is_semver(old) and major_of(new) != major_of(old):
            r.append(("major-version", f"{p} changes major version: {old} -> {new}"))
        if p == tmod:
            if new != tver:
                r.append(("target-version", f"target {p} moves to {new}; the queue's next item is {tver}"))
        elif ev.get(p) == old:
            r.append(("unforced-module", f"{p} moves {old} -> {new}, which the target's requirements do not force"))
        elif ev.get(p) != new:
            r.append(("forced-version", f"forced module {p} moves to {new}; the recompute gives {ev.get(p)}"))
    for p in sorted(p for p in ev if p in mv and ev[p] != mv[p] and hv.get(p) == mv[p] and p != tmod):
        r.append(("forced-missing", f"{p} must move {mv[p]} -> {ev[p]} with the target and does not"))
    for p in sorted(set(hm.requires) & set(mm.requires)):
        a, b = hm.requires[p], mm.requires[p]
        if a.indirect != b.indirect:
            r.append(("indirect-flip", f"{p}: // indirect {'added' if a.indirect else 'removed'}"))
        elif a.block != b.block:
            r.append(("require-block", f"{p} moved {'into' if a.block else 'out of'} a require block"))

    if head_mod != exp_mod and not r:
        r.append(("gomod-format", "go.mod differs from the recompute in comments, spacing or line order"))

    hs, es = parse_gosum(hs_t), parse_gosum(es_t)
    for k in sorted(set(hs) & set(es)):
        if hs[k] != es[k]:
            r.append(("gosum-hash", f"go.sum hash for {k[0]} {k[1]} differs from the recompute"))
    extra = sorted(set(hs) - set(es))
    missing = sorted(set(es) - set(hs))
    if extra:
        r.append(("gosum-extra", "go.sum has lines the recompute does not: "
                  + "; ".join(f"{m} {v}" for m, v in extra[:6]) + (" ..." if len(extra) > 6 else "")))
    if missing:
        r.append(("gosum-missing", "go.sum lacks lines the recompute has: "
                  + "; ".join(f"{m} {v}" for m, v in missing[:6]) + (" ..." if len(missing) > 6 else "")))
    if head_sum != exp_sum and not any(c.startswith("gosum") for c, _ in r):
        r.append(("gosum-format", "go.sum differs from the recompute in order or spacing"))
    return r


def module_moves(old_mod: bytes, new_mod: bytes) -> list[dict]:
    # errors="replace": a file that is not UTF-8 is still described; the
    # verdict on it is made by the byte comparison (reason `encoding`).
    a = gomod_versions(parse_gomod(old_mod.decode(errors="replace")))
    b = gomod_versions(parse_gomod(new_mod.decode(errors="replace")))
    return [{"module": p, "old": a.get(p), "new": b.get(p)} for p in sorted(set(a) | set(b)) if a.get(p) != b.get(p)]


def gosum_changes(old_sum: bytes, new_sum: bytes) -> dict:
    a = set(old_sum.decode(errors="replace").splitlines())
    b = set(new_sum.decode(errors="replace").splitlines())
    return {"removed": sorted(a - b), "added": sorted(b - a)}


def is_utf8(data: bytes | None) -> bool:
    try:
        (data or b"").decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass
class Deps:
    api: Any                                   # get / paginate / post / delete
    proxy: Any                                 # .info(module, version) -> dict
    recompute_factory: Callable[[str], Callable[[dict, str, str], RecomputeResult]]
    now: Callable[[], dt.datetime] = lambda: utcnow()
    class_file_text: str | None = None         # local tests: the class file's text
    class_file_commit: str | None = None


@dataclass
class Event:
    repo: str
    action: str
    pr: int
    head_sha: str
    head_ref: str
    head_repo: str | None
    base_ref: str
    html_url: str = ""
    body: str = ""

    @staticmethod
    def from_payload(repo: str, payload: dict) -> "Event":
        pr = payload["pull_request"]
        return Event(
            repo=repo,
            action=payload.get("action", ""),
            pr=int(pr["number"]),
            head_sha=pr["head"]["sha"],
            head_ref=pr["head"]["ref"],
            head_repo=((pr["head"].get("repo") or {}).get("full_name")),
            base_ref=pr["base"]["ref"],
            html_url=pr.get("html_url", ""),
            body=pr.get("body") or "",
        )


class Verdict:
    def __init__(self, event: Event):
        self.d: dict[str, Any] = {
            "schema": SCHEMA, "verdict": None, "reason_class": None, "reasons": [],
            "reason_codes": [], "pass_kind": None, "head_sha": event.head_sha, "main_sha": None,
            "pr": event.pr, "event_action": event.action, "queue_position": None, "module": None,
            "old": None, "new": None, "forced": [], "release_time": None, "release_age_days": None,
            "class_file_commit": None, "verifier_version": VERIFIER_VERSION,
            "builder_image": None, "labels_in_force": {l: None for l in HEAD_BOUND_LABELS},
            "unknown_label_events": [], "head_pushed_at": None, "records_commit": None,
            "near_misses": {}, "changed_files": [], "gomod_change": [], "gosum_change": None,
            "opened_by": None, "recompute": None, "label_writes": [], "decided_at": None,
            "tampers": [], "packet_comment": None, "head_bound_note": None,
        }

    def reason(self, code: str, text: str):
        self.d["reason_codes"].append(code)
        self.d["reasons"].append(text)

    def finish(self, verdict: str, reason_class: str | None = None, pass_kind: str | None = None):
        self.d["verdict"] = verdict
        self.d["reason_class"] = reason_class
        self.d["pass_kind"] = pass_kind
        return self.d


def decide(event: Event, deps: Deps, force_var: str = "", controls_repo: str = CONTROLS_REPO,
           class_file_path: str = CLASS_FILE_PATH, records_branch: str = RECORDS_BRANCH) -> dict:
    """Compute the full verdict for one event. Never raises: any failure of
    this check's own is an inconclusive of the re-run class, never a pass."""
    v = Verdict(event)
    v.d["decided_at"] = iso(deps.now())
    try:
        return _decide(event, deps, v, force_var.strip(), controls_repo, class_file_path, records_branch)
    except ProxyUnreachable as e:
        v.reason("proxy-unreachable", f"module proxy unreachable after retries: {e}")
        return v.finish("inconclusive", "rerun")
    except OwnFailure as e:
        v.reason("own-failure", f"class-scope could not decide: {e}")
        return v.finish("inconclusive", "rerun")
    except ApiError as e:
        v.reason("own-failure", f"class-scope could not read GitHub: {e}")
        return v.finish("inconclusive", "rerun")
    except Exception as e:  # noqa: BLE001 - a bug here must never become a pass
        v.reason("own-failure", f"class-scope failed: {type(e).__name__}: {e}")
        return v.finish("inconclusive", "rerun")


def opened_by_from_body(body: str) -> dict | None:
    """The controls run named in the pull request body the open job writes
    ("Opened by the factory-controls agent workflow, run <id>, attempt <n>").
    Used only until the run record exists (the record is written just after
    the pull request opens, so the first run on `opened` may not see it), and
    labelled unverified: the body is text the pull request controls."""
    m = re.search(r"factory-controls agent workflow, run (\d+), attempt (\d+)", body or "")
    if not m:
        return None
    return {"controls_run_id": int(m.group(1)), "controls_run_attempt": int(m.group(2)), "job": "open",
            "source": "pull request body (unverified until the run record exists)"}


def read_run_prs(api, repo: str, runs: list[dict], event: Event) -> dict[int, dict]:
    """Every run-record pull request, read by number whatever its base (a
    pull request retargeted away from main drops out of a base=main listing).
    The event's own pull request comes from the event."""
    out: dict[int, dict] = {}
    for n in sorted({r.get("pr_number") for r in runs if isinstance(r.get("pr_number"), int)}):
        if n == event.pr:
            out[n] = {"number": n, "state": "open", "merged_at": None, "merge_commit_sha": None,
                      "head": {"sha": event.head_sha, "ref": event.head_ref}, "base": {"ref": event.base_ref}}
            continue
        try:
            out[n] = api.get(f"/repos/{repo}/pulls/{n}")
        except ApiError as e:
            if e.status != 404:
                raise
    return out


def _decide(event: Event, deps: Deps, v: Verdict, force_var: str, controls_repo: str,
            class_file_path: str, records_branch: str = RECORDS_BRANCH) -> dict:
    api, repo, d = deps.api, event.repo, v.d

    # --- inputs every outcome needs -------------------------------------
    if deps.class_file_text is not None:
        cf_text, cf_commit = deps.class_file_text, deps.class_file_commit or "local"
    else:
        cf_text, cf_commit = read_class_file(api, controls_repo, class_file_path)
    d["class_file_commit"] = cf_commit
    cs = class_settings(load_class_text(cf_text))
    d["builder_image"] = cs["builder_image"]

    main_sha = branch_tip(api, repo, "main")
    d["main_sha"] = main_sha

    events = api.paginate(f"/repos/{repo}/issues/{event.pr}/events")
    pushed = None
    same_repo = event.head_repo in (None, repo)
    if same_repo:
        pushed = head_pushed_at(api, repo, event.head_ref, event.head_sha)
    d["head_pushed_at"] = iso(pushed)
    in_force, unknown = labels_in_force(events, cs["human_logins"], pushed,
                                        synchronize=event.action == "synchronize", same_repo_head=same_repo)
    if pushed is None:
        d["head_bound_note"] = ("the head's push time could not be read: human-cleared and escape:revert count "
                                + ("as applied (a branch of this repository; this check removes both on every "
                                   "push)" if same_repo else "not at all (a fork's head)"))
    d["labels_in_force"] = in_force
    d["unknown_label_events"] = unknown
    cleared = in_force.get(LABEL_HUMAN_CLEARED) is not None
    revert = in_force.get(LABEL_ESCAPE_REVERT) is not None

    records = read_records(api, controls_repo, repo, records_branch)
    d["records_commit"] = records["commit"]
    run_prs = read_run_prs(api, repo, records["runs"], event)
    runs = queue_runs(records["runs"], run_prs)
    pulls_closed = [p for p in run_prs.values() if p.get("state") == "closed"]
    retargeted = sorted(n for n, p in run_prs.items() if (p.get("base") or {}).get("ref") not in (None, "main"))
    for n in retargeted:
        d["tampers"].append({"code": "base-changed", "pr": n,
                             "text": f"queue-item pull request #{n} targets "
                                     f"{(run_prs[n].get('base') or {}).get('ref')}, not main: closed to the class"})
    open_run_prs = {r.get("pr_number") for r in runs} - {p["number"] for p in pulls_closed}

    def reopened_closed(n: int) -> bool:
        # Only for queue-item pull requests that are open now: were they ever
        # closed? (A merged pull request is in the closed list, not here.)
        evs = events if n == event.pr else api.paginate(f"/repos/{repo}/issues/{n}/events")
        return any(e.get("event") == "closed" for e in evs)

    near = near_miss_positions(runs, records["seed_prs"], pulls_closed,
                               lambda n: n in open_run_prs and reopened_closed(n), retargeted)
    d["near_misses"] = {str(k): n for k, n in sorted(near.items())}
    for r in runs:
        if r.get("pr_number") == event.pr:
            d["opened_by"] = {k: r.get(k) for k in ("controls_run_id", "controls_run_attempt", "job",
                                                     "queue_position", "module", "version", "opened_at")}
    if d["opened_by"] is None:
        d["opened_by"] = opened_by_from_body(event.body)

    main_src = module_sources(api, repo, main_sha)
    main_mod, main_sum = main_src["go.mod"], main_src.get("go.sum", b"")
    item = next_item(cs["queue"], gomod_versions(parse_gomod(main_mod.decode())), near)
    if item:
        d["queue_position"], d["module"], d["new"] = item["position"], item["module"], item["version"]
        d["old"] = gomod_versions(parse_gomod(main_mod.decode())).get(item["module"])

    # --- the pull request's shape ---------------------------------------
    if event.base_ref != "main":
        v.reason("base-not-main", f"the pull request targets {event.base_ref}, not main")
        return v.finish("fail")

    cmp = api.get(f"/repos/{repo}/compare/{main_sha}...{event.head_sha}")
    if cmp.get("behind_by", 0) != 0 or cmp.get("status") not in ("ahead", "identical"):
        v.reason("head-behind-main",
                 f"the head does not contain main's tip {main_sha[:12]} (compare status "
                 f"{cmp.get('status')}, behind by {cmp.get('behind_by')}); update the branch")
        return v.finish("fail")
    files = cmp.get("files") or []
    d["changed_files"] = [{"filename": f["filename"], "status": f["status"]} for f in files]
    if len(files) >= 300:
        v.reason("path-outside-class", "the pull request changes 300 or more files")
        return v.finish("fail")
    other = [f for f in files if f["filename"] not in CLASS_FILES or f.get("previous_filename")]
    class_changed = [f for f in files if f["filename"] in CLASS_FILES and not f.get("previous_filename")]

    # Pass for a person's pull request that changes neither class file.
    if not class_changed:
        if cleared:
            return v.finish("pass", None, "human-cleared-nonclass")
        for f in other:
            v.reason("path-outside-class", f"path outside the class: {f['filename']} ({f['status']})")
        v.reason("nonclass-not-cleared",
                 "the pull request changes neither go.mod nor go.sum; it passes only with "
                 "human-cleared applied by a listed human for this head commit")
        return v.finish("fail")

    if other:
        for f in other:
            name = f["filename"] + (f" (from {f['previous_filename']})" if f.get("previous_filename") else "")
            v.reason("path-outside-class", f"path outside the class: {name} ({f['status']})")
        return v.finish("fail")

    head_files, head_modes = class_files_at(api, repo, event.head_sha)
    bad_mode = False
    for f in class_changed:
        if f["status"] != "modified":
            v.reason("class-file-status", f"{f['filename']} is {f['status']}, not modified")
            bad_mode = True
    for p, m in head_modes.items():
        if m != "100644 blob":
            v.reason("class-file-mode", f"{p} at the head is {m}, not a regular file")
            bad_mode = True
    if bad_mode:
        return v.finish("fail")
    head_mod, head_sum = head_files["go.mod"], head_files["go.sum"]
    # Bytes first: a class file that is not UTF-8 text is off-class, and must
    # never reach a decode that would turn it into the check's own failure
    # (an inconclusive nobody clears and nothing closes).
    for name, data in (("go.mod", head_mod), ("go.sum", head_sum)):
        if data is not None and not is_utf8(data):
            v.reason("encoding", f"{name} at the head is not UTF-8 text")
    if any(c == "encoding" for c in d["reason_codes"]):
        d["gomod_change"] = module_moves(main_mod, head_mod or b"")
        d["gosum_change"] = gosum_changes(main_sum, head_sum or b"")
        return v.finish("fail")

    # Pass for a revert of the latest class merge, by a person, with
    # escape:revert in force (ADR-0024 §6, §9 (b)).
    revert_note = None
    if revert:
        latest = latest_class_merge(runs, pulls_closed)
        if latest is None:
            revert_note = "escape:revert is in force but no class merge exists to revert"
        else:
            parent = commit_info(api, repo, latest["merge_commit_sha"])["parents"][0]
            pfiles, _ = class_files_at(api, repo, parent)
            d["revert_of"] = {"pr": latest["number"], "merge_commit": latest["merge_commit_sha"],
                              "parent": parent}
            if pfiles["go.mod"] == head_mod and pfiles["go.sum"] == head_sum:
                d["gomod_change"] = module_moves(main_mod, head_mod or b"")
                d["gosum_change"] = gosum_changes(main_sum, head_sum or b"")
                return v.finish("pass", None, "escape-revert")
            revert_note = (f"escape:revert is in force but go.mod and go.sum do not equal those at "
                           f"{parent[:12]}, the parent of the latest class merge (#{latest['number']})")

    # --- the class: recompute and compare byte for byte ------------------
    if item is None:
        v.reason("queue-exhausted", "no queue item is above main's version and open (queue done)")
        if revert_note:
            v.reason("revert-mismatch", revert_note)
        return v.finish("fail")

    recompute = deps.recompute_factory(cs["builder_image"])
    res = recompute(main_src, item["module"], item["version"])
    d["recompute"] = {"ok": res.ok, "attempts": res.attempts, "network": res.network,
                      "error": res.error[-1500:] if res.error else ""}
    if not res.ok:
        if res.network:
            v.reason("proxy-unreachable",
                     f"module proxy or sum database unreachable after {res.attempts} attempt(s): "
                     f"{res.error.strip().splitlines()[-1] if res.error.strip() else 'no output'}")
        else:
            v.reason("recompute-failed",
                     f"the recompute failed (class-scope's own failure): "
                     f"{res.error.strip().splitlines()[-1] if res.error.strip() else 'no output'}")
        return v.finish("inconclusive", "rerun")

    d["forced"] = [m for m in module_moves(main_mod, res.go_mod) if m["module"] != item["module"]]
    d["gomod_change"] = module_moves(main_mod, head_mod or b"")
    d["gosum_change"] = gosum_changes(main_sum, head_sum or b"")
    d["expected_sha256"] = {"go.mod": sha256_hex(res.go_mod), "go.sum": sha256_hex(res.go_sum)}
    d["head_sha256"] = {"go.mod": sha256_hex(head_mod or b""), "go.sum": sha256_hex(head_sum or b"")}

    if head_mod != res.go_mod or head_sum != res.go_sum:
        for code, text in diagnose(main_mod, main_sum, head_mod, head_sum, res.go_mod, res.go_sum, item):
            v.reason(code, text)
        v.reason("recompute-mismatch",
                 f"go.mod and go.sum are not byte for byte what `go get {item['module']}@{item['version']}` "
                 f"and `go mod tidy` make from main {main_sha[:12]}")
        if revert_note:
            v.reason("revert-mismatch", revert_note)
        return v.finish("fail")

    # The change is the queue's next item. What remains is whether it may
    # pass now.
    clearable: list[tuple[str, str]] = []
    if force_var and force_var == f"{item['module']}@{item['version']}":
        clearable.append(("forced", FORCED_REASON))
    info = deps.proxy.info(item["module"], item["version"])
    rt = parse_time(info["Time"])
    age = (deps.now() - rt).total_seconds() / 86400.0
    d["release_time"] = iso(rt)
    d["release_age_days"] = round(age, 2)
    if age < cs["min_release_age_days"]:
        clearable.append(("release-young",
                          f"{item['module']} {item['version']} was released {iso(rt)}, "
                          f"{age:.2f} days ago; the class's minimum age is {cs['min_release_age_days']:g} days"))
    if clearable:
        for code, text in clearable:
            v.reason(code, text)
        if cleared:
            v.reason("cleared", f"cleared by {in_force[LABEL_HUMAN_CLEARED]['actor']} at "
                                f"{in_force[LABEL_HUMAN_CLEARED]['at']} (human-cleared, this head)")
            return v.finish("pass", None, "class")
        return v.finish("inconclusive", "clearable")
    return v.finish("pass", None, "class")


def latest_class_merge(runs: list[dict], pulls_closed: list[dict]) -> dict | None:
    nums = {r.get("pr_number") for r in runs}
    merged = [p for p in pulls_closed if p["number"] in nums and p.get("merged_at") and p.get("merge_commit_sha")]
    return max(merged, key=lambda p: p["merged_at"]) if merged else None


# ---------------------------------------------------------------------------
# The packet (job summary)
# ---------------------------------------------------------------------------


def upstream_links(module: str, old: str | None, new: str | None) -> list[str]:
    links = []
    m = re.match(r"^github\.com/([^/]+)/([^/]+)", module)
    x = re.match(r"^golang\.org/x/([^/]+)$", module)
    if m and old and new:
        links.append(f"https://github.com/{m.group(1)}/{m.group(2)}/compare/{old}...{new}")
    elif x and old and new:
        links.append(f"https://github.com/golang/{x.group(1)}/compare/{old}...{new}")
    if new:
        links.append(f"https://pkg.go.dev/{module}@{new}")
    return links


# Text the pull request controls (file names, go.mod lines, module paths,
# go.sum lines, and the reasons that quote them) reaches the packet only
# through these two functions, so it can add no heading, line, link or code
# fence to the job summary (ADV-13).
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")


def _no_controls(text: str) -> str:
    # JSON's string escapes for every control character (newlines included).
    return "".join(json.dumps(c)[1:-1] if ord(c) < 32 or ord(c) == 127 else c for c in str(text))


def esc(text: Any) -> str:
    """Inline Markdown text: control characters escaped, Markdown punctuation
    backslash-escaped, so it renders as the literal characters."""
    return _MD_SPECIAL.sub(r"\\\1", _no_controls(text))


def code(text: Any) -> str:
    """A code span (or a line inside a fence): control characters escaped and
    backticks replaced, so it cannot close the span or the fence."""
    return _no_controls(text).replace("`", "'")


def render_packet(d: dict, repo: str, server: str = "https://github.com",
                  controls_repo: str = CONTROLS_REPO) -> str:
    L: list[str] = []
    verdict = d.get("verdict") or "no verdict"
    head = (d.get("head_sha") or "")[:12]
    L.append(f"## class-scope: {verdict.upper()}"
             + (f" ({d['reason_class']})" if d.get("reason_class") else "")
             + (f", {d['pass_kind']}" if d.get("pass_kind") else ""))
    L.append("")
    L.append(f"Pull request #{d.get('pr')} at head `{head}`, recomputed from main `{(d.get('main_sha') or '')[:12]}`.")
    L.append("")
    L.append("### Verdict and reasons")
    if d.get("reasons"):
        for rc, text in zip(d.get("reason_codes", []), d["reasons"]):
            L.append(f"- `{code(rc)}`: {esc(text)}")
    elif verdict == "pass":
        L.append("- none: " + pass_sentence(d))
    else:
        L.append("- none recorded")
    L.append("")
    L.append("### The change")
    if d.get("module"):
        L.append(f"- Queue position {d.get('queue_position')}: `{code(d['module'])}` {esc(d.get('old'))} -> {esc(d.get('new'))}")
    else:
        L.append("- Queue: no next item")
    forced = d.get("forced") or []
    L.append("- Forced modules: " + (", ".join(f"`{code(f['module'])}` {esc(f['old'])} -> {esc(f['new'])}"
                                              for f in forced) if forced else "none"))
    gm = d.get("gomod_change") or []
    L.append("- go.mod at the head, in words: " + ("; ".join(
        f"`{code(c['module'])}` {esc(c['old'] or '(absent)')} -> {esc(c['new'] or '(removed)')}" for c in gm) if gm
        else "no requirement changed"))
    gs = d.get("gosum_change")
    if gs:
        L.append(f"- go.sum lines changed: {len(gs['removed'])} removed, {len(gs['added'])} added")
        if gs["removed"] or gs["added"]:
            L.append("")
            L.append("```diff")
            L.extend("- " + code(x) for x in gs["removed"][:40])
            L.extend("+ " + code(x) for x in gs["added"][:40])
            L.append("```")
    if d.get("release_time"):
        L.append(f"- Release: {d['release_time']} by the module proxy's .info, {d.get('release_age_days')} days old")
    if d.get("module"):
        for link in upstream_links(d["module"], d.get("old"), d.get("new")):
            L.append(f"- Upstream: {link}")
    L.append(f"- govulncheck findings: the `go-test` check's job summary on this head ({server}/{repo}/pull/{d.get('pr')}/checks)")
    if d.get("changed_files"):
        L.append("- Paths changed against main: " + ", ".join(f"`{code(f['filename'])}` ({esc(f['status'])})"
                                                              for f in d["changed_files"]))
    L.append("")
    L.append("### Provenance")
    L.append(f"- Class file: `{controls_repo}` `classes/go-module-bump.yaml` at `{d.get('class_file_commit')}`")
    L.append(f"- Verifier: class_scope.py {d.get('verifier_version')}, builder image `{d.get('builder_image')}`")
    ob = d.get("opened_by")
    if ob and ob.get("controls_run_id"):
        L.append(f"- Opened by: {server}/{controls_repo}/actions/runs/{int(ob['controls_run_id'])}"
                 f"/attempts/{int(ob.get('controls_run_attempt') or 1)} (job `{code(ob.get('job'))}`)"
                 + (f"; {esc(ob['source'])}" if ob.get("source") else ""))
    else:
        L.append("- Opened by: no run record names this pull request (not a queue-item pull request)")
    lif = d.get("labels_in_force") or {}
    L.append("- Labels in force: " + ", ".join(
        f"`{k}` " + (f"by {esc(x['actor'])} at {esc(x['at'])}" if x else "no") for k, x in lif.items()))
    if d.get("head_bound_note"):
        L.append(f"- Note: {esc(d['head_bound_note'])}")
    if d.get("unknown_label_events"):
        L.append(f"- Unknown label events (not counted): {len(d['unknown_label_events'])}")
        for u in d["unknown_label_events"]:
            L.append(f"  - `{code(u['label'])}` {esc(u['event'])} by {esc(u['actor'])} at {esc(u['at'])}"
                     + (f" via App `{code(u['via_app'])}`" if u.get("via_app") else "") + f": {esc(u['effect'])}")
    if d.get("tampers"):
        L.append(f"- Tampers reported (the verdict does not rest on them): {len(d['tampers'])}")
        for t in d["tampers"]:
            L.append(f"  - `{code(t.get('code'))}`: {esc(t.get('text'))}")
    if d.get("near_misses"):
        L.append("- Near-miss-closed queue positions: " + ", ".join(f"{k} (#{n})" for k, n in d["near_misses"].items()))
    L.append("")
    L.append("### Agent summary (unverified)")
    L.append(f"The agent's prose is the pull request body ({server}/{repo}/pull/{d.get('pr')}). "
             "It is not evidence, and this packet does not copy it.")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Side effects in the workflow: labels, outputs
# ---------------------------------------------------------------------------


def pass_sentence(d: dict) -> str:
    """Why a pass with no reasons passed, by its kind (ADV-12): the packet is
    the reviewer's evidence, so it must not call a person's pass a recompute."""
    kind = d.get("pass_kind")
    lif = d.get("labels_in_force") or {}
    if kind == "human-cleared-nonclass":
        x = lif.get(LABEL_HUMAN_CLEARED) or {}
        return (f"changes neither class file; cleared by {esc(x.get('actor'))} at {esc(x.get('at'))} "
                "(human-cleared, this head); the wall entry still needs the listed human's approval")
    if kind == "escape-revert":
        r = d.get("revert_of") or {}
        x = lif.get(LABEL_ESCAPE_REVERT) or {}
        return (f"go.mod and go.sum equal those at {esc((r.get('parent') or '')[:12])}, the parent of "
                f"#{esc(r.get('pr'))}, the latest class merge; escape:revert by {esc(x.get('actor'))} at "
                f"{esc(x.get('at'))}")
    return "the change is byte for byte the queue's next item"


def remove_label(api, repo: str, pr: int, name: str) -> dict:
    try:
        api.delete(f"/repos/{repo}/issues/{pr}/labels/{urllib.parse.quote(name, safe='')}")
        return {"label": name, "action": "remove", "ok": True}
    except ApiError as e:
        if e.status == 404:  # not on the pull request: nothing to remove
            return {"label": name, "action": "remove", "ok": True, "note": "absent"}
        return {"label": name, "action": "remove", "ok": False, "error": str(e)[:300]}


def add_label(api, repo: str, pr: int, name: str) -> dict:
    try:
        api.post(f"/repos/{repo}/issues/{pr}/labels", {"labels": [name]})
        return {"label": name, "action": "add", "ok": True}
    except ApiError as e:
        return {"label": name, "action": "add", "ok": False, "error": str(e)[:300]}


def packet_comment_body(repo: str, pr: int, server: str = "https://github.com") -> str:
    """The same text on every run, so any other text is an edit."""
    return (f"{PACKET_COMMENT_MARKER}\n"
            f"The packet for this pull request is the job summary of its `class-scope` check on the head "
            f"commit: {server}/{repo}/pull/{pr}/checks. This comment only links to it and is not evidence "
            f"(ADR-0024 §12). class-scope writes it again if it is edited or deleted, and reports that.\n")


def ensure_packet_comment(api, event: Event, server: str = "https://github.com") -> dict:
    """Create, keep or restore the one comment that links to the packet.
    A missing comment after the pull request's first run, or one whose text
    is not this check's, is reported as a tamper; the verdict does not change."""
    want = packet_comment_body(event.repo, event.pr, server)
    try:
        comments = api.paginate(f"/repos/{event.repo}/issues/{event.pr}/comments")
        mine = [c for c in comments if (c.get("user") or {}).get("login") == ACTIONS_BOT_LOGIN
                and (c.get("body") or "").startswith(PACKET_COMMENT_MARKER)]
        if not mine:
            c = api.post(f"/repos/{event.repo}/issues/{event.pr}/comments", {"body": want}) or {}
            out = {"id": (c or {}).get("id") if isinstance(c, dict) else None, "action": "created"}
            if event.action != "opened":
                out["tamper"] = {"code": "packet-comment-missing",
                                 "text": "the comment linking to the packet was missing; written again"}
            return out
        c = mine[0]
        if c.get("body") != want:
            api.patch(f"/repos/{event.repo}/issues/comments/{c['id']}", {"body": want})
            return {"id": c["id"], "action": "restored",
                    "tamper": {"code": "packet-comment-edited",
                               "text": "the comment linking to the packet had other text; restored"}}
        return {"id": c["id"], "action": "unchanged"}
    except ApiError as e:
        return {"action": "error", "error": str(e)[:300]}


def run(event: Event, deps: Deps, force_var: str = "", payload_labels: Iterable[str] = (),
        controls_repo: str = CONTROLS_REPO, class_file_path: str = CLASS_FILE_PATH,
        records_branch: str = RECORDS_BRANCH, server: str = "https://github.com") -> dict:
    """decide() plus the writes ADR-0024 §6 and §12 give this check: the
    head-bound labels removed on a push, `needs-human` on an inconclusive,
    and the comment that links to the packet."""
    writes = []
    if event.action == "synchronize":
        # Both, always: the event payload's label list can be stale, and an
        # App's removal from the list does not end a person's label (ADV-19).
        # A 404 means the label was not there.
        for name in HEAD_BOUND_LABELS:
            writes.append(remove_label(deps.api, event.repo, event.pr, name))
    d = decide(event, deps, force_var, controls_repo, class_file_path, records_branch)
    failed = [w for w in writes if not w["ok"]]
    if failed:
        # The removal is what keeps an earlier head's clearance from counting
        # on a later run when the push time cannot be read; without it this
        # run must not stand as a verdict a person could clear.
        d["reasons"].append("could not remove a head-bound label on synchronize: "
                            + "; ".join(w["error"] for w in failed))
        d["reason_codes"].append("own-failure")
        d.update(verdict="inconclusive", reason_class="rerun", pass_kind=None)
    if d["verdict"] == "inconclusive":
        writes.append(add_label(deps.api, event.repo, event.pr, LABEL_NEEDS_HUMAN))
    d["label_writes"] = writes
    pc = ensure_packet_comment(deps.api, event, server)
    if pc.get("tamper"):
        d["tampers"].append(pc.pop("tamper"))
    d["packet_comment"] = pc
    return d


def annotation_message(d: dict) -> str:
    """The verdict's fixed fields as compact JSON, escaped for a workflow
    command's message (`%`, CR and LF)."""
    a = {k: d.get(k) for k in ANNOTATION_FIELDS}
    a["tampers"] = [t.get("code") for t in d.get("tampers") or []]
    a["unknown_label_events"] = [{k: u.get(k) for k in ("label", "event", "actor", "via_app")}
                                 for u in d.get("unknown_label_events") or []]
    text = json.dumps(a, sort_keys=True, separators=(",", ":"))
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def verifier_version() -> str:
    try:
        with open(__file__, "rb") as f:
            digest = sha256_hex(f.read())[:16]
    except OSError:
        digest = "unknown"
    commit = os.environ.get("GITHUB_SHA", "")
    return f"{VERIFIER_VERSION}+sha256.{digest}" + (f" (svc-hello {commit[:12]})" if commit else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", required=True, help="path to the pull_request_target event JSON")
    ap.add_argument("--repo", required=True, help="owner/name of the repository")
    ap.add_argument("--out-dir", required=True, help="where verdict.json and summary.md go")
    ap.add_argument("--controls-repo", default=CONTROLS_REPO)
    ap.add_argument("--class-file", help="read the class file from this path instead of the controls repo (local runs)")
    ap.add_argument("--records-branch", default=os.environ.get("RECORDS_BRANCH") or RECORDS_BRANCH,
                    help="the controls repo's records branch (probe P4: records-p4)")
    args = ap.parse_args(argv)

    with open(args.event) as f:
        payload = json.load(f)
    event = Event.from_payload(args.repo, payload)
    api = GitHub(os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    goproxy = os.environ.get("CLASS_SCOPE_GOPROXY", DEFAULT_GOPROXY)
    deps = Deps(
        api=api,
        proxy=Proxy(goproxy),
        recompute_factory=lambda image: DockerRecompute(image, goproxy=goproxy),
    )
    if args.class_file:
        with open(args.class_file) as f:
            deps.class_file_text = f.read()
        deps.class_file_commit = f"local:{args.class_file}"
    labels = [l["name"] for l in payload["pull_request"].get("labels", [])]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    d = run(event, deps, os.environ.get("M4_FORCE_INCONCLUSIVE", ""), labels, args.controls_repo,
            records_branch=args.records_branch, server=server)
    d["verifier_version"] = verifier_version()
    d["run"] = {k: os.environ.get(k) for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")}

    if d["verdict"] not in ("pass", "fail", "inconclusive"):
        print("class-scope reached no verdict", file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "verdict.json"), "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    packet = render_packet(d, args.repo, server, args.controls_repo)
    with open(os.path.join(args.out_dir, "summary.md"), "w") as f:
        f.write(packet)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(packet)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"verdict={d['verdict']}\nreason_class={d['reason_class'] or ''}\n")
    # Workflow commands are stopped while text the pull request controls is
    # printed (the reasons quote file names and go.mod lines), so no line of
    # it can become an annotation; then the one verdict annotation is written.
    resume = secrets.token_hex(16)
    print(f"::stop-commands::{resume}")
    print(json.dumps({"verdict": d["verdict"], "reason_class": d["reason_class"],
                      "pass_kind": d["pass_kind"], "reasons": d["reasons"]}, indent=2))
    print(f"::{resume}::")
    print(f"::notice title={VERDICT_ANNOTATION_TITLE}::{annotation_message(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
