# Upgrading

The [release notes](changelog.md) list the breaking changes of each release.
This page describes how existing setups and data are affected, and what to
change: first for upgrades from 0.3.0 ([To version 0.4.0](#to-version-040)),
then for deployments from before 0.3.0, i.e., images tagged by date, on FOCA
0.12 ([From versions before 0.3.0](#from-versions-before-030)), which apply
the steps of both parts.

## To version 0.4.0

Check each item; the sections below have the details.

1. **Image and chart:** deploy the image and the Helm chart of this release
   together; both are built on FOCA 0.19 (see [FOCA 0.18 and
   0.19](#foca-018-and-019) for the config changes of FOCA, and [Image and
   chart](#image-and-chart)).
2. **Plugins instead of middlewares:** `custom.middlewares` is refused at
   startup; remove it and configure the top-level section `plugins` (see
   [Plugins](#plugins)). Custom middlewares must be ported to FOCA plugins.
3. **Routing default:** tasks no longer go to the TES instance closest to
   their input data (with random order as the fallback), but to the TES
   instances in the order of `custom.tes.backends`, to the next one only if
   one doesn't accept a task (FOCA's `foca.routing.ordered`). With several
   TES instances and the default settings, all tasks go to the first one,
   without an error or warning. See [Routing](#routing) for how to keep
   distance-based routing.
4. **TES instances:** `custom.tes.service_list` and `custom.tes.discovery`
   are replaced by `custom.tes.backends` (in the Helm chart, by
   `tes.backends`); with a service registry, set the public URL of proTES
   (see [TES instance list](#tes-instance-list)).
5. **Access control rules:** custom lists of
   `security.access_control.policies` must add the new rules
   `users-list-plugins`, `users-get-plugins`, `owners-get-provenance` and
   `admins-get-provenance`, or users get `403` (see [Task
   provenance](#task-provenance)); overlays that change rules by name keep
   them.
6. **Database:** proTES uses new collections in the database of the tasks:
   `plugin_builds`, `pipelines` and `failed_runs` (see [Task
   provenance](#task-provenance)) and `plugin_store` (see [Plugin
   store](#plugin-store)), and creates indexes on startup: the database
   user of proTES needs the right `createIndex` (part of `readWrite`).
   Custom configs used instead of the base config must declare the new
   collections. The collection `middlewares` is no longer used and can be
   dropped (see [Middleware management API](#middleware-management-api)).
7. **Clients:** tasks that no TES instance accepts fail with `503` (was
   `500`), requests that already passed proTES with `508 Loop Detected`
   (if loop detection is configured), and tasks with tags under `gateway.`
   other than `gateway.backends` (and `gateway.pipeline`, if enabled) with
   `400`, which affects clients that submit fetched tasks again (see
   [Plugins](#plugins) and [Task provenance](#task-provenance)). The plugin
   listing has new required fields (see [Plugin store](#plugin-store)).
8. **Forwards in task logs:** `logs[].metadata.forwarded_to` is removed,
   without a deprecation period: tasks are served without it, also those
   stored by earlier versions. Clients read the TES instance and the remote
   identifier of a task from its gateway tags `gateway.backend` and
   `gateway.backend_task_id` instead. `custom.storeLogs` is refused at
   startup, also by the Helm chart: remove it (see [Forwards in task
   logs](#forwards-in-task-logs)).
9. **Code that uses proTES as a library** (e.g., custom controllers):
   `TaskRuns.create_task()`, `TesClient` and the exceptions changed (see
   [Plugins](#plugins)), the TES instances are read with `get_backends()`
   (see [TES instance list](#tes-instance-list)), and the models
   `TesForward` and `Metadata` are removed (see [Forwards in task
   logs](#forwards-in-task-logs)).

### FOCA 0.18 and 0.19

proTES runs on FOCA 0.19 (was 0.17). Custom configs must apply the config
changes of FOCA 0.18 and 0.19 (see the FOCA release notes and migration
guides). With FOCA 0.18, settings and checks that proTES had of its own are
FOCA's:

- `custom.access_control.require_admin` moved to
  `security.access_control.require_admin` (`true` in the base config, as
  before); the old key is refused, also by the Helm chart.
- FOCA refuses group mappings without a group claim
  (`security.access_control.claim_groups`) with a `ValueError` (before, a
  warning, and proTES then refused to start without admins), and counts
  configured rules (`security.access_control.policies`) that grant the role
  `admin` as admins, too.
- FOCA warns of values of the service info in reserved example domains
  (e.g., `id`, `organization.url`), and no longer of `organization.name`.
- Requests rejected by the validation of the API specification (e.g., a task
  with an invalid field) get the reason as `detail` (e.g., `'none' is not of
  type 'array' - 'executors'`) instead of `The request is malformed.`
  (`exceptions.validation_details`).
- Rules of access control changed in the database directly (e.g., with
  `mongosh`) apply only after a restart of proTES (see [Task
  owners](operations.md#task-owners)). Instances of earlier versions don't
  record their changes of rules, and proTES adds owner roles with each new
  task: once all instances run this version, restart them once more (e.g.,
  `kubectl rollout restart deployment/<app>` with the Helm chart, `<app>` as
  in its README, Names and labels), so that they apply the owner roles of
  tasks created during the rolling update; until then, owners of such tasks
  may get `404` from instances of this version. To avoid this window, stop
  the instances of the earlier version before starting those of this version
  (e.g., scale the app's Deployment to 0 first), at the cost of a short
  downtime (see FOCA's [migration guide to
  0.18](https://foca.readthedocs.io/en/latest/migration-0.18/)).
- With several trusted issuers (`security.auth.issuers`), subjects are now
  namespaced by issuer (`<issuer>|<sub>`), so that equal subjects of
  different issuers are different users. Owners of stored tasks
  (`user_id`, owner roles) and roles granted to users via the permissions
  API are stored with bare subjects and then no longer apply: list the
  issuer whose users own the stored tasks with `namespace: ""` (e.g.,
  `issuers: [{issuer: "https://login.example.org/oidc/", namespace: ""},
  "https://other.example.org/"]`, also in the Helm chart's
  `config.security.auth.issuers`), or rewrite
  the stored subjects to the namespaced form, both the owner roles
  (`policy_rules`, `user:<issuer>|<sub>`) and the owners of the tasks
  (`user_id` of the documents in `tasks`, `<issuer>|<sub>`): access depends
  on the owner roles, and operators find tasks by `user_id` (see FOCA's [migration
  guide](https://foca.readthedocs.io/en/latest/migration-0.18/#subjects-of-several-issuers)).
  The owners of tasks created since are stored in the namespaced form, too
  (see [Task owners](operations.md#task-owners)).
  Deployments with a single trusted issuer are not affected.

### TES instance list

TES instances are listed in `custom.tes.backends`, a typed backend list of
FOCA, in order (see [TES instances](configuration.md#tes-instances)): TES
instances (`type: ga4gh-tes`) and service registries whose verified TES
instances are used in their place (`type: ga4gh-service-registry`). It
replaces `custom.tes.service_list` and `custom.tes.discovery` (of 0.3.0), which stop
the app with a hint of the mapping: first the entries of `service_list`, in
order, each with `type: ga4gh-tes` (and its `url`, `auth` and `basic`),
then the registry of `discovery`, if any, as one entry with `type:
ga4gh-service-registry`, its `registry_url` as `url`, and its other
settings (`refresh_interval`, `timeout`, `versions`, `environments`,
`auth`, `allow_private_addresses`), which keeps the order of earlier
versions. For example, this config of 0.3.0:

```yaml
custom:
  tes:
    service_list:
      - url: https://tes.example.org
      - url: https://tes.example.com
        auth: none
    discovery:
      registry_url: https://registry.example.org/ga4gh/registry/v1
      environments: [prod]
    client_credentials:
      issuer: https://login.example.org/oidc/
```

becomes:

```yaml
server:
  public_url: https://protes.example.org/
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://tes.example.org
      - type: ga4gh-tes
        url: https://tes.example.com
        auth: none
      - type: ga4gh-service-registry
        url: https://registry.example.org/ga4gh/registry/v1
        environments: [prod]
    client_credentials:
      issuer: https://login.example.org/oidc/
```

With a registry, set the public URL of proTES (`server.public_url`; the Helm
chart sets it from `hostName` or `publicUrl`): proTES refuses to start
without it. TES instances at or below the public URL are never used, also
without registries, and proTES refuses to start if one of the configured TES
instances is (e.g., `https://<hostName>/funnel` behind the same ingress, if
proTES is served at `https://<hostName>/`: serve proTES under a host or path
prefix of its own, and set `publicUrl` to it, or move the TES instance to
another host). `url_schemes` is set per entry: entries with `http` URLs (for
local tests only) need `url_schemes: [https, http]`;
`custom.tes.url_schemes` applies to the identity provider only. A registry
whose last good read is older than `max_stale` seconds (default: 3600)
yields no TES instances until it can be read again (earlier versions kept
its last list indefinitely), and its own host must have public addresses,
unless `registry_allow_private_addresses` is set. The service info lists the
TES instances as objects (`backends`: `id`, `url`, `source`, `group` and
`labels` of each) instead of URLs.

### Plugins

FOCA's plugin engine replaces the middlewares of proTES (see
[Plugins](plugins.md)). The packages `pro_tes.middleware` and
`pro_tes.plugins` are removed, and with them the distance-based and random
task distribution and the dependency `geopy`.

- `custom.middlewares` is refused: the app stops with a hint to the
  top-level section `plugins`. Remove it from custom configs; set
  `plugins.pipeline` instead (see [Plugins](plugins.md)). The Helm chart
  sets the plugins from the values `plugins` (e.g., `plugins.pipeline`,
  with camelCase keys, see the chart's [README](deployment.md)) and refuses
  `config.custom.middlewares`.
- Tasks go to the TES instances in the order of `custom.tes.backends`
  by default, instead of by distance (see [Routing](#routing)).
- Tasks that no TES instance accepts fail with `503 Service Unavailable`
  (was `500 Internal Server Error`), also if no TES instance is configured,
  and if none accepted within `plugins.defaults.forward_timeout` (20 seconds
  in the base config, so that forwarding a task and getting its state fit
  into 60 seconds, apart from other work such as registry reads; see [TES
  instances](configuration.md#tes-instances));
  errors of the plugin engine have a `problem_type`. Tasks with tags under
  `gateway.` other than `gateway.backends` (and `gateway.pipeline`, if
  enabled) are rejected with `400 Bad Request`; TES instances get the tasks
  without these tags. If the public URL of proTES
  (`server.public_url`) or `plugins.gateway_id` is set (the Helm chart sets
  the former), requests to TES instances carry a `Via` header, and requests
  that already passed proTES are refused with `508 Loop Detected` (see
  [Plugins](plugins.md#how-a-task-is-processed)).
- Custom middlewares must be ported to FOCA plugins (see FOCA's
  documentation): installed packages with entry points of the group
  `foca.plugins`.
- Code that calls `TaskRuns.create_task()` directly (e.g., custom
  controllers) passes the inbound request and the caller
  (`inbound=foca.plugins.adapter.inbound_from_request(task)`,
  `principal=foca.plugins.adapter.current_principal()`), and gets the
  response body and headers (`(body, headers)`) instead of the body.
  `TesClient.create_task()` is replaced by `TesClient.forward()`, and the
  exceptions `NoTesInstancesAvailable`, `MiddlewareException`,
  `InvalidMiddleware`, `InputUriError`, `IPDistanceCalculationError` and
  `TesUriError` are removed (FOCA's `NoBackendAvailable` and other
  `GatewayError`s take their place).

Code that calls `TaskRuns` directly passes the request path of the tasks:
`create_task(task=…, collection=…, …)` and `list_tasks(collection=…, …)`;
`TaskRuns` no longer reads them from the request.

### Routing

0.3.0 sent each task to the TES instance closest to its input data
(middleware `TaskDistributionDistance`, which located TES instances and
inputs with DB-IP and Nominatim), with random order as the fallback
(`TaskDistributionRandom`). This version has no routing of its own: the
default pipeline is FOCA's ordered failover (`foca.routing.ordered`), the
TES instances in the order of `custom.tes.backends` (those of each registry
shuffled), the next one if a TES instance doesn't accept the task. proTES no
longer calls third-party services to locate TES instances and inputs.

To keep distance-based routing, deploy an image derived from that of this
release with the routing plugins of
[foca-plugins](https://foca-plugins.readthedocs.io/en/latest/), which are
published on PyPI as `foca-plugin-tes-routing-distance` (plugin
`tes.routing.distance`) and `foca-plugin-generic-routing-random` (plugin
`generic.routing.random`), see [Plugin images](plugin-images.md), and
enable them, e.g., in the values of the Helm chart. By default, the
distance plugin locates addresses offline, with a GeoIP database that the
derived image must contain; the online lookups of 0.3.0 (DB-IP and
Nominatim) are an opt-in (`provider: dbip`; see the plugin's
[README](https://github.com/elixir-cloud-aai/foca-plugins/tree/dev/plugins/tes/routing/distance)):

```yaml
protes:
  image: registry.example.org/protes-plugins@sha256:<digest>
  tag: ""
plugins:
  available:
    tes.routing.distance:
      distribution: foca-plugin-tes-routing-distance
      version: ">0.1.0"  # a release for FOCA 0.19; or `==` the version of plugins.txt
      config:
        # a GeoIP database added to the image (see the plugin's README)
        database: /opt/geoip/dbip-city-lite.mmdb
    generic.routing.random:
      distribution: foca-plugin-generic-routing-random
      version: ">0.1.0"
  pipeline:
    - id: route
      alternatives:
        - plugin: tes.routing.distance
        - plugin: generic.routing.random
```

!!! warning "Plugin releases for FOCA 0.19"

    The releases 0.1.0 of both plugins support FOCA 0.18 only
    (`foca>=0.18.0,<0.19`), so they can't be installed into the images of
    this release: `resolve.sh` of the recipe fails. Until releases for FOCA
    0.19 are published (see
    [foca-plugins#32](https://github.com/elixir-cloud-aai/foca-plugins/issues/32)),
    deployments that need distance-based routing stay on 0.3.0, or
    accept the ordered failover in the meantime (order
    `custom.tes.backends` by preference).

### Task provenance

New tasks have the provenance of their plugin pipeline in `provenance` of
their document (see [Task provenance](operations.md#task-provenance));
tasks stored by earlier versions have none, and are read, tracked and
canceled as before. proTES declares three collections in the database of the
tasks: `plugin_builds`, `pipelines` and `failed_runs`, whose TTL index on
`failed_at` (30 days) it creates on startup; MongoDB creates the
collections with their first document. Custom configs that are used
instead of the base config (not as overlays, which keep its collections)
must declare them, too. Records of failed runs are stored only with
`plugins.provenance.record_failed: true`; without it, `failed_runs` stays
empty.

Owners and admins get the provenance with `GET /tasks/{id}/provenance`
(rules `owners-get-provenance` and `admins-get-provenance` of the base
config; custom lists of `security.access_control.policies` must add them).
New tasks have the gateway tags of FOCA (`gateway.pipeline_source`,
`gateway.backend`, ...; see [Gateway tags](api.md#gateway-tags)), served
with the task: clients that submit a fetched task again must drop the tags
under `gateway.` other than `gateway.backends` and `gateway.pipeline`, or
it is rejected (`400`). These tags replace `logs[].metadata.forwarded_to`
(see [Forwards in task logs](#forwards-in-task-logs)).

Users list what they may choose per task with `GET /plugins`, and the
service info has a field `gateway` (see [Choices of
clients](plugins.md#choices-of-clients)). Custom lists of
`security.access_control.policies` must add the rules of the base config,
or users get `403`:

```yaml
security:
  access_control:
    policies:
      users-list-plugins: [p, user, /ga4gh/tes/v1/plugins, (GET)|(HEAD)]
      users-get-plugins: [p, user, "/ga4gh/tes/v1/plugins/:id", (GET)|(HEAD)]
```

### Forwards in task logs

proTES no longer records the TES instance and the remote identifier of a
task in its logs (`logs[].metadata.forwarded_to`, a list of `tesForward`
objects with `id`, `url` and `forward_time`), and the API specification no
longer has `tesForward`. This field was not TES-compliant: `metadata` is a
map of strings, so strict clients could fail on tasks. It is removed in
0.4.0 without a deprecation period. Clients that read it use the gateway
tags of the task instead (see [Gateway tags](api.md#gateway-tags)), served
in the views `BASIC` and `FULL`:

- `forwarded_to[-1].url`: `gateway.backend`;
- `forwarded_to[-1].id`: `gateway.backend_task_id`;
- `forwarded_to[-1].forward_time`: `gateway.forwarded_at`.

Tasks stored by earlier versions have no gateway tags; they are served
without `forwarded_to` and the other backend references of earlier
versions in their log metadata (`remote_task_id`, `tes_uri`); other string
metadata is served as stored. They are still canceled at the TES instance
they were forwarded to (by `tes_endpoint.task_id`, or by the forwards or
`remote_task_id` that stay in the database, for tasks stored before 0.3.0), and tracked if they
have `tes_endpoint.task_id`: unfinished tasks stored before 0.3.0 keep
their last state (see [Task tracking](#task-tracking)).
Their final logs are stored without the forwards once they have finished.

The setting `custom.storeLogs` (`execution_trace`), which only switched
these forwards, is removed: the app refuses to start with it, and the Helm
chart refuses to render with `config.custom.storeLogs`; remove it from
custom configs and overlays.

In code that uses proTES as a library, the models `TesForward` and
`Metadata` (of `pro_tes.ga4gh.tes.models`) and the function
`flatten_forwards()` (of `pro_tes.ga4gh.tes.task_runs`) are removed;
`TesTaskLog.metadata` is a map of strings (`dict[str, str]`), and drops
values that aren't strings.

### Plugin store

FOCA 0.19 gives plugins a key-value store (`ctx.store`, e.g., for caches
and per-user counters; see [Plugin store](operations.md#plugin-store)).
proTES keeps its entries in the collection `plugin_store` of the database of
the tasks and creates its indexes on startup, so the database user of
proTES needs the right to create indexes (`createIndex`, part of the role
`readWrite`, which the Helm chart grants its app user; for an external
MongoDB, check the roles of the user). Without it, proTES starts anyway and
logs an error, and writes of plugins to the store fail. The new settings
`plugins.defaults.store` and `plugins.available.<id>.store` (`enabled`,
`max_entry_size`, `max_ttl`; see [Plugins](configuration.md#plugins)) have
defaults, so configs need no change.

The plugin listing (`GET /plugins`, `GET /plugins/{plugin_id}`) has two new
required fields per plugin, from version 1.1.0 of FOCA's specification of
the listing on: what the plugin keeps in its store (`data_stored`) and the
store's mode (`storage`: `shared`, `local` or `off`). Clients that validate
listings against an earlier specification must accept them.

### Middleware management API

The middleware management API is no longer registered, and its
specification is removed: plugins are configured only (see
[Plugins](plugins.md)). Its collection (`middlewares`) is no longer
created, so it can be dropped.

### Image and chart

The image serves the app with FOCA's Gunicorn settings for containers
(`gunicorn --config python:foca.gunicorn_config 'pro_tes.app:init_app()'`,
its default command); `pro_tes.gunicorn_config` of 0.3.0 is gone, so
deployments that override the command must change the module. The Helm
chart of this release needs the image of this release.

TES instances are listed in the chart's value `tes.backends`, with the
chart's camelCase keys (e.g., `urlSchemes`, `basic.usernameEnv`), instead
of `config.custom.tes.service_list` and `discovery`, on which rendering
fails (see the chart's README, Configuration). The chart sets the public URL
of proTES (`server.public_url`) to `https://<hostName>/`, or to `publicUrl`
if set.

## From versions before 0.3.0

Deployments from before 0.3.0 (images tagged by date, FOCA 0.12) apply the
steps of [To version 0.4.0](#to-version-040), too, and those of this part.

### Configuration

proTES runs on Python 3.13. Custom configs must apply the
config changes of FOCA 0.14 to 0.17 (see the FOCA release notes and
migration guides), e.g., `exceptions.mapping` instead of the old exception
members, and no `validation_methods`, `validation_checks`, `environment` or
`use_reloader`.

The service info is configured in `service_info.service` (see [Service
info](configuration.md#service-info)); it is no longer stored in the
database, so the `service_info` collection can be dropped.

Unknown keys in `custom` are errors: the app and the Celery worker refuse
to start with them, naming them, instead of ignoring them (e.g., a
misspelled list of TES instances, which left it empty).
Remove the keys that had no effect from config overlays: all of
`custom.celery` (`custom.celery.monitor.timeout` and
`custom.celery.message_maxsize`) and of `custom.controllers.celery`
(`custom.controllers.celery.monitor.timeout` and
`custom.controllers.celery.message_maxsize`).

API specifications (`api.specs`) and access control rules
(`security.access_control.policies`) of the base config are named entries
(the spec `tes`; rules `users-create-list-tasks`,
`owners-get-cancel-tasks`, `owners-get-provenance`, `admins-get-provenance`,
`users-list-plugins`, `users-get-plugins`, `admins-list-add-permissions`
and `admins-manage-permissions`), which overlays change by name (see
[Roles](access-control.md#roles)); overlays that give lists still replace
them. The configured rules replace the stored ones atomically on startup,
and stored configured rules are removed if an overlay sets no rules. During
a rolling update, instances of earlier versions don't get this guarantee:
update all instances before changing the configured rules, and don't start
earlier versions again afterwards (see FOCA's [migration guide to
0.17](https://foca.readthedocs.io/en/latest/migration-0.17/), also for
downgrades).

### Authentication

The TES API requires access tokens of trusted issuers, and roles from groups
of the identity provider (see [Access control](access-control.md)); before,
anyone could create, list, read and cancel all tasks. Deployments must set
the trusted issuers (`security.auth.issuers`), the audience
(`security.auth.audience`) and the groups of the roles
(`security.access_control.claim_groups` and `group_mapping`, at least one
group of the role `admin`): proTES doesn't start without them (unless
`security.access_control.require_admin` is false). Clients send
`Authorization: Bearer <token>` with each request. HTTP Basic credentials of
requests are no longer passed through to TES instances: proTES uses
credentials of its own for them (see [TES instances and their
credentials](#tes-instances-and-their-credentials)). The Helm chart sets the
config overlay from the value `config`.

Only access tokens as per RFC 9068 (`typ` header `at+jwt`) are accepted;
other tokens of the trusted issuers, such as ID tokens, are rejected with
`401` (see [Authentication](access-control.md#authentication) for identity
providers that set another `typ` header, or none). The challenge of `401`
responses for invalid tokens has an `error_description`, too. The new
option `security.auth.clients` limits the accepted tokens to those of
certain clients (by default, clients are not restricted).

### Task ownership

Users see, read and cancel only their own tasks, and admins no longer may do
everything: they can't see the tasks of others, and only manage the access
control rules (see [Task ownership](access-control.md#task-ownership)).
Tasks of others and unknown tasks get `403 Forbidden` (was `404 Not Found`
for unknown tasks). The rule `users-get-cancel-tasks` is replaced by
`owners-get-cancel-tasks`, and the access control model
(`pro_tes/access_control/model.conf`) has owner roles (`g2`) and no longer
lets admins do everything: overlays naming the old rule, or custom models,
must be updated. Rules added via the permissions API are kept: check that
none of them gives users or admins access to all tasks (e.g., `p, user,
/ga4gh/tes/v1/tasks/:id, GET`).

proTES refuses to start if access control is not enabled and enforced, or
if service clients are configured (`security.auth.service_clients`,
`on_behalf_of_header`, `security.access_control.client_mapping`), which it
doesn't support yet.

Tasks stored by earlier versions have no owner, so nobody can see them
through the API; operators can give them to their users, or purge them (see
[Task owners](operations.md#task-owners)). Each new task adds two rules to
the access control database.

### Task identifiers

Task identifiers (`task.id`) are unique in the database: on startup,
proTES creates a unique index on them (and an index on `worker_id`), and
drops the index of earlier versions (`task_id_1_worker_id_1`), which never rejected a task identifier
that was used already. While stored tasks share identifiers, the index
can't be created, and proTES (the API and the Celery workers) refuses to
start: the error names the shared identifiers (up to 10), and how many
tasks have each. proTES doesn't change these tasks; resolve the duplicates in the
database of the tasks (`taskStore`, unless `MONGO_DBNAME` names another),
e.g., with `mongosh` (`use <database>`), and start proTES again:

1. Back up the tasks, e.g., with
   `mongodump --db <database> --collection tasks`.
2. Find the shared identifiers (`_id: null` stands for tasks without
   identifier, which the API can't serve anyway):

    ```js
    db.tasks.aggregate([
      { $group: { _id: "$task.id", n: { $sum: 1 } } },
      { $match: { n: { $gt: 1 } } },
    ])
    ```

3. List the tasks of each shared identifier:

    ```js
    db.tasks.find(
      { "task.id": "<id>" },
      { "task.id": 1, "task.state": 1, "task.creation_time": 1, user_id: 1 },
    )
    ```

4. Keep one task under the identifier, and either delete each of the
   others, or give it an identifier that is used neither by a task nor by
   owner roles (and tell its owner the new identifier, as the old one now
   leads to the task kept):

    ```js
    db.tasks.deleteOne({ _id: ObjectId("<_id>") })
    db.tasks.updateOne(
      { _id: ObjectId("<_id>") },
      { $set: { "task.id": "<new id>" } },
    )
    ```

5. If the tasks have owners (`user_id`), check the owner roles on the
   identifier: they gave each owner access to all tasks of the identifier.
   Delete those of the owners of the other tasks, and give the owners of
   renamed tasks roles on the new identifiers (see [Task
   owners](operations.md#task-owners)):

    ```js
    const paths = ["/ga4gh/tes/v1/tasks/<id>", "/ga4gh/tes/v1/tasks/<id>:cancel"]
    db.policy_rules.find({ ptype: "g2", v2: { $in: paths } })
    // the roles of a user who doesn't own the task kept
    db.policy_rules.deleteMany({ ptype: "g2", v0: "user:<sub>", v2: { $in: paths } })
    ```

### TES instances and their credentials

TES instances are listed in `custom.tes.backends` (see [TES instance
list](#tes-instance-list)).

Each entry of a TES instance has the URL of the TES instance (`url`) and how
proTES authenticates at it (`auth`), instead of a URL; `auth` defaults to
`client_credentials`, an access token of proTES itself (see [Credentials of
TES instances](access-control.md#credentials-of-tes-instances)). For each
URL of the old list, add an entry, e.g., `- type: ga4gh-tes` with `url:
https://tes.example.org`, and choose its `auth`:

- `client_credentials` for TES instances that accept tokens of proTES' own
  client: register a client for the client credentials grant at the
  identity provider, set its issuer (or token endpoint) in
  `custom.tes.client_credentials`, and its ID and secret in the environment
  variables `PROTES_CLIENT_ID` and `PROTES_CLIENT_SECRET` of the app and the
  Celery worker;
- `basic` for TES instances that require HTTP Basic credentials, with the
  names of the environment variables that hold them (`basic.username_env`
  and `basic.password_env`), set for the app and the Celery worker;
- `none` for TES instances that need no credentials.

proTES creates, gets and cancels tasks with a single request each, to
`<url>/ga4gh/tes/v1` only (earlier versions also tried `<url>/v1` and
`<url>`, and the next path or TES instance after timeouts and server
errors, so that TES instances could get a task more than once). TES
instances must serve the API at `/ga4gh/tes/v1` of their configured URL;
requests to create and cancel tasks don't follow redirects.
Tasks that a TES instance may have accepted without confirming it fail
with `SYSTEM_ERROR` and `502 Bad Gateway`, instead of being sent to the next
TES instance (see [TES instances](configuration.md#tes-instances)).
Requests to TES instances time out after `custom.tes.timeout` seconds
(default: 15, instead of 5); `custom.controllers.post_task.timeout`
(`post`, `poll` and `job`), which had no effect, was removed: remove it
from config overlays.

The base config no longer lists public TES instances; deployments list
their own. URLs of TES instances must use `https`, unless the entry's
`url_schemes` allows `http`, and must not have credentials; duplicate
entries (with the same identifier, by default the normalized URL) are
rejected. Tasks are only forwarded to configured and discovered TES
instances; plugins can only choose among them. Code that reads the TES
instances gets them from
`pro_tes.ga4gh.tes.backends.get_backends()` (FOCA's `Backend`s, in order),
instead of `custom.tes.service_list`; `get_backend(url)` returns the
backend together with its entry of `custom.tes.backends` (`(backend,
entry)`), and `TesClient` takes both.

Tasks with credentials in the URLs of their inputs or outputs
(`https://user:password@host/...`) are rejected with `400 Bad Request`, for
all TES instances (Funnel instances got them before); set
`custom.controllers.post_task.url_credentials` to `strip` to remove the
credentials instead.

On startup, proTES removes the HTTP Basic credentials of callers that
earlier versions stored with each task (`basic_auth`), and credentials in
the URLs of inputs and outputs of stored tasks (also of the outputs that
TES instances logged). The Celery worker refuses to start, too, if
credentials of TES instances are missing. Stored tasks are still tracked
and canceled with the credentials now configured for their TES instance.
The Celery task that tracks tasks no longer takes credentials
(`user`, `password`), so the new worker rejects tracking jobs that earlier
versions queued but no worker started yet: upgrade while no tasks are being
submitted, so that the queue is empty.

### Errors

Errors are [problem details](api.md#errors) (`title`, `status`, `detail`)
instead of objects with `message` and `code`.

### Stored tasks

`logs[].metadata.forwarded_to` of tasks is a list of forwards (`id`, `url`,
`forward_time`), in the order of forwarding, instead of a nested
`tesNextTes` object. Tasks stored by earlier versions are still read, and
can be canceled as before (unfinished ones keep their last state, see
[Task tracking](#task-tracking)). Since 0.4.0, forwards are no longer
recorded or served (see [Forwards in task logs](#forwards-in-task-logs)).

Earlier versions kept polling tasks that their backend reported as
`PREEMPTED`, which is a final state. These tasks are stored as `PREEMPTED`
already, but their final logs are not. The upgrade ends this polling, as
it ends all tracking by the old Celery worker; the logs of these tasks
aren't fetched again.

Tasks that no TES instance accepts (`503`), and tasks that plugins make
invalid or reject, are no longer kept: they are deleted, with
their owner's roles on them, instead of being stored as `SYSTEM_ERROR`, so
that failed requests don't fill the database. Tasks that a TES instance may
have accepted (`502`) are still stored as `SYSTEM_ERROR`. Tasks stored as
`SYSTEM_ERROR` by earlier versions are kept.

### Task tracking

Tasks are tracked by short polls that schedule the next one, instead of one
Celery task per task that held a worker process until the task finished (see
[Task tracking](operations.md#task-tracking)), so a few worker processes
track many tasks. Polls are acknowledged after they ran, and workers resume
the tracking of stored unfinished tasks when they start: restarts and
upgrades of the workers no longer stop the tracking of tasks. Only tasks
with a remote identifier in `tes_endpoint.task_id` (see [Task times and
remote identifiers](#task-times-and-remote-identifiers)) are resumed:
unfinished tasks stored by earlier versions keep their last state.

Failed polls no longer make a task fail: after
`custom.controllers.post_task.polling.attempts` consecutive failed polls
(not failed polls over the task's whole life), its state is `UNKNOWN` until
the TES instance answers again, and polls back off up to
`polling.max_wait` seconds (new, default 600); the first poll is retried,
too, and so are polls of tasks whose TES instance is not (or no longer)
configured or discovered. `polling.wait` and `polling.attempts` must be
positive, and `wait` and `max_wait` below 1800. The workers no longer set
`INITIALIZING` when they start tracking a task: the state is the one
reported by its TES instance.

### Task times and remote identifiers

Times of tasks (`creation_time`, and `start_time` and `end_time` of the
logs) are in UTC, in RFC 3339 format (e.g.,
`2026-10-22T12:34:56.789012+00:00`), instead of local-time strings such as
`10-22-2026 14:34:56`; times of tasks stored by earlier versions are served
as stored. proTES sets `creation_time`, and rejects tasks that set it
(`400`), as the TES specification marks it read-only.

The identifier of a
task at its TES instance is also stored in `tes_endpoint.task_id` (not
served), so that tasks can be canceled without forwards in their logs.

New tasks are stored with the state that their TES instance reports when
they are forwarded (e.g., `QUEUED`), instead of `UNKNOWN` until the Celery
worker tracks them. Tasks in state `UNKNOWN` are canceled at their TES
instance too (canceling them returned `200` without effect); tasks that
have not finished, but whose identifier at their TES instance is unknown,
can't be canceled (`409 Conflict`; was `500`, or `200` without effect
for tasks in state `UNKNOWN`). Tasks in state `UNKNOWN` without
identifier at a TES instance (also none in their logs) that were stored
more than `custom.controllers.post_task.max_create_time` seconds ago
(default 3600) are set to `SYSTEM_ERROR` by the Celery workers, also those
stored by earlier versions (see [Task
tracking](operations.md#task-tracking)).

### Task listing

`GET /tasks` returns an empty `next_page_token` on the last page (it
returned the last task's identifier, so clients kept paging), and an empty
page instead of an error if no task matches; invalid page tokens and page
sizes are rejected (`400`). Pages have 256 tasks by default (was 5) and at
most 2047 (`custom.controllers.list_tasks`, see
[Tasks](configuration.md#tasks)). `name_prefix` is taken literally, not as a
regular expression. proTES no longer starts if
`custom.controllers.list_tasks.default_page_size` exceeds `max_page_size`.
Pages of the view `FULL` have at most 64 tasks
(`custom.controllers.list_tasks.max_page_size_full`), also by default:
clients that list full tasks must follow `next_page_token`.

Tasks with an inline content of an input (`inputs[].content`) of more than
128 KiB (`custom.controllers.post_task.max_inline_content`) are rejected
with `413 Content Too Large`; raise the limit (and
`server.max_content_length`) if needed, or use inputs with URLs.

### Image

The image installs proTES as a package: it serves the app with `gunicorn
--config python:foca.gunicorn_config 'pro_tes.app:init_app()'` (its
default command, with FOCA's settings for containers; earlier images of this
layout loaded the same settings from `pro_tes.gunicorn_config`, which is
gone), and runs the Celery worker with `celery --app
pro_tes.celery_worker worker`. `/app/pro_tes`, `gunicorn.py` and `wsgi.py` are
gone, and the environment variables `GUNICORN_PROCESSES` and
`GUNICORN_THREADS` are replaced by `GUNICORN_CMD_ARGS` (see
[Server](configuration.md#server)). The Helm chart of this version needs
images of this version, and the legacy chart (versions 2 and 3.0.0) needs
earlier images. Released charts have the version of their release, so chart versions
restart below 3.0.0.

Images are tagged `dev` and `sha-<commit>` for commits on `dev`, and `X.Y.Z`
for releases; `latest` is the highest stable release, no longer the latest
commit on `dev`, and date tags are no longer pushed (see [Versioning and
releases](development.md#versioning-and-releases)). The Helm chart takes
the image tag from `protes.tag`, which defaults to the chart's `appVersion`:
`dev` in the repository, the release version in released charts (see
[Image](deployment.md#image)). `protes.image` may still include a tag (e.g.,
`elixircloud/protes:{{ version }}`); the Celery worker runs the app's image.

### Helm chart

The Helm chart is replaced by a chart on FOCA's library chart `foca-common`
(see [Deployment](deployment.md)): other values (camelCase, e.g., `hostName`
instead of `applicationDomain`), resource names and selectors, Secrets of
your own for MongoDB and the broker instead of credentials in the values
(the broker's user was `guest:guest`), Flower off by default and with
credentials from a Secret (was always on, with `admin:admin`),
NetworkPolicies, pinned images, and an Ingress for all paths (was
`/ga4gh/tes/v1` only), or a Gateway API HTTPRoute. The chart has no defaults
for a particular ingress controller: to enable the app's NetworkPolicy
(`protes.networkPolicy.enabled`), set its peers
(`protes.networkPolicy.ingressFrom`, none by default), and with
ingress-nginx, raise its body size limit by an annotation if you raise
`config.server.max_content_length` above 1 MiB (see [Exposing the
app](deployment.md#exposing-the-app)). Releases of the legacy
chart can't be upgraded: uninstall them and install the new chart, keeping
the data as described in [From the legacy
chart](deployment.md#from-the-legacy-chart).

### Docker Compose

Docker Compose runs MongoDB 7.0 (was 3.6, which current PyMongo releases
can't connect to), and keeps the data in named volumes instead of
`PROTES_DATA_DIR` (default `../data/pro_tes`), whose data is not migrated.
MongoDB can't upgrade data files from 3.6 to 7.0 directly: dump the data with
the old version (`mongodump`) and restore it into the new one
(`mongorestore`), or start with an empty database. Only proTES (and Flower,
now opt-in) are published, on `127.0.0.1`.
