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
# A user's token goes over HTTPS only (`ALLOW_HTTP=true` to allow HTTP)
if [ -n "${TOKEN:-}" ] && [[ "${api}" != https://* ]] \
    && [ "${ALLOW_HTTP:-false}" != true ]; then
  echo "::error::${api} isn't an HTTPS URL; clients send tokens to it"
  exit 1
fi
backend="$2"
backend_api="$2$3"
# shellcheck source=tests/integration/lib.sh
. "$(dirname "$0")/lib.sh"
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
# An access token: `TOKEN`, or one of the fake identity provider
auth="${TOKEN:-}"
if [ -z "${auth}" ]; then
  auth="$(fake_token)"
fi

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
# that proTES got; with `TOKEN`, as proTES reports it
backend_state() {
  # To standard error: callers capture the state, and go on if this fails
  check GET "/tasks/$1?view=BASIC" 200 '.tags["gateway.backend_task_id"] | length > 0' >&2
  if [ -n "${TOKEN:-}" ]; then
    jq -r .state "${body}"
    return
  fi
  backend_task "$(backend_task_id)" MINIMAL | jq -r .state
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
if [ -n "${TOKEN:-}" ]; then
  echo "${backend}: not checked whether the backend stopped the task (check it there)"
elif [ "$(backend_state "${running}")" != CANCELED ]; then
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
