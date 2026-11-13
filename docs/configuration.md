# Configuration

The app is configured by
[`pro_tes/config.yaml`](https://github.com/elixir-cloud-aai/protes/blob/dev/pro_tes/config.yaml),
the base config, which documents its values. It is a
[FOCA](https://foca.readthedocs.io/) config: the sections `server`,
`security`, `db`, `api`, `log`, `jobs`, `service_info` and `exceptions` are
described in the [FOCA documentation](https://foca.readthedocs.io/en/latest/),
the app's own settings are in section `custom` (validated on startup by
[`pro_tes.config_models.CustomConfig`](reference.md#pro_tes.config_models.CustomConfig);
unknown keys, e.g., misspelled ones, are errors, so that the app and the
Celery worker don't start with settings they ignore).
To override some values, write them to a config overlay file and list it in
environment variable `FOCA_CONFIG_OVERLAY` (several files separated by `:`);
FOCA merges overlays into the config on startup (see the [FOCA deployment
guide](https://foca.readthedocs.io/en/latest/deployment/)). The Celery worker
reads the same config.

## Local overrides

Docker Compose mounts
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/protes/blob/dev/config.dev.yaml),
an overlay for local development (see [Getting
started](getting-started.md#usage)), into the app and the Celery worker. To
override values locally without changing it, put them in
`config.local.yaml` and mount it in a `docker-compose.override.yaml` (both
are ignored by Git), for both services:

```yaml
services:
    protes:
        environment:
            FOCA_CONFIG_OVERLAY: /etc/protes/config.dev.yaml:/etc/protes/config.local.yaml
        volumes:
            - ./config.local.yaml:/etc/protes/config.local.yaml:ro
    protes-worker:
        environment:
            FOCA_CONFIG_OVERLAY: /etc/protes/config.dev.yaml:/etc/protes/config.local.yaml
        volumes:
            - ./config.local.yaml:/etc/protes/config.local.yaml:ro
```

## TES instances

Tasks are forwarded to the TES instances of the backend list,
`custom.tes.backends`, in order (a typed backend list of FOCA,
`foca.plugins.backends`). Each
entry is either a TES instance (`type: ga4gh-tes`), with its base URL
(`url`, without the API path `/ga4gh/tes/v1`; see [API paths](#api-paths) for
TES instances that serve it elsewhere) and how proTES authenticates
at it (`auth`: with its own access token, the default, with HTTP Basic
credentials from environment variables, or not at all; see [Credentials of
TES instances](access-control.md#credentials-of-tes-instances)), or a
service registry, whose verified TES instances are used in its place (`type:
ga4gh-service-registry`, see [Discovery in a service
registry](#discovery-in-a-service-registry)). The base config lists none;
deployments set their own list, e.g.:

```yaml
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://tes.example.org
        # Labels and location, e.g., for routing
        labels: {region: eu-north}
        location: {latitude: 60.17, longitude: 24.94}
      - type: ga4gh-tes
        url: https://tes.example.com
        auth: none
      # A TES instance that serves the TES API at another path, e.g., TESP
      - type: ga4gh-tes
        url: https://tesp.example.org
        api_path: /v1
    # Identity provider of proTES, for the TES instances with
    # `auth: client_credentials` (the default)
    client_credentials:
      issuer: https://login.example.org/oidc/
```

By default, all TES instances with `auth: client_credentials` get the same
access token of proTES; with `client_credentials.audience_parameter`
(`resource` or `audience`), each gets a token for its own audience: the
`audience` of its entry, or else its URL (see [Tokens per TES
instance](access-control.md#tokens-per-tes-instance)).

URLs are normalized (e.g., lowercase host, without default port and trailing
slashes) and must use one of the schemes of the entry's `url_schemes`
(default: `https` only; add `http` for local tests only, as credentials are
sent unencrypted), without user info. Each entry has an identifier (`id`, by
default its normalized URL); entries with the same identifier are rejected,
and a URL listed twice is used once, at its first place.
`custom.tes.url_schemes` applies to the identity provider of
`custom.tes.client_credentials` only. The [plugins](plugins.md)
decide which of the TES instances are tried, and in which order (see
[Tasks](api.md#tasks)). The service info, which anyone can read, lists the
TES instances (`backends`: identifier, URL, source, group and labels of
each, without credentials; the group of discovered TES instances is the
identifier of their registry entry, by default its URL, so set `id` for
registries at internal URLs), unless you [hide them](#service-info). TES
instances at or below the public URL of proTES (`server.public_url`, if set;
the Helm chart sets it from `hostName`) are never used, so that proTES never
forwards tasks to itself: proTES refuses to start if a configured one is.

proTES reads at most `custom.tes.max_response_size` bytes of each response
of a TES instance (default: 10485760, i.e., 10 MiB; from 1 MiB to 256 MiB),
configured or discovered: a larger response fails, with an error in the logs
(`Response of TES backend larger than ...`), and the rest of it is not read
(error answers of configured TES instances, with statuses other than 2xx,
are cut off instead, as their status decides, and the bodies of redirects
that are followed are not read; compressed responses count with their
decompressed size). For discovered TES instances,
error answers over the limit fail too (FOCA's guarded HTTP client), so that
a refusal with a large body (e.g., the error page of a gateway) counts as an
unknown outcome.
This bounds the memory that each request takes, as a response is held in
memory while it is parsed, which takes about 7 times its size. A larger
response to a request to create a task counts as an unknown outcome, as the
TES instance got the request (see below), and so does one to a request to
cancel a task; one to a request for a task's state counts as a failed poll;
one with the logs of a finished task leaves the task without them (see
[Tasks](#tasks)); one with the service info of a discovered TES instance,
read for its API path, means that the API path is unknown, so that tasks go
to other TES instances (see [API paths](#api-paths)). Raise the limit, and the memory
of the app and of the Celery worker with it, for TES instances whose logs
proTES should store in full (`post_task.max_log_size`).

Requests to configured TES instances time out after `custom.tes.timeout`
seconds without data (default: 15), and after 5 seconds while connecting.
Requests to discovered TES instances must end within `custom.tes.timeout`
seconds as a whole (resolving, connecting, sending and reading), and
connecting within 10 seconds (FOCA's guarded HTTP client). proTES creates, gets
and cancels tasks with a single request each, to the API path of the URL
(`<url>/ga4gh/tes/v1`, see [API paths](#api-paths)), and never sends a request
to create or cancel a task again, so that no TES instance gets a task twice; these requests don't
follow redirects. It tries the next TES instance only if a TES instance
certainly did not accept the task: it was not reached (connection refused,
host not resolved, connection timed out, certificate not accepted), there
were no credentials for it, or it answered with a status other than 2xx and
5xx (e.g., `400` or `401`, or a redirect, e.g., of an authentication
gateway or from `http` to `https`: configure the URL that the redirect
leads to). If
it may have accepted the task, because the response timed out, the
connection was lost after the request was sent, or it answered with a
server error (5xx; gateways answer `502`, `503` or `504` also when the TES
instance behind them got the request), the task is not sent to other TES
instances, as it may run already: it fails with `SYSTEM_ERROR` (see
[Tasks](api.md#tasks)). For configured TES instances, other failures of TLS
handshakes (e.g., an `https` URL of a plain HTTP port) and of proxies count
as unknown outcomes too, so they stop the failover: check their URLs. For
discovered ones, failed handshakes mean that nothing was sent, so that the
next TES instance is tried. Set the
timeout long enough for slow TES instances, but keep the duration of a
request to create a task below the read timeout of a reverse proxy in front
of proTES (e.g., 60 seconds by default for ingress-nginx): clients that
retry requests that timed out may create tasks twice. Forwarding attempts
start within `plugins.defaults.forward_timeout` seconds (20 in the base
config; see [Plugins](plugins.md#how-a-task-is-processed)), but the last
attempt may still take up to the timeout to be answered, and getting the
state of the accepted task up to 5 seconds to connect and the timeout
again, so a request takes up to `forward_timeout + 2 * timeout + 5`
seconds (55 with the defaults), plus the time of the plugins (up to
`plugins.defaults.pipeline_timeout` before and the timeouts of the
`response` steps after forwarding; the default pipeline's plugin takes
almost none). Not included are: reads of service registries that are due
(`refresh_interval`), which the request that finds them due makes before
forwarding (up to twice the registry entry's `timeout` per registry);
getting an access token of proTES (up to `client_credentials.timeout` per
request to the identity provider); connecting to each address of a host
in turn (e.g., IPv6, then IPv4); TES instances that send their answers
slowly (the timeout counts time without data); and writes to the
database. Reading the API path of a discovered TES instance whose path
isn't cached takes up to 5 seconds of the time left to forward (see [API
paths](#api-paths)). Raise the
proxy's timeout, or lower these settings together.

The shorter `forward_timeout`, the fewer TES instances are tried when some
are unreachable: with the defaults, about 4 configured TES instances that
don't answer while connecting (up to 5 seconds each), or 2 discovered ones
(up to 10 seconds each), use up the 20 seconds, and the task fails with
`503` without trying the next ones. Reading the service info of a TES
instance for its version (see [TES versions](#tes-versions)) takes up to 5 more
seconds if the instance can't be reached (and as long again if it accepts the
connection, but doesn't answer), at most once a minute per process, so
the first requests after that get to about half as many instances; for a
discovered TES instance whose API path isn't cached, reading the path takes
up to 5 seconds more. Order
`custom.tes.backends` so that
reliable TES instances come first, or raise `forward_timeout` (and the
proxy's timeout) for long lists of unreliable TES instances.

Requests in flight when an instance of proTES stops are cut off after
Gunicorn's graceful timeout (20 seconds in the chart).

### API paths

proTES appends the API path of the TES specification, `/ga4gh/tes/v1`, to the
URL of a TES instance. TES instances that serve the TES API elsewhere set
`api_path` in their entry (`/` if they serve it at the URL itself). It must be
an absolute path of unreserved characters, without segments `.` or `..`, and
applies to all requests to the TES instance. For configured TES instances, a
path in `url` is no alternative, as the default API path is appended to it
(discovered ones are different, see below).

[TESP](https://github.com/CESNET/tesp-api) (TESP-API with Pulsar) serves the
TES API at `/v1` only, so a configured TESP needs `api_path: /v1`, unless a
proxy in front of it rewrites the default path; one listed in a service
registry with its path (e.g., `https://tesp.example.org/v1`) is found there.
proTES also ignores the fields that TESP adds to tasks (see [TES versions](tes-versions.md#fields-that-tes-instances-add)).

Plugins always see the path of the API of proTES (`/ga4gh/tes/v1/tasks` in
outbound requests, which they can't change): proTES applies `api_path` when it
sends the request, and [task provenance](operations.md#task-provenance) doesn't
show it.

Service registries list the URL of each TES instance, not its API path, so
proTES takes the URL that the registry lists as the base of the TES API of
a discovered TES instance: if `GET <url>/service-info` answers with the
service info of a TES service (`type.group` `org.ga4gh`, `type.artifact`
`tes`), the TES API is at the URL itself (e.g., TESP listed as
`https://tesp.example.org/v1`). If the answer settles that no TES service
info is there (`404` or `410`, or the service info of another service),
proTES appends the default API path. FOCA removes the default API path from
the URLs that registries list, so for a TES instance listed as
`https://tes.example.org/ga4gh/tes/v1`, proTES first tries
`https://tes.example.org/service-info`, then uses the default path.

The service info is read as for [TES versions](#tes-versions): without
credentials unless `custom.tes.probe_with_token` is set, within 5 seconds,
and the path is cached per process for `custom.tes.version_cache_ttl`
seconds. A TES instance that refuses anonymous reads at its URL (`401` or
`403`) gets the default path, with a hint in the logs, and is asked again
after 60 seconds. Other answers (e.g., redirects, `429`, server errors, or
JSON that isn't a service info) and failed requests leave the path unknown.
While a refusal or an unknown answer follows a path that was settled
before (by a TES service info, `404` or `410`, or another service info),
proTES keeps that path, and asks again every 60 seconds; a default path
taken after a refusal is not kept this way. Otherwise, while the path is
unknown, proTES sends no request to the TES instance, and caches nothing,
so that tasks
go to other TES instances, and tracking tries again with its next poll (a
request to a wrong path could make a task count as gone). The version of a
discovered TES instance is then read below the path found, so its first
read takes two requests. A URL that is also listed as an entry of its own
uses the settings of that entry, including its `api_path`.

### TES versions

proTES serves TES 1.1, and relays tasks to TES instances of TES 1.0 and 1.1
(see [TES versions](tes-versions.md)). It needs to know which one each TES
instance implements: the label `tes.version` of its entry (`"1.0"` or `"1.1"`;
other versions make the app refuse to start) wins; otherwise, proTES reads
`GET <url>/ga4gh/tes/v1/service-info` (or below its `api_path`, or that
found for a discovered TES instance; see [API paths](#api-paths)), without
credentials (see below), with timeouts of 5 seconds (to connect and to
answer), and caches the result per process for
`custom.tes.version_cache_ttl` seconds (default: 3600; failures for 60
seconds). It reads `type.version` leniently (e.g., `TES`, `1.0`, `v1.1.0`), if
`type.group` is `org.ga4gh` and `type.artifact` is `tes`. A TES instance that
reports no version that proTES supports, or never answered, counts as TES 1.0
(with a warning in the logs); one that stops answering keeps the version read
before.

The service info is read when a task needs the version: for the TES instance
that a task goes to, and, for the first task with fields that TES 1.0 can't
express, from all TES instances of the list without a label, also from those
that the task doesn't go to. The service info is public in the TES
specification, so proTES reads it anonymously by default
(`custom.tes.probe_with_token: false`): TES instances that tasks don't go to
don't get proTES' access token, and an outage of the identity provider
doesn't make TES instances of TES 1.1 count as TES 1.0. A TES instance that
refuses anonymous reads (`401` or `403`, logged with a hint) counts as TES
1.0: set its label `tes.version` (preferred), or set
`custom.tes.probe_with_token: true`, so that proTES reads service infos with
the credentials of each TES instance (its access token, or Basic
credentials). With it, every TES instance without a label gets proTES' token
when such a task arrives, also those that the task isn't routed to (e.g.,
all verified TES instances of a registry; see [Credentials of TES
instances](access-control.md#credentials-of-tes-instances)), and failing to
get a token makes those whose version wasn't read before count as TES 1.0
until the next read.

**Set the label for Funnel before 0.12.3:** Funnel implements TES 1.1, and
version 0.12.3 reports it (type `org.ga4gh`, `tes`, `1.1.0`), but version
0.11.14 reports an empty `type.version` (`""`), so it would count as TES 1.0:

```yaml
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://funnel.example.org
        auth: none
        labels:
          tes.version: "1.1"
```

The label of a registry entry applies to all TES instances that it discovers.
To tell the versions in a registry apart, use one entry per version, with its
`versions` and label (`versions: ["1.1"]` and `tes.version: "1.1"`).

### Notes on TES implementations

What was seen when running these versions through proTES; other versions may
behave differently. CI runs tasks on real instances of Funnel 0.12.3 and
TESP-API 0.2.0 with Pulsar, and of TESK with tesk-api 1.1.0 (see
[Testing](development.md)). A deployment on Rahti, CSC's OpenShift, was not
tested; [Testing a deployment](development.md#testing-a-deployment) has the
checks to run on one.

[TESP-API](https://github.com/CESNET/tesp-api) 0.2.0:

- It serves the TES API at `/v1`, so set `api_path: /v1`, unless a proxy in
  front of it rewrites the path (see [API paths](#api-paths)).
- It reports TES 1.0.0, so don't set the label `tes.version` to `"1.1"`. A task
  with fields of TES 1.1 only gets `400 Bad Request` if no allowed TES instance
  implements TES 1.1, e.g., if TESP-API is the only one.
- With Pulsar 0.15 or later, it was seen returning empty stdout and stderr;
  Pulsar 0.14.16 works.
- It reports a task as `CANCELED` while its container kept running for minutes;
  check on the Pulsar side whether it has stopped.
- It writes one executor log for all executors of a task.

[Funnel](https://github.com/calypr/funnel) 0.12.3:

- It was seen reporting TES 1.1.0 in its service info, so it needs no label
  `tes.version` (see [TES versions](#tes-versions)); CI runs it both with and
  without the label (see [Testing](development.md)).
- With `ignore_error: true` on an executor that fails, it runs the next
  executors and ends the task in `COMPLETE`.
- It drops keys of `backend_parameters` that it doesn't list in its service
  info (`tesResources_backend_parameters`) from tasks with
  `backend_parameters_strict: false`, as TES 1.1 allows; clients still get the
  task back from proTES as they sent it.

Funnel 0.11.14, tested with proTES 0.5.0 and 0.6.0:

- It reports an empty `type.version`, so set the label `tes.version: "1.1"`.
- The executor log of a canceled task shows `exit_code: 0`.
- With `ignore_error: true` on an executor that fails, it runs the next
  executors, but ends the task in `EXECUTOR_ERROR`, not `COMPLETE`. Upgrade
  to 0.12.3 or later for such tasks.

[TESK](https://github.com/elixir-cloud-aai/TESK) with tesk-api 1.1.0, the
taskmaster and filer v0.10.6, on Kubernetes 1.36:

- With the default of its chart, the taskmaster v0.10.2, all tasks end in
  `SYSTEM_ERROR` on Kubernetes 1.31 or later (system log: `Cancelling taskmaster:
  Got status Error`). Set `tesk.taskmaster_image_version` and
  `tesk.taskmaster_filer_image_version` to v0.10.6, or later, in the chart's
  values.
- It reports TES 1.0, so don't set the label `tes.version` to `"1.1"`. It rejects
  fields it doesn't know with `400 Bad Request`, which proTES avoids by adapting
  tasks (see [TES versions](tes-versions.md)).
- An executor's log has both streams in `stdout`, and no `stderr`.
- It refuses to cancel a task that has finished, with `400 Bad Request`; proTES
  answers such cancels with `200 OK`, and leaves the task as it is.
- A task canceled before its taskmaster's pod runs is reported as `CANCELED`,
  but keeps running: its executor runs to its end, using its resources. A task
  canceled later stops. Neither proTES nor clients can tell from the state.
- Tasks with inputs, outputs or volumes need a default storage class, or the
  chart's value `storageClass`.

In general, if no selected TES instance accepts a task, clients get a generic
`503 Service Unavailable`, without the reason of the refusal.

### Discovery in a service registry

proTES can also take TES instances from service registries implementing the
[GA4GH Service Registry
API](https://github.com/ga4gh-discovery/ga4gh-service-registry) with a list
of verified services, such as the [Cloud
Registry](https://github.com/elixir-cloud-aai/cloud-registry)
(`GET /verifications`), so that new TES instances are used once they are
registered and verified, without config change. Each registry is an entry
of `custom.tes.backends` (`type: ga4gh-service-registry`), whose TES
instances take its place in the list; there is no default registry. With
registries, proTES needs its public URL (`server.public_url`, e.g.,
`https://tes.example.org/`; the Helm chart sets it from `hostName` or
`publicUrl`), and refuses to start without it: TES instances at or below it
are never used, so that proTES never forwards tasks to itself:

```yaml
server:
  public_url: https://protes.example.org/
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://tes.example.org
      - type: ga4gh-service-registry
        # Identifier, e.g., in the service info (`group` of its TES
        # instances); by default, the normalized URL
        id: cloud-registry
        url: https://registry.example.org/ga4gh/registry/v1
        # Seconds between reads of the registry
        refresh_interval: 300
        # Seconds that the last good list is used while the registry can't
        # be read; at least `refresh_interval`
        max_stale: 3600
        # Timeout of each request to the registry, in seconds
        timeout: 10
        # Accepted TES versions, as prefixes (`1.1` accepts `1.1.0`)
        versions: ["1.0", "1.1"]
        # Accepted environments of services (`environment`); any if empty
        environments: [prod]
        # How proTES authenticates at discovered TES instances:
        # `client_credentials` (the default) or `none`
        auth: client_credentials
        # For local setups only: TES instances at private addresses (their
        # URLs must still be `https`)
        allow_private_addresses: false
        # For a registry at a private address, e.g., in the same cluster
        registry_allow_private_addresses: false
        # Labels of the discovered TES instances, e.g., for routing
        labels: {source: cloud-registry}
    client_credentials:
      issuer: https://login.example.org/oidc/
```

Anyone who can register services in the registry could point an entry at a
malicious or internal endpoint, which would receive tasks, their input
locations and proTES' access token. So the registry, set by the deployment,
is the trust anchor, and a registered service is only used if all of the
following hold:

- the registry lists it as verified by an admin, with its current
  identifier, URL and API type (`type.group`, `type.artifact`, and the
  version offered);
- it is a TES service (`org.ga4gh` `tes`) of one of the `versions`, and, if
  `environments` is set, of one of the `environments`;
- its URL is a valid `https` URL without user info (`url_schemes` of the
  entry applies to the registry only); the URL is normalized, and the API
  path `/ga4gh/tes/v1`, which registries list as part of the URL, is
  removed (proTES then finds the API path of the TES instance at that URL;
  see [API paths](#api-paths));
- its host has public addresses only: hosts with private, loopback,
  link-local or other special-purpose addresses (e.g., `10.0.0.1`,
  `localhost`, `169.254.169.254`) are rejected, unless
  `allow_private_addresses` is set. Hosts are checked on discovery, and
  the address of each connection to the TES instance is checked again
  (FOCA's guarded HTTP client), so that hosts whose addresses change after
  the check (DNS rebinding) can't lead requests, and proTES's credentials,
  to such addresses. Requests to discovered TES instances don't follow
  redirects, and their responses are limited as those of configured TES
  instances are (`custom.tes.max_response_size`).

The registry is read without access token (its reads are public), over a
URL with a scheme of the entry's `url_schemes`, at public addresses (unless
`registry_allow_private_addresses`, e.g., for a registry in the same
cluster); redirects are not followed. Each process (each Gunicorn worker and
Celery worker) reads it every `refresh_interval` seconds, and keeps the
last good list while the registry is unreachable or returns invalid
responses, for `max_stale` seconds at most, retrying after 30 seconds at
most; then, and before the first good read, the registry yields no TES
instances. TES instances whose hosts can't be resolved for the time being
are kept for one more read. A process that doesn't know the TES instance
of a task (e.g., the Celery worker, for a TES instance that the app's
process discovered first) reads the registries again, at most every 10
seconds; polls of a task whose TES instance is unknown fail like other
polls, so its tracking goes on (see `post_task.polling` in
[Tasks](#tasks)). A service whose verification is removed is no longer
used once the registry has been read again. Changes of the list and
rejected verified services are logged. TES instances listed in
`custom.tes.backends` themselves keep their settings, also where a
registry lists them, too; a TES instance listed by several registries is
used once, at its first place. Discovered TES instances get the
credentials of the registry entry's `auth` (see [Credentials of TES
instances](access-control.md#credentials-of-tes-instances)); tasks of a TES
instance that is no longer discovered can't be tracked or canceled (their
state becomes `UNKNOWN` until it is discovered again).

## Plugins

The top-level section `plugins` sets which installed plugins are available
(`available`) and the pipeline of steps that process each new task
(`pipeline`): by default, random order, with the routing plugin that the
image contains (no third-party location lookups). Distance routing, which
looks up the hosts of inputs at DB-IP and Nominatim or in a GeoIP database,
is an opt-in; see [Plugins](plugins.md#pipelines).
The former `custom.middlewares` is refused: the app stops with a hint to
`plugins` (see [Upgrading](upgrading.md#plugins)).

The provenance of each task's pipeline run is stored with the task (see
[Task provenance](operations.md#task-provenance)). Records of failed runs
(e.g., tasks rejected by a plugin, which leave no task) are stored only
with `plugins.provenance.record_failed: true`, for 30 days; headers whose
values must not be guessable from the hashes of changes (e.g., API keys
that plugins add for TES instances) belong in
`plugins.provenance.secret_headers`:

```yaml
plugins:
  provenance:
    record_failed: true
    secret_headers: [X-Api-Key]
```

Plugins keep their data in FOCA's key-value store (see [Plugin
store](operations.md#plugin-store)), in the database of the tasks. Its
settings apply to all plugins (`plugins.defaults.store`), or to one plugin
(`plugins.available.<id>.store`, replacing the defaults key by key):
whether the plugin keeps its entries in the database (`enabled`, by
default `true`; with `false`, each process keeps them in memory, so
counters and caches hold per process only), the maximum size of values
(`max_entry_size`, bytes of compact JSON, by default 4096) and the maximum
time to live of entries (`max_ttl`, in seconds, by default 30 days, at
most 10 years); set the same `enabled` for all plugins of a distribution,
which share their entries. For example:

```yaml
plugins:
  defaults:
    store:
      max_ttl: 86400
  available:
    tes.routing.distance:
      store:
        enabled: false
```

## Tasks

`custom.controllers` sets how tasks are created and listed:

- `post_task.db.insert_attempts`: how often proTES tries to store a new task
  with a newly generated identifier if the identifier exists already;
- `post_task.task_id`: the characters (`charset`) and the length (`length`)
  of task identifiers;
- `post_task.polling`: how the Celery workers track a forwarded task: they
  poll the TES instance for the task's state every `wait` seconds (default
  3) until the task has finished. After `attempts` consecutive failed polls
  (default 100; a successful poll resets the count), the task's state is
  `UNKNOWN`, and the polls back off, doubling the interval up to `max_wait`
  seconds (default 600), until the TES instance answers again: failed polls
  never make a task fail, as it may still be running (see [Task
  tracking](operations.md#task-tracking)). `wait` and `max_wait` must be
  below 1800 seconds, RabbitMQ's default acknowledgement timeout
  (`consumer_timeout`), as scheduled polls are acknowledged when they run.
  `max_cancel_time` (default 3600, at most 31536000, i.e., a year) is the
  number of seconds after a cancel at which a task still being canceled
  (`CANCELING`: its TES instance reported neither a final state nor that it
  no longer has the task) is set to `SYSTEM_ERROR`, no longer tracked, and
  logged with a warning, e.g., if its TES instance was decommissioned or keeps
  failing; the task may still run there. The next poll after that time ends
  it, so with failing polls, up to `max_wait` seconds later;
- `post_task.url_credentials`: what happens to tasks with credentials in the
  URLs of their inputs or outputs (`https://user:password@host/...`): they
  are rejected with `400 Bad Request` (`reject`, the default), or the
  credentials are removed (`strip`); credentials are never stored or
  forwarded, and those that plugins add are always removed;
- `post_task.max_inline_content`: the maximum size of the inline content
  of each input of a task (`inputs[].content`), in bytes of UTF-8 (default
  131072, i.e., 128 KiB, the minimum that the TES specification recommends
  per input; all contents together are limited by the [request size
  limit](#request-size-limit)); tasks with larger contents are rejected
  with `413 Content Too Large`, also if plugins added the contents;
- `post_task.max_log_size`: the maximum size of the logs of a TES instance
  that are stored per task, i.e., of the standard output and error of executors
  and the system logs together, in bytes of UTF-8 (default 1048576, i.e., 1 MiB;
  at most 8 MiB, as documents in MongoDB have at most 16 MB, with the task as
  received and as forwarded). Small logs are kept; what they leave is shared
  among the longer ones, which keep their end, where errors usually are. proTES
  gets them once when a task has finished (`GET /tasks/{id}?view=FULL` at the TES
  instance), and holds them in memory in the Celery worker meanwhile: a task
  whose response is larger than `custom.tes.max_response_size` (10 MiB by
  default), or takes longer than `custom.tes.timeout` as a whole, stays without
  them, with an error in the logs. A response takes a worker process about 7
  times its size, so that two of 64 MiB at once would exceed the 512 MiB of the
  [Helm chart](deployment.md)'s worker: raise its memory with the limit (if a
  worker process runs out of memory, the broker delivers the poll again, and
  proTES finishes the task without its logs). If a task would still exceed the
  limit of MongoDB (e.g., with very many short logs), proTES stores it without
  these logs, or if need be also without the executors' logs and the outputs, and
  logs an error. Their owners can read them in the view `FULL`, so
  TES instances should keep secrets out of them. With `0`, proTES neither gets
  nor stores them;
- `post_task.max_create_time`: the number of seconds (default 3600, at
  most 31536000, i.e., a year) after which tasks that are still being
  created, i.e., in state `UNKNOWN` without identifier at a TES instance
  (e.g., because proTES stopped while forwarding them, as in a rolling
  upgrade), are set to `SYSTEM_ERROR` by the Celery workers (see [Task
  tracking](operations.md#task-tracking)).
  It must be longer than creating a task may take: the plugins, the
  requests to MongoDB, the connect timeout (currently 5 seconds) of each TES
  instance that is tried, the token requests
  (`client_credentials.timeout`), and `tes.timeout`, for the TES instance
  that accepts the task;
- `list_tasks.default_page_size`: the number of tasks per page of
  `GET /tasks` if the request sets no `page_size` (default 256, as in the
  TES specification);
- `list_tasks.max_page_size`: the maximum number of tasks per page; larger
  `page_size`s are reduced to it (default and upper limit 2047: the TES
  specification requires fewer than 2048);
- `list_tasks.max_page_size_full`: the maximum number of tasks per page of
  the view `FULL` (default 64; 6 in the [Helm chart](deployment.md), with its
  memory limit of 256 MiB), whose tasks may be large (e.g., with inline
  contents of inputs and logs); larger `page_size`s, and the default page
  size, are reduced to it. It limits the number of tasks, not the size of
  a page (that is `max_page_bytes_full`): a task may be as large as the
  [request size limit](#request-size-limit) (1 MiB by default), plus its logs
  (`post_task.max_log_size`, 1 MiB by default), so a page of the view `FULL`
  may be about `max_page_size_full` times that size (128 MiB with the app's
  defaults). A Gunicorn worker holds about 3 times the size of a page in
  memory while serving it, as the documents, their JSON and the encoded body
  exist at the same time, for each request that it serves at once (one per
  thread). Set it and the memory limit together: about 36 MiB per request with
  6, and about 384 MiB with 64, on top of about 90 MiB of the app; more if logs
  hold text that JSON escapes (up to 6 times its size, e.g., control
  characters), but never much more than `max_page_bytes_full` allows;
- `list_tasks.max_page_bytes_full`: the maximum size of the tasks of a page
  of the view `FULL`, in bytes of the response body (indented JSON; default
  10485760, i.e., 10 MiB; 4 MiB in the [Helm chart](deployment.md)),
  whatever their number: a page ends before the task
  that would exceed it, and its `next_page_token` continues with that task.
  A page always has at least one task, which may be larger (up to the size
  of a stored task). It bounds the response, not the memory: a request of a
  page of the view `FULL` takes about 3 times this size in memory, plus one
  task (about 30 MiB by default), for tasks with large texts (e.g., logs,
  also with text that JSON escapes), and up to about 7 times for tasks with
  many small fields (e.g., thousands of inputs or tags); lower it with the
  memory limit;
- `post_task.limits`: limits of the tasks per user, off by default (see [Task
  limits](#task-limits)).

### Task limits

Deployments can limit the number of tasks of each user, so that a single user
can't occupy the TES instances, the Celery workers or the database. All limits
are off by default (`null`), as many deployments must keep all tasks, e.g., for
accounting and audits:

```yaml
custom:
  controllers:
    post_task:
      limits:
        max_unfinished: 100
        max_stored: 10000
        exempt_roles: [admin]
```

- `max_unfinished`: the maximum number of unfinished tasks per user, i.e., in
  any state but `COMPLETE`, `CANCELED`, `EXECUTOR_ERROR`, `SYSTEM_ERROR` and
  `PREEMPTED` (also `UNKNOWN`, e.g., tasks being created, and `CANCELING`);
- `max_stored`: the maximum number of stored tasks per user, finished or not.
  Users can't delete tasks: unless finished tasks are deleted (by an operator,
  see [Task owners](operations.md#task-owners)), a user who reaches it can't
  create tasks any more, so set it well above the number of tasks a user
  creates over the time that tasks are kept;
- `exempt_roles`: roles whose users are not limited (default: none), e.g.,
  `admin` for admins who also have the role `user` (see [Access
  control](access-control.md)). The roles are those that access control
  applies to the request: opt-in roles (`access_control.opt_in_roles`) count
  only if the request activates them.

A new task of a user who has as many tasks as a limit allows is rejected with
`429 Too Many Requests`, before the plugins run; its problem details name the
limit (`detail`, e.g., ``Limit of unfinished tasks per user reached: at most
100 (`max_unfinished`)``), and it is neither stored nor forwarded. Tasks count
for their owner (`user_id`); tasks of earlier versions without owner don't
count. The limits are soft: proTES counts the tasks of the user, then stores
the new one, so concurrent requests of the same user can each pass the check
and exceed a limit by up to one task per concurrent request. Each check counts
the tasks of the user up to the limit, using the index on owner and state of
the tasks (`db` in `config.yaml`).

## Retention of finished tasks

Deployments can delete finished tasks some days after they finished, so that
the database and the rules of access control stay bounded: each task has two
owner roles, and their number slows down every request. Retention is off by
default (`finished_days: null`), as many deployments must keep all tasks,
e.g., for accounting and audits:

```yaml
custom:
  retention:
    finished_days: 90
    interval: 3600
    batch_size: 500
```

- `finished_days`: the number of days after which finished tasks
  (`COMPLETE`, `CANCELED`, `EXECUTOR_ERROR`, `SYSTEM_ERROR` and
  `PREEMPTED`) are deleted, counted from the end time of their last log
  (`logs[].end_time`), or, without one, from their creation
  (`creation_time`), but at the earliest from when their tracking stored
  their final state (the last poll, `tracking.polled_at`, or recovery,
  `tracking.finished_at`, e.g., for tasks whose creation was cut off): the
  log of proTES ends when a task is forwarded, so tasks that ran for longer
  than `finished_days` are kept that long after they finished, too. Tasks
  tracked by proTES before version 0.3.0 have no last poll, so their time
  counts from when they were forwarded, and that of tasks failed by
  recovery in versions up to 0.5.0 from when they were forwarded or created.
  Unfinished tasks are never deleted;
- `interval`: seconds between runs of the purge in each Celery worker
  (default 3600); the first run is when the worker starts;
- `batch_size`: the maximum number of tasks deleted per run (default 500),
  the oldest first, so that a first run on a large database doesn't hold a
  worker for long. Each deleted task is 2 changes of the rules of access
  control, and app instances that miss more than the last 100 changes (FOCA)
  load all rules again on their next request: with many tasks to delete
  (e.g., on the first run), a smaller `batch_size` (at most 50) avoids
  that, at the cost of more runs.

The Celery workers delete the tasks:

- **Several workers:** each worker purges every `interval` seconds, so with
  several workers, the purge runs that many times more often and deletes up
  to that many times `batch_size` tasks per `interval`. Runs of several
  workers don't interfere, as a task is only replaced while it is still
  finished and not yet deleted; runs at the same time (e.g., when workers
  start together) may remove the same owner roles more than once, which
  adds changes of the rules (see `batch_size`).
- **Deletion:** for each task, the workers remove its owner roles, on the
  default path of the TES API (`/ga4gh/tes/v1/tasks/...`), as the policies
  of `config.yaml` grant them. Then they replace its document by a
  tombstone that keeps only the task's identifier (`task.id`) and the time
  of the deletion (`deleted_at`), so that the identifier is never used for
  another task. The API treats tombstones as tasks that don't exist
  (`404 Not Found`, never listed).
- **What is kept:** tasks are deleted, not archived: keep a backup of the
  database if you need them later. The provenance of a deleted task goes
  with it; the plugin builds and pipelines that tasks refer to are kept.
- **Cost:** each run reads only finished tasks stored more than
  `finished_days` days ago, oldest first, by an index (`task.state`,
  `_id`) that leaves out unfinished tasks and tombstones.

## Accounting of resource usage

Deployments can record which resources the tasks of each user used, and
give admins the totals per user (see [Usage](api.md#usage)). Accounting is
off by default:

```yaml
custom:
  accounting:
    enabled: true
```

With `enabled: true`, the Celery workers record each task that finishes at
its TES instance (any final state that the TES instance reports:
`COMPLETE`, `EXECUTOR_ERROR`, `SYSTEM_ERROR`, `CANCELED` or `PREEMPTED`),
right before they store its final state:

- **Run time:** the time between the start and the end of the task in the
  logs of its TES instance (`logs[].start_time` and `end_time`, or else the
  earliest start and the latest end of its executors), summed up over its
  attempts (task logs). Tasks whose TES instance logged no start or no end
  count 0 seconds, and are counted as `untimed_tasks`.
- **Resources:** the CPU cores, RAM and disk that the task requested
  (`resources.cpu_cores`, `ram_gb` and `disk_gb`, as proTES stored the
  task), each times the run time (`cpu_core_seconds`, `ram_gb_seconds`,
  `disk_gb_seconds`). TES instances don't report the resources that tasks
  consumed, so these are allocations, not measurements; resources that a
  task doesn't request (the TES instance applies its defaults) count 0.
- **Not recorded:** tasks that never reached a TES instance, tasks whose
  final state proTES set itself (e.g., `SYSTEM_ERROR` after
  `max_cancel_time` or `max_create_time`, or `CANCELED` when the TES
  instance no longer has a canceled task), tasks that finished while
  accounting was off, and tasks whose record failed, also by an error
  that MongoDB reported (e.g., a refused write or a timeout; logged as
  `Usage of the task not recorded`). If MongoDB can't be reached, the poll
  is retried until it can, and the task is recorded then.

Each record (collection `usage` of the database of the tasks) holds the
owner of the task (`user_id`, as in the task document, see [Task
owners](operations.md#task-owners)), the task identifier, the URL of the TES
instance, the final state, the times, the requested resources and the
products above; nothing else of the task. The records are kept when tasks
are deleted by the [retention of finished
tasks](#retention-of-finished-tasks), until operators delete them (see
[Usage records](operations.md#usage-records)). Turning accounting off stops
the recording; stored records are still served.

## Security

The TES API requires access tokens of the trusted issuers
(`security.auth`), and roles from groups (`security.access_control`); the
issuers, the audience and the groups are deployment settings without
defaults. See [Access control](access-control.md).

## Request size limit

Request bodies are limited to `server.max_content_length` bytes (default:
1048576, i.e., 1 MiB), by FOCA: larger requests, also chunked ones, are
rejected with `413 Content Too Large` before they are authenticated and
parsed. The inline content of each input (`inputs[].content`) has a lower
limit of its own (`custom.controllers.post_task.max_inline_content`, see
[Tasks](#tasks)), as contents are stored with each task: raise both if
tasks with large inline contents must be accepted. Prefer inputs with URLs.

## Rate limits

proTES has no rate limits of its own. In production, limit the rate of
requests per client at the reverse proxy or ingress controller in front of
it, so that single clients can't overload the app, its database or the TES
instances (see the Helm chart's [README](deployment.md)).

## Server

The image serves the app with [Gunicorn](https://gunicorn.org/), with FOCA's
settings for containers (`foca.gunicorn_config`, see [Running in
production](https://foca.readthedocs.io/en/latest/deployment/#running-in-production)
in FOCA's docs): it listens on port 8080, with one worker process with four
threads. Override settings with the environment variable
`GUNICORN_CMD_ARGS` (e.g., `--workers 2 --threads 8`), but not `--bind`: the
image's health check and the Helm chart expect port 8080. Behind a reverse
proxy, set its addresses in `FORWARDED_ALLOW_IPS`, so that Gunicorn trusts
its `X-Forwarded-*` headers. `server.host` and `server.port` of the config
only apply to Flask's development server (`python -m pro_tes.app`).
`server.debug` must stay off outside local development: Flask's debugger
allows executing arbitrary code.

The Celery worker runs from the same image, with the command `celery --app
pro_tes.celery_worker worker`; it connects to the broker at
`CELERY_BROKER_URL` (with the RabbitMQ credentials; see
[`docker-compose.yaml`](https://github.com/elixir-cloud-aai/protes/blob/dev/docker-compose.yaml)).

## Service info

`GET /service-info` is served by FOCA from section `service_info` of the
config, according to the GA4GH service info schema (validated on startup).
Its `version` is the version of the installed package (`service_info.package`).
The base config describes the app, not a deployment: its `id`
(`org.example.protes`) and `organization` are placeholders with reserved
example domains, which the app warns of on startup. Deployments set their
own `id`, `name`, `description`, `organization`, `contactUrl` and
`environment`, and `storage` (the storage locations their TES instances can
access), in `service_info.service`. The service info also lists the URLs
of the TES instances that tasks may be forwarded to, configured and
discovered (`backends`, from `service_info.runtime_fields`), and what
clients may choose per task (`gateway`: `plugins_url`, the path of
`GET /plugins`, whether clients may choose pipelines and TES instances,
and the capabilities of the plugins, without their identifiers or
versions; see [Choices of clients](plugins.md#choices-of-clients)).

!!! warning "By default, the service info lists the TES instances publicly"

    `GET /service-info` needs no token, so anyone can read the identifier,
    URL, source, group and labels of each TES instance, also of those at
    internal URLs and of those discovered in registries.

To leave out `backends` and keep `gateway`, set this in an overlay (or under
`config` in the values of the Helm chart):

```yaml
custom:
  service_info:
    list_backends: false
```

This hides the TES instances from anonymous callers only:

- Any user who can create tasks still gets the identifiers of all TES
  instances (by default, their URLs) from the provenance of one task
  (`route.candidates_in` and `route.candidates_out`, see
  [Provenance](api.md#provenance)), and the URLs of those tried from its tags
  (`gateway.backend` and `gateway.route`, see [Gateway
  tags](api.md#gateway-tags)). Setting `plugins.client_backends.enabled` to
  `false` doesn't change this. For configured TES instances, setting `id`
  keeps the URL out of the candidates, not out of the tags.
- Clients that choose TES instances with the tag `gateway.backends` (see
  [Choices of clients](plugins.md#choices-of-clients)) no longer find their
  identifiers in the service info.
- Registries that proTES discovers TES instances in may publish them anyway.

To hide `backends` and `gateway` together, set `service_info.runtime_fields`
to `null` in an overlay.
