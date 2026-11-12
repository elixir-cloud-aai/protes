#!/usr/bin/env bash
# Integration test of the TES version paths of proTES (see `docs/tes-versions.md`)
# against a real TES implementation, in the Compose stack of this directory, as
# `check.sh`: a task with fields of TES 1.1 only that can be dropped reaches a
# TES 1.0 instance without them and a TES 1.1 instance with them, and a task
# that a TES 1.0 instance can't run unchanged (`ignore_error: true`) is rejected
# for it, and runs on a TES 1.1 instance. Run it from the repository root, with
# the base URL of the TES API, the URL and the API path of the backend, as in
# `config.yaml`, and the TES version that proTES must find for the backend
# (`1.0` or `1.1`), as arguments; needs curl, jq and docker compose (with
# `COMPOSE_FILE` set as for `up`). The task that ignores an error ends in
# `COMPLETE`, as TES 1.1 has it, unless `IGNORE_ERROR_STATE` says otherwise:
# Funnel 0.11.14 runs the next executor, but ends the task in `EXECUTOR_ERROR`
# (set `IGNORE_ERROR_STATE=EXECUTOR_ERROR` for it). The helpers and the image
# are those of `check.sh`, with `check_backend` added: keep them in sync.
set -euo pipefail

api="$1"
backend="$2"
backend_api="$2$3"
version="$4"
case "${version}" in
  1.0 | 1.1) ;;
  *)
    echo "::error::TES version must be 1.0 or 1.1, not ${version}"
    exit 2
    ;;
esac
final_wait="${FINAL_WAIT:-180}"
image='alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f'
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT

# An access token of the fake identity provider
auth="$(docker compose exec -T fake-tes python -c \
  "import json, urllib.request; \
  print(json.load(urllib.request.urlopen( \
  'http://localhost:8080/oidc/token?sub=alice', timeout=60))['access_token'])")"

# Request a path of proTES, with an optional JSON body; check the status and
# that the JSON response matches a jq filter
check() {
  local method="$1" path="$2" status="$3" filter="$4" data="${5:-}" actual
  local args=(--silent --show-error --max-time 60 --output "${body}"
    --write-out '%{http_code}' -X "${method}"
    --header 'Accept: application/json'
    --header "Authorization: Bearer ${auth}")
  if [ -n "${data}" ]; then
    args+=(--header 'Content-Type: application/json' --data "${data}")
  fi
  actual="$(curl "${args[@]}" "${api}${path}")"
  if [ "${actual}" != "${status}" ] \
      || ! jq -e "${filter}" "${body}" > /dev/null; then
    echo "::error::${backend}: ${method} ${path}: status ${actual}, expected ${status} and ${filter}"
    cat "${body}"
    return 1
  fi
}

# A task for this backend, from the executors and other fields (JSON) given
task() {
  local extra='{}'
  [ -n "${2:-}" ] && extra="$2"
  jq -n --arg backend "${backend}" --argjson executors "$1" \
    --argjson extra "${extra}" '{
      name: "integration-versions",
      tags: {"gateway.backends":
        ({type: "allow", backends: [$backend]} | tojson)},
      executors: $executors
    } + $extra'
}

# Wait until a task has a state
await_state() {
  local state=""
  for _ in $(seq "${final_wait}"); do
    check GET "/tasks/$1?view=MINIMAL" 200 '.state != null'
    state="$(jq -r .state "${body}")"
    [ "${state}" = "$2" ] && return
    sleep 1
  done
  echo "::error::${backend}: task $1 is ${state}, not $2"
  check GET "/tasks/$1?view=FULL" 200 'true'
  cat "${body}"
  return 1
}

# Check the task of proTES as its backend serves it (view `FULL`), from the
# Compose network, against a jq filter
check_backend() {
  local remote
  check GET "/tasks/$1?view=BASIC" 200 '.tags["gateway.backend_task_id"] | length > 0'
  remote="$(jq -r '.tags["gateway.backend_task_id"]' "${body}")"
  docker compose exec -T fake-tes python -c \
    "import json, sys, urllib.request; \
    print(json.dumps(json.load(urllib.request.urlopen(sys.argv[1], timeout=60))))" \
    "${backend_api}/tasks/${remote}?view=FULL" > "${body}"
  if ! jq -e "$2" "${body}" > /dev/null; then
    echo "::error::${backend}: task $1 at the backend doesn't match $2"
    cat "${body}"
    return 1
  fi
}

echo "${backend}: a task with fields of TES 1.1 that can be dropped (TES ${version})"
executor="$(jq -n --arg image "${image}" \
  '[{image: $image, command: ["cat", "/in/input.txt"], ignore_error: false}]')"
extra='{
  "inputs": [{"content": "integration-input\n", "path": "/in/input.txt",
    "type": "FILE", "streamable": true}],
  "resources": {"backend_parameters": {"protes-integration": "hint"}}
}'
check POST /tasks 200 '.id | length > 0' "$(task "${executor}" "${extra}")"
id="$(jq -r .id "${body}")"
await_state "${id}" COMPLETE
# Clients get the task back as they sent it
check GET "/tasks/${id}?view=FULL" 200 '
  .inputs[0].streamable == true
  and .executors[0].ignore_error == false
  and .resources.backend_parameters["protes-integration"] == "hint"
  and (.logs[0].logs[0].stdout | contains("integration-input"))'
# A TES 1.0 backend serves none of these fields (it may ignore fields that it
# doesn't know, so the unit tests of the adapters check that proTES drops them;
# `null` counts as absent, as some backends serve unset fields as `null`)
if [ "${version}" = 1.0 ]; then
  check_backend "${id}" '
    .inputs[0].path == "/in/input.txt"
    and (.executors | length == 1)
    and .inputs[0].streamable == null
    and .executors[0].ignore_error == null
    and .resources.backend_parameters == null'
else
  check_backend "${id}" '
    .inputs[0].streamable == true
    and .resources.backend_parameters["protes-integration"] == "hint"'
fi

echo "${backend}: a task that ignores the error of an executor (TES ${version})"
executors="$(jq -n --arg image "${image}" '[
  {image: $image, command: ["sh", "-c", "exit 3"], ignore_error: true},
  {image: $image, command: ["echo", "integration-after-error"]}
]')"
if [ "${version}" = 1.0 ]; then
  # No TES instance left that can run it unchanged: rejected, naming the field
  check POST /tasks 400 'tostring | contains("ignore_error")' \
    "$(task "${executors}")"
else
  check POST /tasks 200 '.id | length > 0' "$(task "${executors}")"
  id="$(jq -r .id "${body}")"
  check_backend "${id}" '.executors[0].ignore_error == true'
  # The next executor runs; the final state is `IGNORE_ERROR_STATE`
  await_state "${id}" "${IGNORE_ERROR_STATE:-COMPLETE}"
  check GET "/tasks/${id}?view=FULL" 200 '
    (.logs[0].logs | length == 2)
    and .logs[0].logs[0].exit_code == 3
    and (.logs[0].logs[1].stdout | contains("integration-after-error"))'
fi

echo "${backend}: TES ${version} paths passed"
