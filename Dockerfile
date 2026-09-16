# Multi-stage build for svc-hello.
#
# This image is built on a LAPTOP and pushed by hand, once, per the M2 build
# log ("how the svc-hello image gets into the registry ... pushed by hand once
# for M2, recorded as a manual step, with a CI identity deferred"). So the base
# images here come straight from Docker Hub and gcr.io, NOT through the
# Artifact Registry remotes: ADR-0010 governs what the CLUSTER pulls at runtime,
# and at runtime this pod pulls exactly one image — the one we push to
# us-central1-docker.pkg.dev/platform-factory-ref/svc-hello. Everything below is
# baked into that image's layers before it ever reaches the cluster.

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
# golang 1.27.1 rather than the 1.23 the plan first assumed: pgx v5.11.0 (the
# latest v5, verified on GitHub 2026-09-16) declares `go 1.25.0`, so a 1.23
# toolchain refuses to build it. 1.27.1 is the current release (Docker Hub tag
# list read 2026-09-16).
FROM golang:1.27.1-alpine AS builder

# git is needed by the module proxy fallback path; ca-certificates for TLS to
# proxy.golang.org.
RUN apk add --no-cache git ca-certificates

WORKDIR /src

# No Go toolchain is installed on the machine that authored this repo, so go.sum
# cannot be produced outside a container. `go mod tidy` inside the builder is
# what resolves and records the dependency graph. GOFLAGS=-mod=mod lets it write
# go.mod/go.sum in the build context copy (the default -mod=readonly would fail
# the moment tidy wanted to add a line).
ENV GOFLAGS=-mod=mod \
    CGO_ENABLED=0 \
    GOOS=linux

# One COPY, not three, because go.sum is optional: `go.su[m]` is a glob, and a
# glob that matches nothing is only tolerated when some other source in the same
# COPY matched (go.mod always does). If go.sum is committed, tidy verifies
# against it; if it is not, tidy writes it. Either way the build works.
COPY go.mod go.su[m] main.go ./

RUN go mod tidy

# vet before build. It is the cheapest static check Go has and it costs a second
# here instead of a failed deploy later.
RUN go vet ./...

# -trimpath keeps absolute build paths out of the binary; -s -w drop the symbol
# table and DWARF, which is most of the size. Fully static because the runtime
# stage below has no libc.
RUN go build -trimpath -ldflags="-s -w" -o /out/svc-hello .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
# distroless static: no shell, no package manager, nothing but ca-certificates,
# /etc/passwd and the binary. The `nonroot` variant runs as uid 65532, which is
# what k8s/deployment.yaml's runAsNonRoot + runAsUser assert.
FROM gcr.io/distroless/static-debian13:nonroot

COPY --from=builder /out/svc-hello /usr/local/bin/svc-hello

# Documentation only — Kubernetes routes by the Service's targetPort, not by
# this. Kept so `docker run -P` does the obvious thing.
EXPOSE 8080

USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/svc-hello"]
