# Access control

## Authentication

The TES API requires an access token of a trusted identity provider (OpenID
Connect), sent as a bearer token, for all operations on tasks:

```bash
curl -X POST "http://localhost:8080/ga4gh/tes/v1/tasks" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d @task.json
```

`GET /service-info`, the specification (`/ga4gh/tes/v1/openapi.json`) and
the Swagger UI are public. Requests without a valid
token are rejected with `401 Unauthorized` and a Bearer challenge
(`WWW-Authenticate: Bearer`, RFC 6750; with `error="invalid_token"` for
invalid tokens); requests with a valid token, but without the required role,
with `403 Forbidden` (both as [problem details](api.md#errors)).

proTES accepts only tokens that:

- are access tokens as per [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068)
  (`typ` header `at+jwt`), not, e.g., ID tokens;
- are issued by one of the trusted issuers (`security.auth.issuers`, the
  `iss` claim), and verified with the issuer's keys (from its OpenID Connect
  discovery document);
- are for one of the configured audiences (`security.auth.audience`, the
  `aud` claim), i.e., were issued to one of the apps that call proTES (e.g.,
  its front ends, by their client IDs);
- if `security.auth.clients` is set, were obtained by one of these clients
  (the `client_id` claim, or `azp` if there is none);
- have not expired.

If your identity provider sets another `typ` header on its access tokens,
list it in `security.auth.token_types`; if it sets none, disable the check
with `security.auth.verify_token_type: false` (see [FOCA's authentication
options](https://foca.readthedocs.io/en/latest/configuration/auth/)).

## Roles

| Role | May |
| --- | --- |
| `user` | create tasks (`POST /tasks`) |
| any authenticated caller | list their own tasks (`GET /tasks`), also without a role |
| `owner` (of a task) | read and cancel the task (`GET /tasks/{id}`, `POST /tasks/{id}:cancel`) |
| `admin` | manage the access control rules ([permissions API](#permissions-api)), and get the resources that tasks used per user ([Usage](api.md#usage)) and as execution reports ([Execution reports](api.md#execution-reports)); not the tasks of others |

Roles are given to groups of the identity provider: the values of the claim
`security.access_control.claim_groups` (from the token, or from the user info
endpoint if listed in `security.auth.userinfo_claims`) are mapped to the
roles `user` and `admin` by `security.access_control.group_mapping`. Values
are matched exactly, so list each group whose members get a role, including
subgroups. The role `owner` is not mapped from groups: users get it on the
tasks they create (see [Task ownership](#task-ownership)).

The roles and their permissions are the policies of
`security.access_control.policies` in the base config (see
[FOCA's access control](https://foca.readthedocs.io/en/latest/access-control/)),
named rules (`users-create-tasks`, `authenticated-list-tasks`,
`owners-get-cancel-tasks`, `owners-get-provenance`, `admins-get-provenance`,
`admins-get-usage`, `admins-get-usage-reports`, `users-list-plugins`,
`users-get-plugins`, `admins-list-add-permissions` and
`admins-manage-permissions`): a config
overlay adds, changes or removes (`null`) single rules by name, e.g., rules
that let only users list their tasks, and owners only read them:

```yaml
security:
  access_control:
    policies:
      authenticated-list-tasks: null
      users-list-tasks: [p, user, /ga4gh/tes/v1/tasks, (GET)|(HEAD)]
      owners-get-cancel-tasks: [p, owner, "/ga4gh/tes/v1/tasks/:id", (GET)|(HEAD)]
```

(`users-list-tasks` is a name of your own; names only need to be unique.)
The subject `"*"` of a rule (as in `authenticated-list-tasks`) stands for
any authenticated caller.

The access control model of proTES (`pro_tes/access_control/model.conf`)
differs from FOCA's reference model in that the role `admin` may not do
everything: admins only have the rights that rules give them.

## Task ownership

The user who creates a task (the `sub` claim of the access token, or the
claim set in `security.access_control.claim_identity`) becomes its owner:

- `GET /tasks` lists only the tasks of the caller, also for admins, and is
  allowed for every authenticated caller (rule `authenticated-list-tasks`),
  so that former users still find their tasks (also everyone else whom
  your identity providers issue tokens for proTES, who see an empty list;
  set `authenticated-list-tasks: null` and add a rule for the role `user`
  to refuse them, see [Roles](#roles));
- `GET /tasks/{id}` and `POST /tasks/{id}:cancel` are allowed for the owner
  only; everyone else gets `403 Forbidden`, also admins and for unknown
  tasks, so that responses don't tell which tasks exist. The owner gets
  `404 Not Found` for a task that was deleted from the database.

- `GET /tasks/{id}/provenance` is allowed for the owner and for admins
  (rules `owners-get-provenance` and `admins-get-provenance`); everyone else
  gets `403 Forbidden`. The provenance holds no inputs or logs of the task,
  only which plugins and TES instances processed it (see
  [Provenance](api.md#provenance)); remove `admins-get-provenance` (`null`
  in an overlay) to keep it from admins.

- `GET /plugins` and `GET /plugins/{plugin_id}` (what users may choose per
  task, see [Choices of clients](plugins.md#choices-of-clients)) are allowed
  for the role `user` (rules `users-list-plugins` and `users-get-plugins`);
  everyone else gets `403 Forbidden`, also admins without the role `user`,
  and requests without an access token `401 Unauthorized`.

Tasks are not shared with other users, and admins can't read or cancel the
tasks of others through the API: tasks and their logs may hold sensitive
data, and users' entitlements to access data can't be passed on to others.
Owners keep access to their tasks if they lose the role `user` (e.g., when
their group membership is revoked): they still list, read and cancel them,
but can't create tasks then.

Owners are stored as resource-scoped roles of FOCA (`g2` rules in the
collection `policy_rules`) on the task and on its cancellation, e.g., for
task `ABC123`:

```text
g2, user:<sub>, owner, /ga4gh/tes/v1/tasks/ABC123
g2, user:<sub>, owner, /ga4gh/tes/v1/tasks/ABC123:cancel
```

The task document records its owner, too (`user_id`), e.g., for operators
looking for the tasks of a user (see [Operations](operations.md#task-owners)).
Each task adds two rules, and FOCA loads all rules on each request (and
creating or listing tasks loads them a few more times), so the time of each
request grows with the number of tasks (see FOCA's notes on
[scaling](https://foca.readthedocs.io/en/latest/access-control/#scaling)).
As every authenticated caller may list tasks, limit request rates at the
ingress if your identity providers issue tokens for proTES to many people.
"Authenticated" means any valid token with a subject from a trusted issuer:
also tokens of clients that are not [service clients](#service-clients),
proTES' own client among them, and, with
`security.auth.verify_audience: false`, tokens issued for other services.
Such callers own no tasks, so they get an empty list.

Ownership is taken from the caller's own token only: a user's token makes
the user the owner, and a service client's own token the client (see
[Service clients](#service-clients)). Nobody can create tasks for someone
else: proTES refuses to start if `security.auth.on_behalf_of_header` is set,
or a service client may act for users (`on_behalf_of`).

New tasks never get the identifier of a stored task, or of a task that
owner roles are left on (e.g., of a deleted task), so that nobody gets
access to the task of someone else with the same identifier.

A list replaces all rules of the base config. The configured rules replace
the stored ones atomically on each startup; if an overlay leaves no rules,
the stored ones are removed.
The role `admin` comes only from groups in `group_mapping`: the startup
check doesn't count `g` rules of the policies or of the permissions API.

## Service clients

Apps that run tasks for themselves, not for a user (e.g., a workflow engine
with a client-credentials token of its own, or automated tests), can be
allowed as service clients. This is off by default: without
`security.auth.service_clients`, tokens of clients are handled as before,
and their own client IDs are not accepted audiences. Each allowed client is
listed explicitly, by its issuer and client ID, and gets the role `user`,
e.g., for a client `my-engine` of the identity provider of the example
below (see [Deployment settings](#deployment-settings)):

```yaml
security:
  auth:
    audience:
      - my-portal-client-id
      # if the identity provider puts only the client's own ID into `aud`
      # of its client-credentials tokens
      - my-engine
    service_clients:
      - client_id: my-engine
        issuer: https://login.example.org/oidc/
  access_control:
    client_mapping:
      my-engine: user
```

A token is the service token of a listed client if it was issued by the
client's issuer (`iss`), to the client (`client_id`, or `azp` without it),
and its subject (`sub`) is the client ID (or the client's `subject`, see
[FOCA's authentication
options](https://foca.readthedocs.io/en/latest/configuration/auth/)). With
such a token, the client:

- creates tasks, and becomes their owner as a client (`client:<client
  ID>`: owner roles such as `g2, client:my-engine, owner,
  /ga4gh/tes/v1/tasks/<id>`, and `user_id: "client:my-engine"` in the task
  document);
- lists, reads and cancels its own tasks, and reads their provenance;
- sees no tasks of others, and others (users, other clients, admins) don't
  see its tasks; a user whose `sub` equals a client ID is another owner;
- gets nothing beyond: no admin rights, and no user it could act for.

Its tasks are relayed to the TES instances as all tasks, with proTES' own
credentials (see [Credentials of TES
instances](#credentials-of-tes-instances)); the client's token is not
passed on. Limits of the tasks per user count the tasks of each client as
those of `client:<client ID>`, and accounting records them under that owner.

**Audience.** Some identity providers put only the client's own ID into the
audience (`aud`) of its client-credentials tokens, so that ID must be an
accepted audience (`security.auth.audience`, or the `audience` of the
client's issuer); proTES warns on startup for clients whose ID isn't. As
that audience would also accept other tokens for the client, e.g., users'
tokens obtained by it (by login, or by token exchange, after which some
identity providers keep the exchanging client in `aud`), proTES accepts the
ID of a service client as audience only for that client's own service
tokens: other tokens whose only accepted audiences are IDs of service
clients are rejected on all endpoints (`401`, with an `invalid_token`
challenge). Tokens for the other audiences are not affected.

**Groups.** Service tokens with groups (in the group claim,
`claim_groups`) are rejected likewise (`401`), also without audience
validation, as the roles of their groups (`group_mapping`) would be added
to the role `user` of the client, e.g., `admin`; proTES logs a warning
naming the client and the claim. Many identity providers put no groups into
client-credentials tokens, and proTES doesn't request user info for them.
Where they do (e.g., for groups of a client's service account), remove the
groups from the client's tokens at the identity provider (its group
memberships or the mapper of the claim) before listing the client.

proTES refuses to start if a service client has no explicit `issuer` (client
IDs are unique per issuer only), isn't mapped to the role `user` or is
mapped to another role, may act for users (`on_behalf_of`), or is proTES'
own client (`PROTES_CLIENT_ID`), whose tokens TES instances get. The allowed
clients are logged on startup.

Clients acting for users are not supported: tasks belong to the caller of
their token. To pass users on (e.g., from a workflow engine), token exchange
(RFC 8693) is the way: a user's token exchanged for proTES' audience is a
user token, so its tasks belong to the user, and the service client role
isn't needed for it.

## Permissions API

Admins can list, add, change and delete access control rules at runtime
through [FOCA's permissions
API](https://foca.readthedocs.io/en/latest/access-control/) at
`/admin/access-control/permissions`; everyone else gets `401` (no token) or
`403`. The rules are stored in the database (collection `policy_rules` of
`taskStore`, or of the database in `MONGO_DBNAME` if set); the configured
policies are restored on each startup. The Helm chart's Ingress (or
HTTPRoute, or Route) routes all paths to the app, including the permissions
API; to keep it off the internet, block `/admin/` in front of the cluster
(see [Exposing the app](deployment.md#exposing-the-app)).

The permissions API can write any rule, also owner roles on tasks (`g2`
rules), so admins could make themselves owners of the tasks of others. FOCA
logs each request to it; give the role `admin` only to operators trusted
with the data of the tasks.

## Deployment settings

Which identity providers are trusted, for which audiences, and which groups
have which role is deployment configuration: the base config has no
defaults, and the app refuses to start without trusted issuers, without an
audience (unless `security.auth.verify_audience` is false, which accepts
tokens for any app, or each issuer has an audience of its own, see below)
and without a group mapped to the role `admin` (unless
`security.access_control.require_admin` is false). Set them in a [config
overlay](configuration.md), e.g.:

```yaml
security:
  auth:
    issuers:
      - https://login.example.org/oidc/
    audience:
      - my-portal-client-id
    # if the identity provider releases groups via user info only
    userinfo_claims:
      - groups
  access_control:
    claim_groups: groups
    group_mapping:
      compute-users: user
      compute-admins: admin
```

Issuers may also be maps of the issuer (`issuer`) and settings of their own
(FOCA's `IssuerConfig`): the namespace of their users' subjects
(`namespace`; by default the issuer, also if only one issuer is
trusted; `""` keeps the subjects bare; see [Task
owners](operations.md#task-owners)), and an `audience`, a group claim
(`claim_groups`) and `userinfo_claims` that replace the global ones for
the issuer's tokens (unless unset or null), e.g.:

```yaml
security:
  auth:
    issuers:
      - issuer: https://login.example.org/oidc/
        audience:
          - my-portal-client-id
      - issuer: https://other.example.org/
        audience:
          - my-portal-client-id
        # take no groups from this issuer's tokens
        claim_groups: ""
```

Group claim values are not namespaced: take groups only from issuers that
cannot issue each other's groups, and set `claim_groups: ""` for the others
(see [FOCA's authentication
options](https://foca.readthedocs.io/en/latest/configuration/auth/)).

The app logs the groups of each role on startup, and warns if no group has
the role `user` (nobody can create tasks then) and if `group_mapping` is
set without `claim_groups` (no group gets a role then). FOCA also warns on
every startup that `group_mapping` is set without
`security.access_control.keep_group_subjects`, so that rules granted to
groups (`group:<value>`) don't apply: this warning is harmless for proTES,
whose rules grant roles only, and is expected (see
[foca#577](https://github.com/elixir-cloud-aai/foca/issues/577)). It refuses to start
if access control is not enabled and enforced
(`security.access_control.enabled` and `enforce`), as users could access
the tasks of others then. The Docker Compose setup
uses `config.dev.yaml`, which accepts tokens for any audience and requires no
admins; set your issuer and groups there. For local tests without an
identity provider, the [smoke test](development.md) brings a fake one.

## Credentials of TES instances

proTES authenticates at TES instances with credentials of its own, never
with those of its callers: the access tokens of requests are not forwarded,
and no credentials of callers are stored or queued for the Celery worker.
How proTES authenticates is set for each entry of `custom.tes.backends`, by
`auth` (TES instances discovered in a service registry get the `auth` of
the registry's entry, `client_credentials` or `none`):

- `client_credentials` (the default): with an access token of proTES itself,
  which it gets from its identity provider with the OAuth 2.0 client
  credentials grant ([RFC 6749, section
  4.4](https://www.rfc-editor.org/rfc/rfc6749#section-4.4)). TES instances
  accept tokens of proTES' client (e.g., by the `client_id` claim of the
  token). proTES caches the token, and fetches a new one shortly before it
  expires (`refresh_before` seconds, at most half its lifetime), also in the
  Celery worker, which polls tasks for hours;
- `basic`: with HTTP Basic credentials from environment variables, whose
  names are set in `basic` (`username_env`, `password_env`). This is a
  transitional mode for TES instances that don't accept tokens yet, and will
  be removed once all of them do;
- `api_key`: with an API key of proTES at the TES instance, for TES
  instances that don't accept tokens of proTES' client, but API keys, e.g.,
  [Poiesis](https://poiesis.jaeaeich.com/docs/concepts/api-keys.html) (see
  [API keys of TES instances](#api-keys-of-tes-instances)). For configured
  TES instances only: registries can't hand one key to all the TES
  instances that they list. This is a transitional mode too, until proTES
  exchanges the tokens of its users per TES instance;
- `none`: without credentials, e.g., for TES instances on an internal
  network that need none.

Requests to TES instances get only these credentials: none from a `.netrc`
file of the environment (`~/.netrc`, or the file of `NETRC`), which
`requests` would otherwise add to requests without credentials, put in
place of proTES' access token, and look up again after redirects. Redirects
within the same host and port keep the credentials, as do redirects from
`http` to `https` on the default ports; all other redirects drop them (also
API keys in headers other than `Authorization`).
Proxies and CA certificates of the environment (e.g., `HTTPS_PROXY`,
`REQUESTS_CA_BUNDLE`) still apply to configured TES instances. Requests to
the identity provider of proTES don't get credentials from `.netrc` either,
except after redirects of its discovery document.

The client ID and secret of proTES are read from the environment variables
`PROTES_CLIENT_ID` and `PROTES_CLIENT_SECRET`, and Basic credentials and API
keys from the variables named in the config, e.g., set from Kubernetes
Secrets; they are never part of the config or the database. The app refuses
to start if one of them is not set for a TES instance that needs it, and
warns of each TES instance with `basic`, `api_key` or `none`. Set the TES
instances and the identity
provider of proTES in a [config overlay](configuration.md), e.g.:

```yaml
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://tes.example.org
      - type: ga4gh-tes
        url: https://tes.example.com
        auth: basic
        basic:
          username_env: TES_EXAMPLE_COM_USERNAME
          password_env: TES_EXAMPLE_COM_PASSWORD
      - type: ga4gh-tes
        url: https://poiesis.example.net
        auth: api_key
        api_key:
          key_env: TES_POIESIS_API_KEY
    client_credentials:
      # The token endpoint is that of the issuer's OpenID Connect discovery
      # document (`token_endpoint` sets it instead)
      issuer: https://login.example.org/oidc/
      # Scope of the tokens, if the identity provider needs one
      scope: null
      # Parameter that names the TES instance in token requests, for a
      # token per TES instance: `resource` (RFC 8707) or `audience`; by
      # default (null), all TES instances get the same token (see below)
      audience_parameter: null
      # `client_secret_basic` (HTTP Basic) or `client_secret_post`
      auth_method: client_secret_basic
```

The Docker Compose setup passes `PROTES_CLIENT_ID` and
`PROTES_CLIENT_SECRET` of the host to the app and the Celery worker; with
the Helm chart, set them (and the variables of Basic credentials) from a
Secret with `protes.extraEnv` (see the chart's README). The Celery worker
checks the credentials on startup, too.

The client of proTES is registered at the identity provider for the client
credentials grant; it is a client of its own, not the client of the front
ends whose tokens proTES accepts (`security.auth.audience`). By default,
all TES instances with `client_credentials` get the same token, so each of
them could use it at the others until it expires: configure only TES
instances that you trust, and keep the lifetime of the tokens short. This
includes the TES instances discovered in service registries, whose entries
default to `auth: client_credentials`: every TES instance that a registry's
admins verify gets the token, and could use it at the other TES instances
of proTES, also at the configured ones. Use registries whose admins you
trust as much as the TES instances that you configure yourself, or set
`auth: none` for registry entries whose TES instances don't need the token
of proTES; or request a token per TES instance (see [Tokens per TES
instance](#tokens-per-tes-instance)). On startup, proTES logs which TES
instances get its token (`TES backends with access tokens of proTES: ...`,
with ``(each for its own audience, requested with `resource`)`` for tokens
per TES instance), with the registries whose verified TES instances get it
(`those verified by registry <url>`). Service infos, read for the TES
versions of TES instances (and the API paths of discovered ones), get no
credentials, unless
`custom.tes.probe_with_token` is set, which sends the token also to TES
instances that a task isn't routed to (see [TES
versions](configuration.md#tes-versions)). Tasks are only
forwarded to configured TES instances, and to those discovered in the
service registries of `custom.tes.backends` (only verified ones, see
[Discovery in a service
registry](configuration.md#discovery-in-a-service-registry)); plugins
can only choose among them (see [Plugins](plugins.md)). The credentials of
TES instances are not passed to plugins, and the plugin API doesn't let
them choose other credentials for a TES instance than its own; in the
`forward` phase, they may only drop them (`none`), e.g., to send
credentials of their own. Plugins are fully trusted code, though: they run
in the processes of proTES and can read its credentials (see
[Plugins](plugins.md#how-a-task-is-processed)). Keep TES instances in the list until their tasks have finished:
tasks of TES instances that are no longer configured can't be tracked or
canceled (their state becomes `UNKNOWN`, and is tracked again once the TES
instance is configured again).

URLs of TES instances, registries and of the identity provider must use
`https` (`url_schemes` of each entry of `custom.tes.backends`, and
`custom.tes.url_schemes` for the identity provider); allow `http` for local
tests only, as tokens and credentials are sent unencrypted then. URLs of TES
instances must not have credentials (`https://user:password@host`).

Credentials in the URLs of inputs and outputs of tasks
(`https://user:password@host/...`) are not accepted either: such tasks are
rejected with `400 Bad Request`, or, if
`custom.controllers.post_task.url_credentials` is `strip`, the credentials
are removed before the task is stored and forwarded (see
[Tasks](configuration.md#tasks)). Credentials that plugins add are
always removed. The headers `Authorization`, `Cookie` and
`Proxy-Authorization` of callers are left out of what plugins see, but
plugins get the caller's validated access token (FOCA's `Principal.token`,
e.g., for token exchange): enable only plugins that you trust with it.

proTES doesn't tell TES instances on whose behalf it forwards tasks yet:
they only see proTES. Tags with the identity of the user are planned, once
their names are agreed with other services.

### Tokens per TES instance

With `custom.tes.client_credentials.audience_parameter`, proTES requests a
token of its own for each TES instance, which names the TES instance as the
audience of the token, so that a TES instance that checks the audience
refuses the tokens of the others. The parameter of the token request is
`resource` (resource indicators, [RFC
8707](https://www.rfc-editor.org/rfc/rfc8707)) or `audience` (as some
identity providers name it); its value is the `audience` of the TES
instance's entry, as written, or else its URL (normalized, e.g., without
trailing slash). TES instances discovered in a service registry always get tokens
for their base URLs: the URLs that the registry lists, normalized and
without the TES API path (e.g., `https://tes.example.org` for
`https://tes.example.org/ga4gh/tes/v1/`). For `resource`, an `audience` of
an entry must be an absolute URI without query and fragment (e.g.,
`urn:example:tes`); for `audience`, it can be any string without
surrounding whitespace (e.g., the client ID of the TES instance). proTES
caches the tokens per audience (entries with the same `audience` share a
token), and refuses to start if an entry sets `audience` without `auth:
client_credentials`, or while `audience_parameter` is not set. Errors of
token requests name the audience, in quotes (`No access token for resource
'<audience>' from <token endpoint>: ...`).

```yaml
custom:
  tes:
    backends:
      # Gets a token for `https://tes.example.org`
      - type: ga4gh-tes
        url: https://tes.example.org
      # Gets a token for the audience of the entry
      - type: ga4gh-tes
        url: https://tes.example.com
        audience: urn:example:tes
      # Each verified TES instance gets a token for its URL
      - type: ga4gh-service-registry
        url: https://registry.example.org/ga4gh/registry/v1
    client_credentials:
      issuer: https://login.example.org/oidc/
      audience_parameter: resource
```

This protects TES instances from each other only if all of the following
hold:

- the identity provider issues tokens for the requested audience (in the
  `aud` claim of the token, or in what its token introspection returns).
  Identity providers that set the audience of a client once, at its
  registration, ignore the parameter (all TES instances get tokens with
  that audience again) or refuse the request (`invalid_target`, logged as
  `No access token for <parameter> '<audience>' from <token endpoint>`, and
  tasks aren't forwarded to the TES instance);
- each TES instance accepts only tokens for its own audience, e.g., its URL
  as proTES sends it, or the `audience` of its entry.

proTES doesn't check the audience of the tokens it gets, which may be
opaque to it. Test the setup before relying on it: decode a token of the
identity provider for one TES instance, and check that the other TES
instances refuse it.

### API keys of TES instances

Some TES instances don't accept tokens of proTES' client, but API keys: e.g.,
[Poiesis](https://poiesis.jaeaeich.com/docs/concepts/api-keys.html), which
checks an entitlement claim that client-credentials tokens don't carry. For
these, an entry with `auth: api_key` gives proTES an API key of an account
at the TES instance; `api_key` says where proTES reads it, and how it sends
it:

```yaml
custom:
  tes:
    backends:
      - type: ga4gh-tes
        url: https://poiesis.example.net
        auth: api_key
        api_key:
          # Environment variable with the key (or `<key_env>_FILE`, see below)
          key_env: TES_POIESIS_API_KEY
          # Header of the key (default): `Authorization: Bearer <key>`, as
          # Poiesis expects it; other headers (e.g., `X-API-Key`) carry the
          # key as it is
          header: Authorization
```

- **Where the key comes from:** the environment variable `key_env`, or the
  file named in the variable `<key_env>_FILE` (e.g.,
  `TES_POIESIS_API_KEY_FILE=/run/secrets/poiesis-api-key`, a mounted
  Secret), as FOCA reads its own credentials; setting both is an error. A
  final line break is dropped (e.g., of a Secret created from a file).
  proTES reads the key for each request, and never writes it to the config,
  the logs, the database or the task documents, nor passes it to plugins.
  Keys must be visible ASCII characters (no spaces or line breaks). Unknown
  keys of `api_key` (e.g., the key itself as a value) stop the app on
  startup, and the error doesn't show their values.
- **Checks on startup:** the app and the Celery worker refuse to start if
  neither variable is set for a TES instance with `auth: api_key` (named
  with the other missing credentials), or if the key can't be used (both
  set, the file can't be read, or invalid characters). The messages name
  the TES instance by its URL, never the key, nor the names of the
  variables (a key pasted as `key_env` by mistake may be a valid variable
  name): look up `key_env` of that TES instance in the config. Missing keys
  are listed as ``the API keys of TES backends <url> (the variables that
  their `key_env` names, or their `_FILE` variables)``, keys that can't be
  used as ``TES backend <url>: API key: ...``; requests that find no usable
  key fail with ``TES backend <url>: API key ...``. Each TES instance with
  an API key is logged with a warning (``TES backend <url>: API key
  (`auth: api_key`, header <header>) ...``).
- **Which requests:** every request to the TES instance: creating, getting
  and canceling tasks (also by the Celery worker, which polls them), and its
  service info if `custom.tes.probe_with_token` is set (otherwise service
  infos are read without credentials). Redirects to other hosts or ports
  drop the key, in any header.
- **Only configured TES instances:** registry entries can't have `auth:
  api_key`, as every TES instance that a registry lists would get the key.
  Give each TES instance a key of its own, so that a TES instance can't use
  the key at another one.
- **Task ownership at the TES instance:** the TES instance sees all tasks
  of proTES as tasks of the key's account (in Poiesis, a key acts as the
  user who minted it), not of the users who created them. Users still only
  see their own tasks in proTES (see [Task ownership](#task-ownership)),
  but quotas, accounting and access control of the TES instance apply to
  the key's account as a whole, and anyone with access to that account at
  the TES instance sees all tasks that proTES sent there. Use an account
  that is meant for proTES, not a personal one.
- **Rotating the key:** create a new key at the TES instance, update the
  Secret, and restart the app and the Celery worker if the key comes from
  the environment variable (with the Helm chart, `kubectl rollout restart
  deploy/<app> deploy/<app>-worker -n <namespace>`, `<app>` as in its
  README, Names and labels, e.g., `protes` for the release `protes`); with
  `<key_env>_FILE`, the file is read for each request, so the new key is
  used once the file is updated. Then revoke the old key at the TES
  instance (see its docs, e.g., [Poiesis' API
  keys](https://poiesis.jaeaeich.com/docs/concepts/api-keys.html)). Keys
  may expire: rotate them in time, as requests with an expired key fail
  with `401 Unauthorized` (new tasks then go to other TES instances, and
  the tasks at that TES instance can't be tracked or canceled until a valid
  key is set).

API keys are a transitional mode until proTES exchanges the tokens of its
users for tokens of each TES instance (OAuth 2.0 token exchange, [RFC
8693](https://www.rfc-editor.org/rfc/rfc8693)); TES instances then see the
users again, and no shared key is needed.
