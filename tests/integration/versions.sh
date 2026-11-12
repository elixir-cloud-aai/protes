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
# `COMPLETE`, as TES 1.1 has it. The helpers and the image are in `lib.sh`,
# shared with `check.sh`.
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
# shellcheck source=tests/integration/lib.sh
. "$(dirname "$0")/lib.sh"
auth="$(fake_token)"

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

# Check the task of proTES as its backend serves it (view `FULL`), from the
# Compose network, against a jq filter
check_backend() {
  check GET "/tasks/$1?view=BASIC" 200 '.tags["gateway.backend_task_id"] | length > 0'
  backend_task "$(backend_task_id)" FULL > "${body}"
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
  # TES 1.1 lets a backend ignore keys of `backend_parameters` that it doesn't
  # support, unless they are strict: it keeps the key or drops it (Funnel
  # 0.12.3 keeps only the keys that it lists in its service info); any other
  # value of the key fails
  check_backend "${id}" '
    .inputs[0].streamable == true
    and (.resources.backend_parameters | type == "object")
    and (.resources.backend_parameters
      | (has("protes-integration") | not)
        or .["protes-integration"] == "hint")'
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
  # The next executor runs, and the task completes
  await_state "${id}" COMPLETE
  check GET "/tasks/${id}?view=FULL" 200 '
    (.logs[0].logs | length == 2)
    and .logs[0].logs[0].exit_code == 3
    and (.logs[0].logs[1].stdout | contains("integration-after-error"))'
fi

echo "${backend}: TES ${version} paths passed"
