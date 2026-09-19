# svc-hello

The canonical example service for the Platform Factory. It is deliberately
boring — an HTTP server with three routes and one table — because what it is
here to demonstrate is not the code. It is the road the code travels on.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at
[https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

Platform Factory was designed and written by **Ronak Patel**
([thecloudgeek LLC](https://github.com/thecloudgeek)). Licensed Apache-2.0 —
the attribution to keep is in [NOTICE](NOTICE), and
[CITATION.cff](CITATION.cff) says how to cite it.

This repo is built out in **M2**.

## What it demonstrates

Three things, and it is worth being precise about which is which.

1. **A service repo carries its own infrastructure request.** `k8s/` holds the
   `Deployment` and the `Service` — and `database.yaml`, a five-field
   `Database` claim. The team asks for a Postgres database in the same pull
   request as the code that uses it, reviewed by the same people, and nobody
   files a ticket.

2. **The application never holds a database password.** Not a rotated one, not
   a mounted one, not one in a Secret it can read. See below.

3. **The platform gave the team the namespace, the quota, the registry
   repository, the Argo CD project and the Kubernetes identity, from one line
   in another repo.** A `System` in the `systems` repo names `svc-hello` and its
   owning team; everything above appears because of it. Nothing in this repo
   creates a `Namespace` or a `ServiceAccount` — those are the platform's. The
   `AppProject` would reject a `Namespace` outright, because it denies every
   cluster-scoped kind. It would *accept* a `ServiceAccount`, deliberately —
   but a tenant-made one is inert, because the project blocks
   `rbac.authorization.k8s.io`, so nothing can ever be bound to it.

## How it reaches its database with no password

This is ADR-0013, in plain words.

The usual way an app gets to a database is a username and a password, kept in a
secret store, copied into the cluster, mounted into the pod, and rotated by
somebody or something on a schedule. Every one of those steps is a place the
credential can leak, and "rotated" is usually a promise rather than a mechanism.

Here there is no password to leak, because the identity the database trusts is
not a string — it is the pod.

```
  ┌─────────────────── pod: svc-hello ────────────────────┐
  │                                                        │
  │  svc-hello              cloud-sql-proxy (sidecar)      │
  │  ─────────              ─────────────────────────      │
  │  connects to ─────────► 127.0.0.1:5432                 │
  │  user: svc-hello@…iam            │                     │
  │  password: (none)                │ asks the node's     │
  │                                  │ metadata server for │
  │                                  │ a token, as THIS    │
  │                                  │ pod's identity      │
  │                                  ▼                     │
  │                        short-lived IAM token           │
  └──────────────────────────────────┼─────────────────────┘
                                     │ mutual TLS, private IP
                                     ▼
                       Cloud SQL — checks IAM, not a password
```

Reading it from the top:

- The pod runs as the Kubernetes service account `svc-hello`. GKE Workload
  Identity ties that service account to the Google service account
  `svc-hello@platform-factory-ref.iam.gserviceaccount.com`. That tie is the only
  credential in the picture, and it is a property of the cluster, not a file.
- The **Cloud SQL Auth Proxy sidecar** uses that identity to mint a token good
  for about an hour, and opens a mutually-authenticated TLS connection to the
  instance over its private IP. The instance has no public IP at all.
- The **app** connects to `127.0.0.1:5432` — inside its own pod — with a
  username and no password whatsoever. Look at `newPool` in `main.go`: the
  connection string has no `password` key in it.
- On the database side, the `Database` composition created a Postgres user of
  type `CLOUD_IAM_SERVICE_ACCOUNT` named `svc-hello@platform-factory-ref.iam`
  (Cloud SQL's rule: the service account email with `.gserviceaccount.com`
  removed, because Postgres usernames have a length limit).

"Rotation" is therefore not a process anyone runs. The only runtime credential
expires in about an hour and is replaced automatically, which is what ADR-0003's
"managed rotation" was always trying to buy.

**This ran for real on 2026-09-16, and it works — but the Postgres user it
depends on could not be created by the platform.** At 17:13:28 a probe pod in
this namespace did `POST /notes` then `GET /notes` and got the row back, with
`/healthz` answering 200 and no Secret mounted anywhere in the application pod.
So the mechanism above is confirmed end to end [C].

What is broken sits one step earlier. provider-upjet-gcp v3.0.0 cannot create a
**passwordless** Cloud SQL user — the create path panics with `async create
failed: recovered from panic: not a string` (upstream
crossplane-contrib/provider-upjet-gcp issue #1000, open; root-caused by a
maintainer on 2026-09-14: v3.0.0 strips `password_wo` from the runtime schema,
so every passwordless `sql.User` create panics). A
`CLOUD_IAM_SERVICE_ACCOUNT` user is passwordless by definition, which means the
one kind this whole design needs is the one kind the provider cannot make at
this version. Nor can the Composition dodge it by supplying a password: Cloud
SQL answers `HTTPError 400: Invalid request: Cloud IAM password cannot be set
in the database.`

Until a fixed provider ships, **each database costs one manual command**, run
out of band so the provider's `Observe` path adopts the user it did not create:

```bash
gcloud sql users create svc-hello@platform-factory-ref.iam \
  --instance=svc-hello-main --type=CLOUD_IAM_SERVICE_ACCOUNT
```

The symptom that says you need it is in this service's own log:
`FATAL: password authentication failed for user "svc-hello@platform-factory-ref.iam"`.
On the first run the managed resource sat failing from 17:08:17 until that
command at 17:10:44; the user was adopted `Ready` at 17:11:22, the `GRANT` Job
ran 17:11:22→17:11:42, and this pod went Ready at 17:11:51. None of that
changes the credential story — the app still holds no password — but it does
mean the paved road is not yet hands-off for a database, and it is recorded as
a manual intervention every time.

**The one password that does exist** is the built-in `postgres` account's. The
platform generates it, stores it as the Secret `main-admin` in this namespace,
and uses it exactly once per grant change — a `Job` that runs `psql` and grants
the IAM user its privileges on the database, because a freshly-created Cloud SQL
IAM user starts with none at all. The team can read that Secret; it is their
database. Nothing on the golden path uses it. It is break-glass, not runtime.

**The cost to the team is the sidecar block** in `k8s/deployment.yaml`. It is
not injected — a `Deployment` that omits it simply cannot reach the database.
That is a deliberate choice (ADR-0013): automatic injection via a mutating
webhook interacts badly with Argo CD's drift detection and is its own change.

## Routes

| Route          | What it does                                                         |
|----------------|----------------------------------------------------------------------|
| `GET /`        | `hello from svc-hello` plus the pod hostname. No database.           |
| `GET /healthz` | Readiness. With `DB_ENABLED=true`, runs `SELECT 1` through the proxy. |
| `POST /notes`  | Request body is the note text. Inserts one row.                      |
| `GET /notes`   | Lists the most recent 100 rows.                                      |

Configuration is five environment variables, all set in `k8s/deployment.yaml`:
`DB_ENABLED`, `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_NAME` (plus `PORT`). With
`DB_ENABLED` unset the service runs with no database and `/notes` returns 503.

### Deploying before the database exists

`k8s/deployment.yaml` ships with `DB_ENABLED=true`, and the service is deployed
before its `Database` claim is Ready. That window is safe but not invisible, and
it is worth knowing its shape before you watch it:

- the pod starts and stays up — the connect-and-create-table loop retries in the
  background with backoff instead of crash-looping, because "the IAM user exists
  but has no privileges yet" is a normal state until the Composition's `GRANT`
  job runs;
- `/healthz` returns 503 for that whole window, so the pod is not Ready and the
  `Service` has no endpoints;
- Argo CD therefore shows the `svc-hello` Application **Progressing**, not
  Healthy, until the database is usable.

The `Deployment` sets `progressDeadlineSeconds: 3600` for exactly this reason. A
cold Cloud SQL instance takes longer to provision than the 600-second default,
and past that deadline Kubernetes would mark the `Deployment`
`ProgressDeadlineExceeded` and Argo CD would report it **Degraded** — a red
Application caused by nothing but waiting. This is the expected shape of the
C-07(a) window, not a failure.

## Building and pushing the image — by hand, once

M2 pushes this image from a laptop. That is a recorded decision, not an
oversight: the build log settles it as *"pushed by hand once for M2, recorded as
a manual step, with a CI identity deferred to the M4 change class that needs
it."* So there is no workflow in this repo that builds or pushes, and no service
account key anywhere.

```bash
make login       # once per laptop: gcloud auth configure-docker us-central1-docker.pkg.dev

docker buildx build --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/platform-factory-ref/svc-hello/svc-hello:$(git rev-parse --short HEAD) \
  --push .

make set-image   # rewrites the image line in k8s/deployment.yaml to that sha
git commit -am "svc-hello: pin image to $(git rev-parse --short HEAD)"
git push
```

That last push is the deploy: Argo CD syncs `k8s/` from `main`.

**`docker buildx`, and `--platform linux/amd64`, and a builder stage that runs
natively — all three, and none of them is a preference.** This was the one M2
step an authoring agent reported as "docker build succeeded" when it had not
succeeded, so it is written out here: the two failures that actually appeared
on 2026-09-16, plus the standing reason the platform is named at all.

The reason the platform is named: the GKE node pool is **amd64** and the laptop
that pushes may not be, so an arm64 image would land in the registry perfectly
happily and then fail on the node with `exec format error`. That was not hit on
2026-09-16, because the platform was named.

The two failures, in the order they appeared:

- The **legacy builder cannot cross-build at all.** It loses the requested
  platform at the first intermediate layer, so `--platform` on it is a
  suggestion rather than an instruction. `buildx` (BuildKit) is what honours
  it, and it is also what defines the `$BUILDPLATFORM`, `TARGETOS` and
  `TARGETARCH` arguments the Dockerfile reads.
- **Emulating the compiler does not work either.** With `buildx` alone, the
  amd64 Go toolchain runs under CPU emulation on Apple silicon and Go's
  runtime panics inside the net resolver during `go mod tidy` — a goroutine
  dump from `net.(*Resolver).lookupIPAddr`, exit 2.

The fix is in the `Dockerfile`, not in the command: the builder stage is
declared `FROM --platform=$BUILDPLATFORM`, so the Go toolchain runs natively on
whatever machine is building, and Go cross-compiles the binary for
`TARGETOS`/`TARGETARCH`. Cross-compiling is the thing Go is good at; emulating
a compiler is not. The runtime stage is still distroless static for the target
platform.

> **The `Makefile` has not caught up.** `make build` (and therefore `make
> push`) still calls `docker build`, so it only does the right thing on a
> Docker installation where BuildKit is the default builder. Use the explicit
> `docker buildx build` above until the target is changed; `make login` and
> `make set-image` are unaffected.

The manifest ships with the tag `REPLACE_ME` on purpose. A checkout that has
never been pushed fails visibly at the registry instead of quietly running
whatever `:latest` happens to be, and the PR check refuses to let `REPLACE_ME`
reach `main`.

The image goes to
`us-central1-docker.pkg.dev/platform-factory-ref/svc-hello/svc-hello:<sha>` —
the Artifact Registry repository the `System` composition created, named after
the System.

## Running it locally

```bash
make build                     # same caveat as above: use docker buildx if make build fails
make run                       # no database; GET localhost:8080/ and /healthz
```

To exercise the database path without any cloud, run a throwaway Postgres and
point the service at it. The proxy is not in the picture, which is exactly the
point: the app's side of the connection is identical either way.

```bash
docker network create svc-hello-local
docker run -d --name pg --network svc-hello-local \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=app -e POSTGRES_USER=dev \
  postgres:16-alpine
docker run --rm --network svc-hello-local -p 8080:8080 \
  -e DB_ENABLED=true -e DB_HOST=pg -e DB_USER=dev -e DB_NAME=app \
  us-central1-docker.pkg.dev/platform-factory-ref/svc-hello/svc-hello:$(git rev-parse --short HEAD)
```

## C-07 — the test this service exists to run

C-07 is the claim *"guardrails replace review for databases."* It has three
parts. This repo is the subject of (a) and (c).

### (a) PR → usable database, timed

1. Merge a PR that adds `k8s/database.yaml` to `main`.
   **Record the merge timestamp** from the GitHub API.

2. Argo CD syncs it; Crossplane creates the Cloud SQL instance, the `app`
   database, the IAM user, the admin Secret, and runs the `GRANT` job.
   **Record the timestamp on the `Database` XR's `Ready` condition:**

   ```bash
   kubectl -n svc-hello get database main \
     -o jsonpath='{.status.conditions[?(@.type=="Ready")]}{"\n"}'
   ```

3. **Record when the app's readiness probe goes green** — this is the second of
   ADR-0013's two timestamps, and it is the one that means *usable*, because
   `/healthz` only answers 200 once the service has created its table through
   the IAM identity.

   ```bash
   kubectl -n svc-hello get pods -l app.kubernetes.io/name=svc-hello -w
   ```

4. Prove it end to end. Check first that no Secret is mounted in this pod, so
   the result cannot be explained any other way.

   ```bash
   kubectl -n svc-hello get pod -l app.kubernetes.io/name=svc-hello \
     -o jsonpath='{.items[0].spec.volumes}{"\n"}'      # expect: no secret volumes

   kubectl -n svc-hello port-forward svc/svc-hello 8080:80 &

   curl -s localhost:8080/healthz
   curl -s -X POST --data-binary 'the paved road works' localhost:8080/notes
   curl -s localhost:8080/notes
   ```

   A row comes back that was written by an identity which was never given a
   password.

**Run on 2026-09-16.** The PR had merged at 16:31:24 while the cluster was
still down, so the honest clock starts when Argo CD applied the claim: claim
created 16:54:06, instance `svc-hello-main` (db-f1-micro, private IP
`10.60.0.3`, no public IP, `cloudsql.iam_authentication` on, deletion
protection on) `RUNNABLE` at roughly 17:08, IAM user created by hand at
17:10:44 (see the provider bug above), `GRANT` Job 17:11:22→17:11:42, pod Ready
17:11:51, `Database` XR Ready 17:12:41. **Claim → usable: 17m45s, including one
manual step**, of which 17:08:17→17:10:44 is the managed resource sitting
failed while waiting for a human to run that command. Step 4's
proof ran at 17:13:28 and returned the row.

One thing that bit on the way and is worth knowing before you copy step 4: the
namespace's `ResourceQuota` rejects a probe pod that declares no resources —
`pods "c07-probe" is forbidden: failed quota: svc-hello: must specify
limits.cpu … requests.memory`. The quota is doing its job; give the probe pod
requests and limits.

### (b) Denials — the fixtures, and what each one demonstrates

`docs/c07-denials/` holds three deliberately-bad `Database` claims. They are
never applied from `k8s/` — the test applies them by hand and records what the
developer sees at each surface. Each one demonstrates a different *kind* of
rule, which is the point: ADR-0014 says the schema denies first because a
schema can say more than people expect.

| Fixture | What is wrong | What it demonstrates |
|---|---|---|
| `wrong-region.yaml` | `region: europe-west1` | a plain `enum` — the denial lists the regions that exist |
| `oversized.yaml` | `size: XL` | a second `enum` — the denial lists the sizes that exist |
| `cel-size-tier.yaml` | `size: L` with `tier: standard` | a **CEL rule** across two fields, which no enum can express, returning the platform's own sentence rather than a type error |

Two of the three surfaces were recorded on 2026-09-16. The API server, verbatim:

```
The Database "denied-region" is invalid: * spec.region: Unsupported value: "europe-west1": supported values: "us-central1", "us-east1"
The Database "denied-size" is invalid: * spec.size: Unsupported value: "XL": supported values: "S", "M", "L"
The Database "denied-cel" is invalid: spec: Invalid value: size L is only available to a critical-tier database. Set tier: critical if this database really is business critical, otherwise use size M.
```

(The two enum denials also print `* <nil>: Invalid value: null: some validation
rules were not checked because the object was invalid; correct the existing
errors to complete validation` — the CEL rules are skipped once the object has
already failed structurally.)

The CLI surface, offline and with no cluster, is the same three sentences:

```
crossplane resource validate <xrd.yaml> docs/c07-denials/
→ Total 3 resources: 0 missing schemas, 0 success cases, 3 failure cases
```

prefixed `[x] schema validation error …` and `[x] CEL validation error …`
(crossplane v2.5.0).

The Argo CD surface was run on 2026-09-17 by merging `wrong-region.yaml` into
`k8s/` on `main`, deliberately skipping CI (PR #5, reverted by #6). The
Application went `OutOfSync` while staying `Healthy`, the claim showed
`SyncFailed`, the sync kept retrying, and the running service was untouched:

```
one or more synchronization tasks completed unsuccessfully, reason: Database.platform.thecloudgeek.io "denied-region" is invalid: [spec.region: Unsupported value: "europe-west1": supported values: "us-central1", "us-east1", …]
```

Only that one claim went through Argo CD; the oversized and CEL claims were
recorded at the CLI and the API server.

The third denial in C-07(b) is not a schema denial at all and lives in
`platform-config`: Kyverno's reality gate, tested by applying a raw
`DatabaseInstance` by hand in this namespace. After the fix that landed during
the run, it answers:

```
admission webhook "validate.kyverno.svc-fail" denied the request: resource DatabaseInstance/svc-hello/raw-observe-only was blocked due to the following policies deny-raw-managed-resources: no-raw-managed-resources-in-tenant-namespaces: Cloud resources are not created by hand here. Ask for what you need with a platform.thecloudgeek.io claim — a Database, for example — …
```

**Before** that fix the identical test was *admitted* and created a real Cloud
SQL instance. A schema failure names the field path; a Kyverno failure names
the policy and the rule; and a policy whose webhook matches nothing names
nothing at all. That last case is the one worth remembering.

### (c) Delete the claim, the database survives — run 2026-09-17, and it held

Delete `k8s/database.yaml`, merge, let Argo prune the claim, then confirm the
Cloud SQL instance is still there. ADR-0015 makes the instance, the database and
the registry repository durable: the managed resources carry
`managementPolicies: [Observe, Create, Update, LateInitialize]`, with no
`Delete`, so Crossplane forgets them rather than destroying them. Re-adding the
claim adopts the same instance back, by its deterministic external name
`svc-hello-main`.

What happened (PRs #3 and #4): the claim was pruned 23 seconds after the merge
and every composed object left the namespace — but the Cloud SQL instance
stayed `RUNNABLE` with its original creation time, the `app` database and the
IAM user stayed, and this service's pod never stopped serving, because nothing
it depends on had changed. When the claim came back, the instance was adopted
rather than created: Ready 66 seconds after the claim appeared, against about
fourteen minutes for a fresh instance, and the row written the day before read
back through `/notes`. The IAM user was adopted too, so the provider bug above
was never touched — it bites only when a user has to be *created*. The same
row then survived a full cluster teardown, a `park`, and a rebuild.

The *deletion protection* half had been exercised by accident the day before,
on 2026-09-16, on the raw instance created by hand while the Kyverno gate was
blind: removing it needed both locks cleared deliberately — the managed
object's `deletionProtection` patched to false **and** `gcloud sql instances
patch --no-deletion-protection` — after which the provider deleted it. Both
locks held until someone deliberately removed them, which is the property
ADR-0015 was after, arrived at from the wrong direction.

## What's in this repo

```
main.go                         the service: three routes, pgx, no framework
go.mod / go.sum                 one dependency (pgx v5)
Dockerfile                      multi-stage; native builder cross-compiles, distroless static runtime, nonroot
Makefile                        login / build / push / set-image — the hand-push path
                                (build still calls `docker build`; see the build section)
k8s/deployment.yaml             app container + Cloud SQL Auth Proxy native sidecar
k8s/service.yaml                ClusterIP 80 → 8080
k8s/database.yaml               the Database claim: main, POSTGRES_16, us-central1, S
docs/c07-denials/               three claims that must be denied — one per rule kind
.github/workflows/validate.yml  YAML parse, go vet/build, best-effort XRD check
```

`k8s/` is plain manifests, no Kustomize. Argo CD syncs the directory as it is,
and a reader should be able to see what will be applied by reading the files.

## Status

**Status:** built in M2 and **running as of 2026-09-16**. The image is pushed
by hand. The manifest pins `:3870e4c`, the commit the service code was authored
in — but no image could be built at all until the Dockerfile fix in `8225853`,
so the image that is actually running was built from a later tree and tagged
with that earlier sha. Re-pinning it is a follow-up. The
service is deployed in the `svc-hello` namespace with its Cloud SQL instance
`svc-hello-main`, reachable with no password, and C-07(a) is recorded above.

Two things about this repo are not settled. The `Database` claim still needs
one manual `gcloud sql users create` per database while
provider-upjet-gcp #1000 is open, so the paved road is not hands-off here yet.
And **C-07(c) has not been run** — the claim has never been deleted and
re-added, so "delete the claim, the database survives, re-adding adopts it
back" is still an assertion in this repo rather than a result.

The System that owns this service was moved from the `payments` team to
`checkout` on 2026-09-16 as the C-06 test. Nothing in this repo changed for
that, which is the point — the namespace, the registry repository, the image
paths and this service's own identity all belong to the System, not to the
team.
