#!/usr/bin/env bash
# Checks, after `../check.sh`, that canceling a running task stopped its
# executor at TESK, as TESK reports `CANCELED` at once, whether or not it did.
# The task canceled right away may keep its executor (`sleep 300`): TESK doesn't
# tell the taskmaster of a task whose pod doesn't run yet (see "Notes on TES
# implementations" in `docs/configuration.md`), so one is allowed. Needs jq and
# docker compose (with `COMPOSE_FILE` set as for `up`).
set -euo pipefail

active=""
for _ in $(seq 60); do
  active="$(docker compose exec -T k3s kubectl get jobs --namespace tesk \
    --selector job-type=executor --output json \
    | jq '[.items[] | select((.status.active // 0) > 0)] | length')"
  if [ "${active}" -le 1 ]; then
    echo "TESK: ${active} executors still active after the check"
    exit 0
  fi
  sleep 2
done
echo "::error::TESK: ${active} executors still active after canceling their tasks"
exit 1
