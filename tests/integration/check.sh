#!/usr/bin/env bash
# Integration test of proTES against a real TES implementation, started with
# the Compose override of this directory (`compose.yaml`): tasks that finish,
# fail and are canceled, with their states and logs as clients see them. Run it
# from the repository root, with the base URL of the TES API, and the URL and
# the API path of the backend, as in `config.yaml`, as arguments; needs curl, jq
# and docker compose (with `COMPOSE_FILE` set as for `up`, to get a token and to
# ask the backend). Tasks run real containers, whose images the backend pulls,
# so the waits are long. TESP-API reports tasks as canceled, but leaves their
# containers running, so the executors end by themselves. TESK has one log per
# executor, with both streams, in `stdout`: set `COMBINED_LOGS=true` for it, so
# that what an executor writes to its standard error is looked for there. TESK
# reports tasks as canceled at once; `tesk/stopped.sh` checks their executors.
# TESP-API with Pulsar 0.15 or later returns empty logs: set `EMPTY_LOGS=true`
# for it, so that empty streams pass (other contents must still match).
#
# To check a deployed instance instead (e.g., on CSC's Rahti; see "Testing a
# deployment" in `docs/development.md`), set `TOKEN` to an access token of a
# user with the role `user`, and pass the public URL of its TES API and the URL
# and API path of one of its TES instances, as in its config. Then docker
# compose isn't needed, and the states of tasks at the backend are those that
# proTES reports, as the backend may not accept the user's token. Tokens are
# read from the environment only, so that they don't show up in the process
# list.
set -euo pipefail

api="$1"
backend="$2"
backend_api="$2$3"
# Seconds to wait for a state (a pull and a start may take long)
final_wait="${FINAL_WAIT:-180}"
# Where a backend reports the standard error of an executor
stderr_field=stderr
if [ "${COMBINED_LOGS:-false}" = true ]; then
  stderr_field=stdout
fi
# A jq function: whether a stream of an executor's log has a text; also if
# it is empty with `EMPTY_LOGS=true` (TESP-API with Pulsar 0.15 or later)
empty_logs=false
if [ "${EMPTY_LOGS:-false}" = true ]; then
  empty_logs=true
fi
logged="def logged(\$text):
  if ${empty_logs} and (. // \"\") == \"\" then true else contains(\$text) end;"
image='alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f'
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT

# An access token: `TOKEN`, or one of the fake identity provider
auth="${TOKEN:-}"
if [ -z "${auth}" ]; then
  auth="$(docker compose exec -T fake-tes python -c \
    "import json, urllib.request; \
    print(json.load(urllib.request.urlopen( \
    'http://localhost:8080/oidc/token?sub=alice'))['access_token'])")"
fi

# Request a path, with an optional JSON body; check the status and that the
# JSON response matches a jq filter
check() {
  local method="$1" path="$2" status="$3" filter="$4" data="${5:-}" actual
  local args=(--silent --show-error --max-time 60 --output "${body}"
    --write-out '%{http_code}' -X "${method}"
    --header 'Accept: application/json' --header @-)
  if [ -n "${data}" ]; then
    args+=(--header 'Content-Type: application/json' --data "${data}")
  fi
  # The token goes to curl on its standard input, not as an argument
  actual="$(printf 'Authorization: Bearer %s\n' "${auth}" \
    | curl "${args[@]}" "${api}${path}")"
  if [ "${actual}" != "${status}" ] \
      || ! jq -e "${filter}" "${body}" > /dev/null; then
    echo "::error::${backend}: ${method} ${path}: status ${actual}, expected ${status} and ${filter}"
    cat "${body}"
    return 1
  fi
}

# Create a task on this backend; the command is a shell script. The identifier
# is in `${id}`
create() {
  local task
  task="$(jq -n --arg backend "${backend}" --arg image "${image}" \
    --arg script "$1" '{
      name: "integration",
      tags: {"gateway.backends":
        ({type: "allow", backends: [$backend]} | tojson)},
      inputs: [{content: "integration-input\n", path: "/in/input.txt", type: "FILE"}],
      executors: [{image: $image, command: ["sh", "-c", $script]}]
    }')"
  check POST /tasks 200 '.id | length > 0' "${task}"
  id="$(jq -r .id "${body}")"
}

