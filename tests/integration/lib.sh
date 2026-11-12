# Helpers of the integration tests (`check.sh`, `versions.sh`). Source it
# after setting `api` (the base URL of proTES's TES API) and `backend` (the
# backend's URL, for messages), and `backend_api` (the URL of its TES API) for
# `backend_task`; then set `auth`, the access token that `check` sends. Sourcing
# creates the temporary file `${body}` and an `EXIT` trap that removes it: a
# script that sets its own `EXIT` trap must remove the file too. Every request
# and every call to the backend has a limit of 60 seconds; waits for a state
# poll up to `FINAL_WAIT` times (default 180), about once a second. Tokens go to curl on its standard input,
# so that they don't show up in the process list. Needs curl and jq; docker
# compose (with `COMPOSE_FILE` set as for `up`) only for `fake_token` and
# `backend_task`.
# shellcheck shell=bash

# Polls while waiting for a state, one a second (a pull and a start may take long)
final_wait="${FINAL_WAIT:-180}"
image='alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f'
# The last response body
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT
# The access token of `check`; set by the sourcing script
auth=""

# An access token of the fake identity provider, for the user `alice`
fake_token() {
  timeout 60 docker compose exec -T fake-tes python -c \
    "import json, urllib.request; \
    print(json.load(urllib.request.urlopen( \
    'http://localhost:8080/oidc/token?sub=alice', timeout=60))['access_token'])"
}

# Request a path of proTES, with an optional JSON body; check the status and
# that the JSON response matches a jq filter
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

# The identifier of a task of proTES at its backend, from the task in
# `${body}` (view `BASIC` or `FULL`)
backend_task_id() {
  jq -r '.tags["gateway.backend_task_id"]' "${body}"
}

# A task as the backend serves it (JSON), by its identifier there and a view,
# from the Compose network, as the backend isn't published
backend_task() {
  timeout 60 docker compose exec -T fake-tes python -c \
    "import json, sys, urllib.request; \
    print(json.dumps(json.load(urllib.request.urlopen(sys.argv[1], timeout=60))))" \
    "${backend_api}/tasks/$1?view=$2"
}
