# svc-hello — build and hand-push the image.
#
# M2 pushes this image BY HAND, once, from a laptop. That is a recorded
# decision, not an omission: the M2 build log settles "how the svc-hello image
# gets into the registry" as "pushed by hand once for M2, recorded as a manual
# step, with a CI identity deferred to the M4 change class that needs it".
# So there is no CI workflow here that builds or pushes, and no service account
# key anywhere.
#
# Usual sequence:
#
#   make login          # once per laptop
#   make push           # build + push, tagged with the current commit
#   make set-image      # rewrite k8s/deployment.yaml to that tag
#   git commit -am 'svc-hello: pin image to <sha>' && git push
#
# Argo CD syncs k8s/ from main, so the last step is the deploy.

# ADR-0010's image plane: this repository is the System's own Artifact Registry
# repository, created by the System Composition and named after the System.
REGION   ?= us-central1
PROJECT  ?= platform-factory-ref
REGISTRY ?= $(REGION)-docker.pkg.dev/$(PROJECT)
REPO     ?= svc-hello
IMAGE    ?= $(REGISTRY)/$(REPO)/svc-hello

# The tag is the commit being built. Never "latest": the deployment manifest
# names an exact tag so that what is running is always traceable to a commit,
# and so a restarting pod cannot silently pick up different bytes.
TAG ?= $(shell git rev-parse --short HEAD)

# linux/amd64 explicitly. The GKE node pool is amd64 and the laptop may not be;
# without this, a build on Apple silicon produces an arm64 image that lands in
# the registry and fails on the node with "exec format error".
PLATFORM ?= linux/amd64

DEPLOYMENT := k8s/deployment.yaml

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

.PHONY: login
login: ## Teach docker how to authenticate to Artifact Registry (once per laptop)
	gcloud auth configure-docker $(REGION)-docker.pkg.dev

.PHONY: build
build: ## Build the image locally, tagged with the current commit
	docker build --platform $(PLATFORM) -t $(IMAGE):$(TAG) .

.PHONY: push
push: build ## Build and push to Artifact Registry
	docker push $(IMAGE):$(TAG)
	@echo
	@echo "pushed $(IMAGE):$(TAG)"
	@echo "next: make set-image, then commit $(DEPLOYMENT)"

.PHONY: set-image
set-image: ## Rewrite the image tag in k8s/deployment.yaml to the current commit
	@# The manifest ships with the placeholder REPLACE_ME so that nobody can
	@# mistake an unpushed checkout for a deployable one. This target is the
	@# documented way it gets replaced — and it matches an existing sha too, so
	@# it is safe to run again after the next push.
	@# '#' as the sed delimiter, so the slashes in the image path need no
	@# escaping. Only the matched span is replaced, so the YAML indentation in
	@# front of "image:" survives untouched.
	@sed -i.bak -E 's#(image: $(IMAGE):).*#\1$(TAG)#' $(DEPLOYMENT)
	@rm -f $(DEPLOYMENT).bak
	@grep -n 'image: $(IMAGE)' $(DEPLOYMENT)

.PHONY: run
run: ## Run the image locally with no database (DB_ENABLED unset)
	docker run --rm -p 8080:8080 $(IMAGE):$(TAG)

.PHONY: clean
clean: ## Remove the locally built image
	-docker rmi $(IMAGE):$(TAG)
