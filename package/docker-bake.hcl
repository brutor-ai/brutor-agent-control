# The Community images of Brutor Agent Control: what each is built from, the
# name it is published under, its tags and the labels only a build knows. This
# is the one place the image names are written. The root workflow
# publish-images.yml publishes through this file, and each component's
# workflow builds its image through it.
#
# Run from the repository root (the build contexts are relative to it):
#
#   docker buildx bake -f package/docker-bake.hcl --load                # every image, for this machine
#   docker buildx bake -f package/docker-bake.hcl core-gateway --load   # one image
#   docker buildx bake -f package/docker-bake.hcl --print               # what would be built, as JSON
#
# The variables below are read from the environment of the same name, for
# example VERSION=1.4.0 REVISION=$(git rev-parse HEAD). Each Dockerfile
# carries the labels that never change between builds (title, description,
# licence, source, vendor, edition).
#
# To run the images you built, start deploy/compose/ with its build override
# instead: it builds every service from its component directory.

variable "REGISTRY" {
  default = "ghcr.io"
}

# The version the images are tagged and labelled with (a release tag without
# its "v").
variable "VERSION" {
  default = "dev"
}

# The commit the images are built from.
variable "REVISION" {
  default = ""
}

# Appended to every tag. publish-images.yml builds each architecture on its
# own runner (-amd64, -arm64) and then writes one manifest over both.
variable "TAG_SUFFIX" {
  default = ""
}

function "tags" {
  params = [repository]
  result = ["${REGISTRY}/${repository}:${VERSION}${TAG_SUFFIX}"]
}

group "default" {
  targets = ["core-gateway", "control-plane", "admin-console", "skill-runner", "witness"]
}

# What every Community image has in common.
target "community" {
  dockerfile = "Dockerfile"
  labels = {
    "org.opencontainers.image.version" = "${VERSION}"
    "org.opencontainers.image.revision" = "${REVISION}"
  }
}

target "core-gateway" {
  inherits = ["community"]
  context = "core-gateway"
  tags = tags("brutor-ai/brutor-agent-control-core-gateway")
}

target "control-plane" {
  inherits = ["community"]
  context = "control-plane"
  tags = tags("brutor-ai/brutor-agent-control-control-plane")
  # Read by /health and recorded in the database on every start.
  args = {
    BRUTOR_VERSION = "${VERSION}"
  }
}

target "admin-console" {
  inherits = ["community"]
  context = "admin-console"
  tags = tags("brutor-ai/brutor-agent-control-admin-console")
}

target "skill-runner" {
  inherits = ["community"]
  context = "skill-runner"
  tags = tags("brutor-ai/brutor-agent-control-skill-runner")
}

target "witness" {
  inherits = ["community"]
  context = "witness"
  tags = tags("brutor-ai/brutor-agent-control-witness")
}
