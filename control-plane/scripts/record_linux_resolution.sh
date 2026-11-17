#!/bin/sh
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Records what the images' install resolves to on Linux, per architecture.
#
#     scripts/record_linux_resolution.sh            (from the repository root; needs Docker)
#
# The images are built on linux/amd64 and linux/arm64 from python:3.13-slim
# (`Dockerfile`, `enterprise/Dockerfile`). Some dependencies are there only on
# Linux (SQLAlchemy needs greenlet on x86_64 and aarch64, not on macOS arm64),
# so a lock frozen on a developer's Mac misses them and the image's licence
# step refuses the build. This script installs the lock in that image, for
# each architecture, exactly as the Dockerfiles do, and writes `pip freeze
# --all` to
#
#     tests/fixtures/linux_resolution/<arch>.txt              the Community image
#     enterprise/tests/fixtures/linux_resolution/<arch>.txt   the Enterprise image
#                                                             (where enterprise/ exists)
#
# `tests/test_third_party_licenses.py` (and its Enterprise half) hold each lock
# equal to these recordings. Run it after every change of a lock.
# =============================================================================
set -eu

cd "$(dirname "$0")/.."
IMAGE=python:3.13-slim
EDITIONS=community
[ -d enterprise ] && EDITIONS="community enterprise"

for arch in amd64 arm64; do
  files="pyproject.toml README.md constraints.txt app main.py"
  [ -d enterprise ] && files="$files enterprise/pyproject.toml enterprise/README.md enterprise/constraints.txt enterprise/brutor_enterprise enterprise/main.py"
  # shellcheck disable=SC2086
  out=$(tar --exclude='__pycache__' -cf - $files | docker run --rm -i --platform "linux/$arch" "$IMAGE" sh -c "
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq gcc libpq-dev >/dev/null 2>&1
mkdir /w && cd /w && tar -xf -
q='--quiet --no-cache-dir --root-user-action=ignore --disable-pip-version-check'
pip install \$q -c constraints.txt --upgrade pip setuptools wheel
pip install \$q -e . -c constraints.txt --no-binary psycopg2
echo '### community'; pip freeze --all --exclude-editable
if [ -d enterprise ]; then
  pip install \$q -c constraints.txt -c enterprise/constraints.txt --upgrade pip setuptools wheel
  pip install \$q -e . -c constraints.txt -c enterprise/constraints.txt --no-binary psycopg2
  pip install \$q -e ./enterprise -c constraints.txt -c enterprise/constraints.txt
  echo '### enterprise'; pip freeze --all --exclude-editable
fi
echo \"### python \$(python -c 'import platform; print(platform.python_version(), platform.machine())')\"
")
  python_line=$(printf '%s\n' "$out" | sed -n 's/^### python //p')
  for edition in $EDITIONS; do
    if [ "$edition" = community ]; then dir=tests/fixtures/linux_resolution; else dir=enterprise/tests/fixtures/linux_resolution; fi
    mkdir -p "$dir"
    {
      echo "# What the $edition image's install resolves to on linux/$arch ($IMAGE, Python $python_line):"
      echo "# pip freeze --all, recorded by scripts/record_linux_resolution.sh. Do not edit."
      printf '%s\n' "$out" | awk -v e="### $edition" '$0==e{f=1;next} /^###/{f=0} f'
    } > "$dir/$arch.txt"
    echo "wrote $dir/$arch.txt"
  done
done
