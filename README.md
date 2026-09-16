# svc-hello

The canonical example service for the Platform Factory. It is deliberately
boring — an HTTP server with three routes and one table — because what it is
here to demonstrate is not the code. It is the road the code travels on.

## Part of the Platform Factory

This repo is one of seven that make up the reference implementation of the
**Platform Factory** pattern. The design seed — pattern docs, ADRs, and the
build plan — lives at
[https://github.com/thecloudgeek/platform-factory](https://github.com/thecloudgeek/platform-factory).

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
make push        # builds linux/amd64, tags it with the current commit sha, pushes
make set-image   # rewrites the image line in k8s/deployment.yaml to that sha
git commit -am "svc-hello: pin image to $(git rev-parse --short HEAD)"
git push
```

That last push is the deploy: Argo CD syncs `k8s/` from `main`.

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
make build
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

### (b) Denials — recorded from the other two repos

Change `spec.size` to `L` while `spec.tier` is `standard`, or `spec.region` to
something outside the enum, and record the message verbatim from the PR check,
the API server, and the Argo CD UI. A schema failure names the field path; a
Kyverno failure names the policy and the rule. That difference is the ADR-0014
"schema denies first" distinction made visible.

### (c) Delete the claim, the database survives

Delete `k8s/database.yaml`, merge, let Argo prune the claim, then confirm the
Cloud SQL instance is still there. ADR-0015 makes the instance, the database and
the registry repository durable: the managed resources carry
`managementPolicies: [Observe, Create, Update, LateInitialize]`, with no
`Delete`, so Crossplane forgets them rather than destroying them. Re-adding the
claim adopts the same instance back, by its deterministic external name
`svc-hello-main`.

## What's in this repo

```
main.go                         the service: three routes, pgx, no framework
go.mod / go.sum                 one dependency (pgx v5)
Dockerfile                      multi-stage; distroless static runtime, nonroot
Makefile                        build / push / set-image — the hand-push path
k8s/deployment.yaml             app container + Cloud SQL Auth Proxy native sidecar
k8s/service.yaml                ClusterIP 80 → 8080
k8s/database.yaml               the Database claim: main, POSTGRES_16, us-central1, S
.github/workflows/validate.yml  YAML parse, go vet/build, best-effort XRD check
```

`k8s/` is plain manifests, no Kustomize. Argo CD syncs the directory as it is,
and a reader should be able to see what will be applied by reading the files.

## Status

**Status:** built in M2. The image is pushed by hand; the tag in
`k8s/deployment.yaml` is the commit it was built from.
