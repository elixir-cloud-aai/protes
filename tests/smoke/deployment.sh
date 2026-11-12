#!/usr/bin/env bash
# Checks of a deployed instance of proTES at its public URL, as on CSC's Rahti:
# that it is served over HTTPS only, that its service info is public, and that
# it enforces access control, with access tokens of its identity provider. It
# creates no tasks: run `tests/integration/check.sh` with `TOKEN` for each TES
# instance for that (see "Testing a deployment" in `docs/development.md`). Run
# it from anywhere, with the base URL of the TES API as argument (e.g.,
# `https://tes.example.org/ga4gh/tes/v1`); needs curl and jq. Environment:
#
# - `TOKEN` (required): an access token of a user with the role `user`
# - `OTHER_TOKEN`: an access token of a user without a role (e.g., not in the
#   groups mapped to roles), who must be refused; skipped if unset
# - `ALLOW_HTTP=true`: accept a URL with `http`, and skip the check that plain
#   HTTP doesn't serve the API (for CI's smoke test, without TLS)
#
# Tokens are read from the environment only, so that they don't show up in
# the process list.
set -euo pipefail

api="${1:?usage: $0 <base URL of the TES API>}"
api="${api%/}"
if [ -z "${TOKEN:-}" ]; then
  echo "::error::Set TOKEN to an access token of a user with the role user"
  exit 1
fi
body="$(mktemp)"
trap 'rm -f "${body}"' EXIT

# Request a path with a token (none if empty); check the status and that the
# JSON response matches a jq filter
check() {
  local token="$1" method="$2" path="$3" status="$4" filter="$5" actual
  local args=(--silent --show-error --max-time 60 --output "${body}"
    --write-out '%{http_code}' -X "${method}"
    --header 'Accept: application/json')
  if [ -n "${token}" ]; then
    # The token goes to curl on its standard input, not as an argument
    actual="$(printf 'Authorization: Bearer %s\n' "${token}" \
      | curl "${args[@]}" --header @- "${api}${path}")"
  else
    actual="$(curl "${args[@]}" "${api}${path}" < /dev/null)"
  fi
  if [ "${actual}" != "${status}" ] \
      || ! jq -e "${filter}" "${body}" > /dev/null; then
    echo "::error::${method} ${path}: status ${actual}, expected ${status} and ${filter}"
    cat "${body}"
    return 1
  fi
}

# Served over HTTPS only: plain HTTP is refused or redirected to HTTPS (an
# OpenShift Route of the chart redirects)
case "${api}" in
  https://*)
    # The same host and path, at the default port of HTTP (not at 443)
    rest="${api#https://}"
    authority="${rest%%/*}"
    path="${rest#"${authority}"}"
    plain="http://${authority%:443}${path}/service-info"
    if result="$(curl --silent --max-time 30 --output /dev/null \
        --write-out '%{http_code} %{redirect_url}' "${plain}")"; then
      status="${result%% *}"
      location="${result#* }"
      case "${status}" in
        3??)
          if [[ "${location}" != https://* ]]; then
            echo "::error::${plain} redirects to '${location}', not to HTTPS"
            exit 1
          fi
          ;;
        2??)
          echo "::error::${plain} is served over plain HTTP (status ${status})"
          exit 1
          ;;
      esac
    fi
    ;;
  *)
    if [ "${ALLOW_HTTP:-false}" != true ]; then
      echo "::error::${api} isn't an HTTPS URL; clients send tokens to it"
      exit 1
    fi
    ;;
esac

# The service info is public; tasks and plugins need a valid token
check "" GET /service-info 200 '.type.artifact == "tes"'
check "" GET /tasks 401 '.status == 401'
check "" GET /plugins 401 '.status == 401'
check not-a-token GET /tasks 401 '.status == 401'
check "${TOKEN}" GET '/tasks?view=MINIMAL' 200 '.tasks | type == "array"'
check "${TOKEN}" GET /plugins 200 '.tag_prefix | type == "string"'
# Unknown tasks are forbidden, as tasks of others are
check "${TOKEN}" GET /tasks/UNKNWN 403 '.status == 403'
if [ -n "${OTHER_TOKEN:-}" ]; then
  check "${OTHER_TOKEN}" GET /tasks 403 '.status == 403'
fi
echo "Deployment checks passed"