# The state of a task of proTES as its backend reports it, by the identifier
# that proTES got (from the Compose network, as the backend isn't published);
# with `TOKEN`, as proTES reports it
backend_state() {
  local remote
  check GET "/tasks/$1?view=BASIC" 200 '.tags["gateway.backend_task_id"] | length > 0'
  if [ -n "${TOKEN:-}" ]; then
    jq -r .state "${body}"
    return
  fi
  remote="$(jq -r '.tags["gateway.backend_task_id"]' "${body}")"
  docker compose exec -T fake-tes python -c \
    "import json, sys, urllib.request; \
    print(json.load(urllib.request.urlopen(sys.argv[1]))['state'])" \
    "${backend_api}/tasks/${remote}?view=MINIMAL"
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

echo "${backend}: a task that finishes"
create 'cat /in/input.txt; echo integration-stdout; echo integration-stderr >&2'
done="${id}"
await_state "${done}" COMPLETE
check GET "/tasks/${done}?view=FULL" 200 "${logged}
  .tags[\"gateway.backend\"] == \"${backend}\"
  and (.logs | length == 1)
  and .logs[0].logs[0].exit_code == 0
  and (.logs[0].logs[0].stdout | logged(\"integration-input\"))
  and (.logs[0].logs[0].stdout | logged(\"integration-stdout\"))
  and (.logs[0].logs[0].${stderr_field} | logged(\"integration-stderr\"))"
check GET "/tasks/${done}?view=BASIC" 200 '
  .state == "COMPLETE"
  and (.logs[0].logs[0] | has("stdout") or has("stderr") | not)
  and (.logs[0] | has("system_logs") | not)'
# Fields that the backend adds aren't served (e.g., `author` of TESP)
check GET "/tasks/${done}?view=FULL" 200 'has("author") | not'
# Finished tasks are left as they are
check POST "/tasks/${done}:cancel" 200 '. == {}'
check GET "/tasks/${done}?view=MINIMAL" 200 '.state == "COMPLETE"'

echo "${backend}: a task that fails"
create 'echo integration-failure >&2; exit 3'
failed="${id}"
await_state "${failed}" EXECUTOR_ERROR
check GET "/tasks/${failed}?view=FULL" 200 "${logged}
  .logs[0].logs[0].exit_code == 3
  and (.logs[0].logs[0].${stderr_field} | logged(\"integration-failure\"))"

echo "${backend}: a task that is canceled while it runs"
create 'echo integration-started; sleep 300'
running="${id}"
for _ in $(seq "${final_wait}"); do
  [ "$(backend_state "${running}")" = RUNNING ] && break
  sleep 1
done
if [ "$(backend_state "${running}")" != RUNNING ]; then
  echo "::error::${backend}: task ${running} doesn't run at its backend"
  exit 1
fi
check POST "/tasks/${running}:cancel" 200 '. == {}'
check GET "/tasks/${running}?view=MINIMAL" 200 '
  .state == "CANCELING" or .state == "CANCELED"'
await_state "${running}" CANCELED
if [ "$(backend_state "${running}")" != CANCELED ]; then
  echo "::error::${backend}: task ${running} isn't canceled at its backend"
  exit 1
fi
# Stays canceled, also if canceled again
check POST "/tasks/${running}:cancel" 200 '. == {}'
check GET "/tasks/${running}?view=FULL" 200 '.state == "CANCELED"'

echo "${backend}: a task that is canceled right away"
create 'sleep 300'
check POST "/tasks/${id}:cancel" 200 '. == {}'
await_state "${id}" CANCELED

echo "${backend}: integration test passed"
