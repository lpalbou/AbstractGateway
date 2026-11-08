#!/bin/sh
# Installs AbstractGateway into an image (Dockerfile and Dockerfile.nvidia run it at build time).
#
#   ABSTRACTGATEWAY_INSTALL_MODE   local (the copied source tree) or pypi (the default)
#   ABSTRACTGATEWAY_VERSION        the PyPI version to install (pypi mode; empty = the latest)
#   ABSTRACTGATEWAY_EXTRAS         extras, e.g. gpu (empty = none)
#   ABSTRACTGATEWAY_PIP_RETRY_DELAYS  seconds to wait before each retry of a pinned PyPI install
#
# A pinned PyPI install is retried with a bounded backoff (by default 5 retries over about 5 minutes):
# the release job builds the image right after publishing that version, and the PyPI index can still
# answer "No matching distribution found for abstractgateway==X.Y.Z" for a while (v0.20.2, release run
# 42339708494), although the runner-side wait already saw the version. Any other failure fails the
# same way once the retries are spent, so a real error still fails the build, only later.
set -eu

mode="${ABSTRACTGATEWAY_INSTALL_MODE:-pypi}"
version="${ABSTRACTGATEWAY_VERSION:-}"
extras="${ABSTRACTGATEWAY_EXTRAS:-}"
delays="${ABSTRACTGATEWAY_PIP_RETRY_DELAYS:-15 30 60 90 120}"

suffix=""
if [ -n "$extras" ]; then
  suffix="[${extras}]"
fi

if [ "$mode" = "local" ]; then
  exec python -m pip install "/tmp/abstractgateway-src${suffix}"
fi
if [ "$mode" != "pypi" ]; then
  echo "install-gateway: unknown ABSTRACTGATEWAY_INSTALL_MODE '${mode}' (local or pypi)" >&2
  exit 2
fi
if [ -z "$version" ]; then
  exec python -m pip install "abstractgateway${suffix}"
fi

spec="abstractgateway${suffix}==${version}"
attempt=1
for delay in $delays; do
  if python -m pip install "$spec"; then
    exit 0
  fi
  echo "install-gateway: ${spec} not installable yet (attempt ${attempt}); retrying in ${delay}s" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done
if python -m pip install "$spec"; then
  exit 0
fi
echo "install-gateway: could not install ${spec} after ${attempt} attempts" >&2
exit 1
