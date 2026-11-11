#!/usr/bin/env bash
# Smoke test of proTES started with the Compose override of this directory
# (`compose.yaml`): relays a task to the fake TES backends, which complete it
# right away, waits until the Celery worker has tracked it (via RabbitMQ) to
# the end, and checks the other endpoints and access control, with access
# tokens of the fake identity provider; then runs the checks of deployed
# instances (`deployment.sh`) against it. Run it from the repository root,
# with the base URL of the TES API as argument (default
# `http://localhost:8080/ga4gh/tes/v1`); needs curl, jq and docker compose
# (with `COMPOSE_FILE` set as for `up`, to query the fake backend).
set -euo pipefail

api="${1:-http://localhost:8080/ga4gh/tes/v1}"
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT

# Get an access token of a user from the fake identity provider
token() {
  docker compose exec -T fake-tes python -c \
    "import json, urllib.request; \
    print(json.load(urllib.request.urlopen( \
    'http://localhost:8080/oidc/token?sub=$1'))['access_token'])"
}
# Access token of the requests of `check` (none if empty)
auth="$(token alice)"

# Request a path, with an optional JSON body; check the status and that the
# JSON response matches a jq filter
check() {
  local method="$1" path="$2" status="$3" filter="$4" data="${5:-}" actual
  local args=(--silent --show-error --output "${body}"
    --write-out '%{http_code}' -X "${method}"
    --header 'Accept: application/json')
  if [ -n "${auth}" ]; then
    args+=(--header "Authorization: Bearer ${auth}")
  fi
  if [ -n "${data}" ]; then
    args+=(--header 'Content-Type: application/json' --data "${data}")
  fi
  actual="$(curl "${args[@]}" "${api}${path}")"
  if [ "${actual}" != "${status}" ] \
      || ! jq -e "${filter}" "${body}" > /dev/null; then
    echo "::error::${method} ${path}: status ${actual}, expected ${status} and ${filter}"
    cat "${body}"
    return 1
  fi
}

check GET /service-info 200 '.gateway.plugins_url == "/ga4gh/tes/v1/plugins"'
check GET /service-info 200 '.type.version == "1.1.0"'
check GET /plugins 200 '.tag_prefix == "gateway."'
# Wait until a task (the first argument) has a state (the second one)
await_state() {
  local state=""
  for _ in $(seq 60); do
    check GET "/tasks/$1?view=MINIMAL" 200 '.state != null'
    state="$(jq -r .state "${body}")"
    [ "${state}" = "$2" ] && return
    sleep 1
  done
  echo "::error::Task $1 not $2, but ${state}"
  return 1
}
# TES 1.0 and 1.1: the first backend (TES 1.0) rejects fields of TES 1.1, so a
# task with fields that can be dropped only reaches it without them, and is
# served with them
check POST /tasks 200 '.id | length > 0' \
  '{"name": "smoke", "tags": {"smoke.key": "a,b"},
    "inputs": [{"content": "a", "path": "/a", "streamable": true}],
    "outputs": [{"url": "s3://b/c", "path": "/c", "path_prefix": "/"}],
    "resources": {"backend_parameters": {"VmSize": "small"}},
    "executors": [{"image": "alpine", "command": ["true"], "ignore_error": false}]}'
id="$(jq -r .id "${body}")"
await_state "${id}" COMPLETE
check GET "/tasks/${id}?view=FULL" 200 \
  '.name == "smoke" and .tags["gateway.backend"] == "http://fake-tes-10:8080" and .logs[0].metadata.forwarded_to == null and .inputs[0].streamable == true and .outputs[0].path_prefix == "/" and (.logs[0].logs[0].stdout | contains("fake stderr")) and (.logs[0].logs[0] | has("stderr") | not)'
# Tasks that TES 1.0 can't express go to the backend of TES 1.1, which serves
# a field that the TES specification doesn't have: it is tracked all the same,
# and the field isn't served
needs_11='"executors": [{"image": "alpine", "command": ["true"], "ignore_error": true}]'
check POST /tasks 200 '.id | length > 0' "{${needs_11}}"
id_11="$(jq -r .id "${body}")"
await_state "${id_11}" COMPLETE
check GET "/tasks/${id_11}?view=FULL" 200 \
  '.tags["gateway.backend"] == "http://fake-tes:8080" and .executors[0].ignore_error == true and (has("author") | not)'
# ... and are rejected if the client chooses only the other one
check POST /tasks 400 '.detail | contains("ignore_error")' \
  "{${needs_11}, \"tags\": {\"gateway.backends\":
    \"{\\\"type\\\": \\\"allow\\\", \\\"backends\\\": [\\\"http://fake-tes-10:8080\\\"]}\"}}"
# A backend with an API path of its own (`api_path`, as for TESP): tasks are
# created, tracked and canceled there
only_v1='"tags": {"gateway.backends":
    "{\"type\": \"allow\", \"backends\": [\"http://fake-tes-v1:8080\"]}"'
check POST /tasks 200 '.id | length > 0' \
  "{${only_v1}}, \"executors\": [{\"image\": \"alpine\", \"command\": [\"true\"]}]}"
