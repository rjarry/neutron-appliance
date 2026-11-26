#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Robin Jarry
#
# Build the appliance image and its provenance manifest. The tree must be
# clean: the image records HEAD, and building from modified files would make
# that record a lie.

set -eu
set -o pipefail

cd "$(dirname "$0")/.."

IMAGE_NAME=${IMAGE_NAME:-neutron-appliance}
ARCH=${ARCH:-amd64}
DIB_RELEASE=${DIB_RELEASE:-9-stream}
OUTPUT_DIR=${OUTPUT_DIR:-build}
BASE_ELEMENT=centos-minimal

if [ "$DIB_RELEASE" != '9-stream' ]; then
	echo "error: the RHEL 9 baseline needs DIB_RELEASE=9-stream" >&2
	exit 1
fi

if ! command -v disk-image-create >/dev/null 2>&1; then
	echo "error: disk-image-create not found" >&2
	echo "       install it, e.g. pip install diskimage-builder" >&2
	exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
	echo "error: tracked files are modified; commit them before building" >&2
	git status --short
	exit 1
fi

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)
if [ -z "$version" ]; then
	echo "error: cannot read the version from pyproject.toml" >&2
	exit 1
fi
commit=$(git rev-parse HEAD)

mkdir -p "$OUTPUT_DIR"
output=$(cd "$OUTPUT_DIR" && pwd)
manifest=$output/$IMAGE_NAME.manifest.json
rm -f "$manifest"

build=(
	disk-image-create
	-a "$ARCH"
	-o "$OUTPUT_DIR/$IMAGE_NAME"
	-t qcow2
	"$BASE_ELEMENT" vm neutron-appliance
)
build_command="${build[*]}"

export DIB_RELEASE
export DIB_NEUTRON_APPLIANCE_SOURCE="$PWD"
export DIB_NEUTRON_APPLIANCE_VERSION="$version"
export DIB_NEUTRON_APPLIANCE_COMMIT="$commit"
export DIB_NEUTRON_APPLIANCE_BASE_ELEMENT="$BASE_ELEMENT"
export DIB_NEUTRON_APPLIANCE_BASE_RELEASE="$DIB_RELEASE"
export DIB_NEUTRON_APPLIANCE_ARCH="$ARCH"
export DIB_NEUTRON_APPLIANCE_BUILD_COMMAND="$build_command"
export DIB_NEUTRON_APPLIANCE_MANIFEST_PATH="$manifest"
export ELEMENTS_PATH="$PWD/image/elements${ELEMENTS_PATH:+:$ELEMENTS_PATH}"

printf 'building %s: %s, %s, commit %s\n' \
	"$IMAGE_NAME" "$ARCH" "$DIB_RELEASE" "${commit:0:12}"

"${build[@]}"

if [ ! -f "$manifest" ]; then
	echo "error: the build did not write $manifest" >&2
	exit 1
fi

printf 'image:    %s/%s.qcow2\n' "$output" "$IMAGE_NAME"
printf 'manifest: %s\n' "$manifest"
