#!/usr/bin/env bash
# Checks, after `../check.sh`, that canceling a running task stopped its
# executor at TESK, as TESK reports `CANCELED` at once, whether or not it did.
# That task is the only one with this marker in its script. The task canceled
# right away isn't checked: it may keep its executor (`sleep 300`), as TESK doesn't
# tell the taskmaster of a task whose pod doesn't run yet (see "Notes on TES
# implementations" in `docs/configuration.md`). Needs jq and docker compose (with
# `COMPOSE_FILE` set as for `up`).
set -euo pipefail

marker=integration-started
active=""
for _ in $(seq 60); do
  active="$(docker compose exec -T k3s kubectl get jobs --namespace tesk \
    --selector job-type=executor --output json \
    | jq --arg marker "${marker}" '[.items[]
      | select((.status.active // 0) > 0)
      | select(.spec.template | tostring | contains($marker))] | length')"
  if [ "${active}" -eq 0 ]; then
    echo "TESK: the executor of the canceled task has stopped"
    exit 0
  fi
  sleep 2
done
echo "::error::TESK: the executor of a canceled task is still active"
exit 1
