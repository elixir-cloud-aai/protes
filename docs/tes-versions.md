# TES versions

proTES serves [TES](https://github.com/ga4gh/task-execution-schemas) 1.1 to its
clients, and relays tasks to TES instances of TES 1.0 and 1.1, so clients of
TES 1.1 can use TES instances that implement only TES 1.0. How proTES finds out
the version of a TES instance is described in [TES
versions](configuration.md#tes-versions).

proTES stores tasks in TES 1.1, as clients sent them. Each TES instance gets its
own copy in its version. Plugins always get the task in TES 1.1, and must not
assume a version of TES instances.

## Routing

Plugins of the `route` phase only see the TES instances that can run the task
unchanged, so routing needs no knowledge of versions, and the provenance of a
task shows these TES instances. Tasks for which none remains, also among those
that the client chose (`gateway.backends`), are rejected with `400 Bad Request`,
naming the fields, and are not stored.

Plugins of other phases can use
`pro_tes.ga4gh.tes.backends.eligible_backends(task, backends)` for the same
check. If a plugin of the `forward` phase makes a task impossible to express
for a TES instance, that attempt fails with the reason `incompatible`, and the
next TES instance is tried.

A TES instance of TES 1.1 may still reject a task, e.g., if it doesn't support
`ignore_error` or strict `backend_parameters` keys: the next TES instance is
then tried, as for any rejected task. proTES doesn't compare the keys of
`backend_parameters` with those that a TES instance lists: enforcing strict
parameters is up to the TES instance, as the specification says.

A TES instance can itself be proTES: it reports TES 1.1, gets tasks unchanged,
and adapts them for its own TES instances. If it has none that can run a task,
it answers `400`, and the next TES instance is tried.

If a TES instance didn't answer so far and no other can run the task, the answer
is `503 Service Unavailable` instead, as retrying may help. A TES instance whose
version was read once keeps that version if it fails to answer later.

## Tasks sent to TES 1.0

| Field (TES 1.1) | Sent to TES 1.0 |
| --- | --- |
| `executors[].ignore_error: true` | Not eligible: the first failing executor would stop the task. |
| `executors[].ignore_error: false` | Dropped. |
| `inputs[].streamable` | Dropped: only a hint. |
| `resources.backend_parameters` | Dropped, unless strict. |
| `resources.backend_parameters_strict: true`, with parameters | Not eligible: they would not be honored. |
| `outputs[].path` with wildcards (`*`, `?`, `[`) | Not eligible: TES 1.0 takes them literally. |
| `outputs[].path_prefix` | Dropped: ignored without wildcards. |
| `inputs[].type`, `outputs[].type` | Set to `FILE` if missing, as TES 1.0 requires them. |

Dropped fields stay in the stored task, which clients get back. The stored
task also records the version that it was relayed in
(`tes_endpoint.tes_version`, not served). Tasks of TES 1.0 are valid in TES 1.1,
so responses of TES 1.0 instances stay as they are.

## Service info

`tesResources_backend_parameters` of the service info of proTES lists the keys
that TES instances listed in the service infos that this process of proTES has
read so far (it is not read for this purpose, as the endpoint is public; TES
instances with a label `tes.version` are not read at all), so it may be
incomplete, and differ between workers. To state it, set
`service_info.service.tesResources_backend_parameters`, which wins. Keys of a
task aren't checked against it. It stays if `custom.service_info.list_backends`
hides the TES instances, as it doesn't say which ones list a key.

## Adding a TES version

1. Vendor its specification (see [TES specification](development.md#tes-specification)),
   and update the models (`pro_tes/ga4gh/tes/models.py`) to it.
2. Add one adapter between it and the version before it to `ADAPTERS` in
   `pro_tes/ga4gh/tes/versions.py`, a subclass of `TesAdapter`, with its tests:
   `incompatibilities` lists what the older version can't express without
   changing what a task does, `downgrade_task` drops the rest, and
   `upgrade_task` converts tasks that backends of the older version serve.

Chains of adapters then cover all older versions.
