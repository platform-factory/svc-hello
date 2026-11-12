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
# golang 1.27.1 rather than the 1.23 the plan first assumed: in M2, pgx v5.11.0
# declared `go 1.25.0`, so a 1.23 toolchain refused to build it, and 1.27.1 was
# the current release (Docker Hub tag list read 2026-09-16). The image is pinned
# by its multi-platform index digest (as resolved on 2026-11-11), and the
# go-test job in .github/workflows/validate.yml runs in the same image at the
# same digest, so the toolchain that tests a change is the one that builds the
# image. Change the two together.
#
# --platform=$BUILDPLATFORM: the builder stage runs NATIVELY on whatever
# machine runs the build, and Go cross-compiles for the target below. Without
# this, `docker build --platform linux/amd64` on an Apple-silicon laptop runs
# the amd64 Go toolchain under CPU emulation, and Go's runtime panics inside
# the net resolver during `go mod tidy` (observed 2026-09-16 on the first M2
# image build: a goroutine dump from net.(*Resolver).lookupIPAddr, exit 2).
# The build no longer runs tidy; `go mod download` also resolves names, so the
# same failure would be expected there (not tried).
# Cross-compiling is what Go is good at; emulating a compiler is not.
FROM --platform=$BUILDPLATFORM golang:1.27.1-alpine3.24@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS builder

# git is needed by the module proxy fallback path; ca-certificates for TLS to
# proxy.golang.org.
RUN apk add --no-cache git ca-certificates

WORKDIR /src

# go.mod and go.sum are the reviewed inputs, and the image must be built from
# exactly them. GOFLAGS=-mod=readonly makes the build fail when either is out of
# step with the code, instead of rewriting them; go.sum is copied by name, so
# the build fails without it; and `go mod tidy` is not run here. (Until M4 the
# build copied go.sum as optional and ran tidy, so the checksums that shipped
# were whatever tidy wrote, not the committed ones.) A stale go.sum fails at
# `go vet` below with "missing go.sum entry"; a tampered one fails at
# `go mod download` with a checksum mismatch. CI's go-test job runs
# `go mod tidy -diff`, so a pull request that leaves either file untidy fails
# before it merges.
#
# TARGETOS/TARGETARCH are set by BuildKit from --platform (linux/amd64 for the
# GKE node pool, see the Makefile); the binary is built for the target while
# the toolchain runs on the build host.
ARG TARGETOS
ARG TARGETARCH
ENV GOFLAGS=-mod=readonly \
    CGO_ENABLED=0 \
    GOOS=${TARGETOS:-linux} \
    GOARCH=${TARGETARCH:-amd64}

# The module files first, in their own layer, so a change to main.go alone
# reuses the downloaded modules.
COPY go.mod go.sum ./
RUN go mod download && go mod verify

COPY main.go ./

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
