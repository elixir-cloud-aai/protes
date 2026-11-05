#!/usr/bin/env bash
# Installs TESK from its chart, at the commit `TESK_COMMIT` of its repository, in
# the k3s cluster of `compose.yaml` (it must be up, with `COMPOSE_FILE` set as for
# `up`), and waits until TESK serves its API. Run it from the repository root;
# needs git, Helm and docker compose.
set -euo pipefail

# The head of the default branch when this was last updated
commit="${TESK_COMMIT:-93fce503e899f3410bf1849bf39f3e685a131b22}"
namespace=tesk
source="$(mktemp -d)"
trap 'rm -rf "${source}"' EXIT

k() {
  docker compose exec -T k3s kubectl "$@"
}

git -C "${source}" init --quiet
git -C "${source}" fetch --quiet --depth 1 \
  https://github.com/elixir-cloud-aai/TESK.git "${commit}"
git -C "${source}" -c advice.detachedHead=false checkout --quiet FETCH_HEAD

k create namespace "${namespace}" --dry-run=client --output yaml \
  | k apply --filename -
helm template tesk "${source}/charts/tesk" --namespace "${namespace}" \
  --values tests/integration/tesk/values.yaml \
  | k apply --namespace "${namespace}" --filename -
if ! k rollout status deployment/tesk-api --namespace "${namespace}" --timeout=300s; then
  k get pods --namespace "${namespace}"
  k logs --namespace "${namespace}" deployment/tesk-api --tail=50
  exit 1
fi
# No readiness probe in the chart: the rollout doesn't tell that it serves
for _ in $(seq 60); do
  if docker compose exec -T k3s wget -q -O /dev/null \
      http://localhost:31567/ga4gh/tes/v1/service-info 2> /dev/null; then
    exit 0
  fi
  sleep 2
done
echo "::error::TESK doesn't serve its API"
k logs --namespace "${namespace}" deployment/tesk-api --tail=50
exit 1