id_v1="$(jq -r .id "${body}")"
await_state "${id_v1}" COMPLETE
check GET "/tasks/${id_v1}?view=FULL" 200 \
  '.tags["gateway.backend"] == "http://fake-tes-v1:8080"'
check POST /tasks 200 '.id | length > 0' \
  "{${only_v1}, \"fake.hold\": \"true\"}, \"executors\": [{\"image\": \"alpine\", \"command\": [\"true\"]}]}"
held_v1="$(jq -r .id "${body}")"
await_state "${held_v1}" RUNNING
check POST "/tasks/${held_v1}:cancel" 200 '. == {}'
await_state "${held_v1}" CANCELED
# The logs of TES instances, in the view `FULL` only
check GET "/tasks/${id_11}?view=FULL" 200 \
  '.logs[0].logs[0].stdout == "fake stdout" and .logs[0].logs[0].stderr == "fake stderr" and .logs[0].system_logs == ["fake system log"]'
check GET "/tasks/${id_11}?view=BASIC" 200 \
  '(.logs[0].logs[0] | has("stdout") | not) and (.logs[0] | has("system_logs") | not)'
# List filters
check GET '/tasks?view=MINIMAL' 200 "any(.tasks[]; .id == \"${id}\")"
check GET '/tasks?view=MINIMAL&state=COMPLETE' 200 "any(.tasks[]; .id == \"${id}\")"
check GET '/tasks?view=MINIMAL&state=RUNNING' 200 "all(.tasks[]; .id != \"${id}\")"
check GET '/tasks?view=MINIMAL&tag_key=smoke.key&tag_value=a%2Cb' 200 \
  "any(.tasks[]; .id == \"${id}\")"
check GET '/tasks?view=MINIMAL&tag_key=smoke.key&tag_value=a' 200 \
  "all(.tasks[]; .id != \"${id}\")"
check GET '/tasks?view=MINIMAL&tag_key=smoke.other&tag_value=a%2Cb' 200 \
  "all(.tasks[]; .id != \"${id}\")"
check GET '/tasks?view=MINIMAL&tag_key=gateway.backend&tag_value=http://fake-tes:8080' 200 \
  "any(.tasks[]; .id == \"${id_11}\") and all(.tasks[]; .id != \"${id}\")"
# Canceled tasks are `CANCELING` until their backend reports the final state
check POST "/tasks/${id}:cancel" 200 '. == {}'
check POST /tasks 200 '.id | length > 0' \
  '{"tags": {"fake.hold": "true"}, "executors": [{"image": "alpine", "command": ["true"]}]}'
held="$(jq -r .id "${body}")"
await_state "${held}" RUNNING
check POST "/tasks/${held}:cancel" 200 '. == {}'
check GET "/tasks/${held}?view=MINIMAL" 200 '.state == "CANCELING" or .state == "CANCELED"'
await_state "${held}" CANCELED
# Unknown tasks are forbidden, as tasks of others are
check GET /tasks/UNKNWN 403 '.status == 403'
# Access control: no token (but the service info is public), no role, other
# users and admins (only owners see and cancel tasks)
auth=""
check GET /service-info 200 '.type.artifact == "tes"'
check GET /tasks 401 '.status == 401'
check GET /plugins 401 '.status == 401'
auth="$(token eve)"
check GET /tasks 403 '.status == 403'
auth="$(token bob)"
check GET '/tasks?view=MINIMAL' 200 "all(.tasks[]; .id != \"${id}\")"
check GET "/tasks/${id}" 403 '.status == 403'
check POST "/tasks/${id}:cancel" 403 '.status == 403'
auth="$(token admin)"
check GET "/tasks/${id}" 403 '.status == 403'
# The fake backend got the task (and those of earlier runs, if not
# restarted), with the access token of proTES (which it requires); listed
# with a token of proTES' client
docker compose exec -T fake-tes python -c \
  "import base64, json, sys, urllib.request; \
  sys.path.insert(0, '/smoke'); \
  from fake_tes import CLIENT_ID, CLIENT_SECRET; \
  basic = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode(); \
  request = urllib.request.Request('http://localhost:8080/oidc/token', \
    data=b'grant_type=client_credentials', headers={'Authorization': f'Basic {basic}'}); \
  token = json.load(urllib.request.urlopen(request))['access_token']; \
  request = urllib.request.Request('http://localhost:8080/ga4gh/tes/v1/tasks', \
    headers={'Authorization': f'Bearer {token}'}); \
  tasks = json.load(urllib.request.urlopen(request))['tasks']; \
  assert tasks and all(task['state'] in ('COMPLETE', 'CANCELED') for task in tasks), tasks"
# The checks of deployed instances (`deployment.sh`), over plain HTTP here,
# so that its access checks are known to pass where they should; it repeats
# some of the checks above, as it runs on its own against deployments. The
# tokens are fetched first, so that a failure stops the test
user_token="$(token alice)"
no_role_token="$(token eve)"
TOKEN="${user_token}" OTHER_TOKEN="${no_role_token}" ALLOW_HTTP=true \
  "$(dirname "$0")/deployment.sh" "${api}"
echo "Smoke test passed"
