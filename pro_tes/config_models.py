"""Custom app config models."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import SplitResult, urlsplit

from foca.plugins.backends import BackendListConfig, ServiceEntry
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)

from pro_tes.ga4gh.tes.versions import SUPPORTED, TesVersion


class ConfigModel(BaseModel):
    """Base of the models of the custom configuration: unknown keys are errors.

    So that misspelled or removed keys (e.g., in config overlays) stop the
    app and the Celery worker on startup, instead of being ignored.
    """

    model_config = ConfigDict(extra="forbid")


class DB(ConfigModel):
    """DB config for post_task.

    Args:
        insert_attempts: Number of attempts to insert a new task in DB.

    Attributes:
        insert_attempts: Number of attempts to insert a new task in DB.
    """

    insert_attempts: int = 10


class TaskID(ConfigModel):
    """Task ID config.

    Args:
        charset: Characters to use when generating task IDs.
        length: Length of the generated task ID.

    Attributes:
        charset: Characters to use when generating task IDs.
        length: Length of the generated task ID.
    """

    charset: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    length: int = 6


# RabbitMQ's default acknowledgement timeout (`consumer_timeout`), in
# seconds: scheduled polls are held unacknowledged until they are due
CONSUMER_TIMEOUT = 1800
# Upper limit of settings of seconds after events (a year; cut-offs before the
# Unix epoch are invalid)
MAX_SECONDS = 31536000


class Polling(ConfigModel):
    """How tasks are tracked at their TES backends.

    Args:
        wait: Seconds between polls of the state of a task.
        attempts: Consecutive failed polls (the count is reset by a
            successful one) after which the state of the task is set to
            `UNKNOWN`, and polls back off.
        max_wait: Maximum number of seconds between polls while they back
            off (values below `wait` count as `wait`); below RabbitMQ's
            default acknowledgement timeout (`consumer_timeout`, 30 minutes),
            as are those of `wait`.
        max_cancel_time: Seconds after a cancel at which a task that is
            still being canceled (`CANCELING`: its backend has neither
            reported a final state nor that it no longer has the task) is set
            to `SYSTEM_ERROR`, and no longer tracked; at most a year.

    Attributes:
        wait: Seconds between polls of the state of a task.
        attempts: Consecutive failed polls (the count is reset by a
            successful one) after which the state of the task is set to
            `UNKNOWN`, and polls back off.
        max_wait: Maximum number of seconds between polls while they back
            off (values below `wait` count as `wait`); below RabbitMQ's
            default acknowledgement timeout (`consumer_timeout`, 30 minutes),
            as are those of `wait`.
        max_cancel_time: Seconds after a cancel at which a task that is
            still being canceled is set to `SYSTEM_ERROR`, and no longer
            tracked.
    """

    wait: Annotated[int, Field(gt=0, lt=CONSUMER_TIMEOUT)] = 3
    attempts: PositiveInt = 100
    max_wait: Annotated[int, Field(gt=0, lt=CONSUMER_TIMEOUT)] = 600
    max_cancel_time: Annotated[int, Field(gt=0, le=MAX_SECONDS)] = 3600


class PostTask(ConfigModel):
    """Configuration for POST /task.

    Args:
        db: DB insert behavior.
        task_id: Task ID generation settings.
        polling: Polling behavior.
        url_credentials: What happens to tasks with credentials in the URLs
            of their inputs or outputs (`https://user:password@host/...`):
            they are rejected (`reject`), or the credentials are removed
            (`strip`); credentials are never stored or forwarded.
        max_inline_content: Maximum size of the inline contents of the
            inputs of a task (`inputs[].content`), together, in bytes
            (UTF-8); tasks with larger contents are rejected.
        max_create_time: Seconds after which tasks still being created
            (in state `UNKNOWN`, without identifier at a TES instance; e.g.,
            if proTES stopped while forwarding them) are set to
            `SYSTEM_ERROR`; longer than creating a task may take, and at
            most a year.
        max_log_size: Maximum size of the logs of a TES instance that are
            stored per task (standard output and error of executors and
            system logs together), in bytes (UTF-8); longer ones are cut,
            keeping their end. `0` stores none, without getting them. At most
            8 MiB, as documents in MongoDB have at most 16 MB.

    Attributes:
        db: DB insert behavior.
        task_id: Task ID generation settings.
        polling: Polling behavior.
        url_credentials: What happens to tasks with credentials in the URLs
            of their inputs or outputs (`https://user:password@host/...`):
            they are rejected (`reject`), or the credentials are removed
            (`strip`); credentials are never stored or forwarded.
        max_inline_content: Maximum size of the inline contents of the
            inputs of a task, together, in bytes (UTF-8).
        max_create_time: Seconds after which tasks still being created are
            set to `SYSTEM_ERROR`.
        max_log_size: Maximum size of the logs of a TES instance that are
            stored per task, in bytes (UTF-8).
    """

    db: DB = DB()
    task_id: TaskID = TaskID()
    polling: Polling = Polling()
    url_credentials: Literal["reject", "strip"] = "reject"
    max_inline_content: PositiveInt = 131072
    max_create_time: Annotated[int, Field(gt=0, le=MAX_SECONDS)] = 3600
    max_log_size: Annotated[int, Field(ge=0, le=8388608)] = 1048576


class ListTasks(ConfigModel):
    """Configuration for GET /tasks.

    Defaults are those of the TES specification (pages of fewer than 2048
    tasks).

    Args:
        default_page_size: Number of tasks per page if the request sets no
            page size.
        max_page_size: Maximum number of tasks per page, at most 2047;
            larger requested page sizes are reduced to it.
        max_page_size_full: Maximum number of tasks per page of the view
            `FULL` (with the logs and the contents of inputs); larger page
            sizes are reduced to it (and to `max_page_size`).

    Attributes:
        default_page_size: Number of tasks per page if the request sets no
            page size.
        max_page_size: Maximum number of tasks per page; larger requested
            page sizes are reduced to it.
        max_page_size_full: Maximum number of tasks per page of the view
            `FULL`.
    """

    default_page_size: PositiveInt = 256
    # the TES specification allows pages of fewer than 2048 tasks
    max_page_size: PositiveInt = Field(default=2047, le=2047)
    # full tasks may be large (e.g., with inline contents of inputs)
    max_page_size_full: PositiveInt = 64

    @model_validator(mode="after")
    def check_default_page_size(self) -> "ListTasks":
        """Check that the default page size is at most the maximum.

        Returns:
            The validated configuration.

        Raises:
            ValueError: The default page size exceeds the maximum.
        """
        if self.default_page_size > self.max_page_size:
            raise ValueError(
                f"default_page_size ({self.default_page_size}) exceeds"
                f" max_page_size ({self.max_page_size})"
            )
        return self


class Controllers(ConfigModel):
    """Controller configurations.

    Args:
        post_task: Settings for POST /task.
        list_tasks: Settings for GET /tasks.

    Attributes:
        post_task: Settings for POST /task.
        list_tasks: Settings for GET /tasks.
    """

    post_task: PostTask = PostTask()
    list_tasks: ListTasks = ListTasks()


def split_server_url(url: str, name: str) -> SplitResult:
    """Split the URL of a server, checking its scheme, host and port.

    Refuses user info, as FOCA does for backends, since URLs are logged. Error
    messages leave out the URL, as it may contain credentials, and FOCA shows
    them.

    Args:
        url: URL, e.g., of a TES backend or an identity provider.
        name: What the URL is of, for error messages.

    Returns:
        Parts of the URL.

    Raises:
        ValueError: The URL is malformed, has no scheme or host, has an
            invalid port, or has user info.
    """
    try:
        parts = urlsplit(url)
        # `port` raises for ports that are not numbers in the valid range
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not (parts.scheme and parts.hostname):
        raise ValueError(f"{name} without scheme or host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{name} must not contain user info")
    return parts


class ClientCredentials(ConfigModel):
    """Client of proTES at its identity provider (OAuth 2.0 client credentials).

    proTES gets its own access tokens with the client credentials grant (RFC
    6749, section 4.4) for the TES backends with `auth: client_credentials`.
    The client ID and secret are read from the environment variables
    `PROTES_CLIENT_ID` and `PROTES_CLIENT_SECRET`, never from the config.

    Args:
        issuer: Issuer of the identity provider; its token endpoint is taken
            from its OpenID Connect discovery document unless
            `token_endpoint` is set.
        token_endpoint: Token endpoint of the identity provider.
        scope: Scope of the requested tokens (space-separated), if any.
        auth_method: How the client authenticates at the token endpoint:
            with HTTP Basic (`client_secret_basic`) or in the request body
            (`client_secret_post`).
        refresh_before: Seconds before the expiry of a token at which a new
            one is fetched (at most half its lifetime).
        timeout: Timeout of requests to the identity provider, in seconds.

    Attributes:
        issuer: Issuer of the identity provider; its token endpoint is taken
            from its OpenID Connect discovery document unless
            `token_endpoint` is set.
        token_endpoint: Token endpoint of the identity provider.
        scope: Scope of the requested tokens (space-separated), if any.
        auth_method: How the client authenticates at the token endpoint:
            with HTTP Basic (`client_secret_basic`) or in the request body
            (`client_secret_post`).
        refresh_before: Seconds before the expiry of a token at which a new
            one is fetched (at most half its lifetime).
        timeout: Timeout of requests to the identity provider, in seconds.
    """

    issuer: str | None = None
    token_endpoint: str | None = None
    scope: str | None = None
    auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_basic"
    )
    refresh_before: PositiveInt = 60
    timeout: PositiveInt = 10


# Hint of the startup error for the keys replaced by `custom.tes.backends`
BACKENDS_HINT = (
    "list the TES backends in `custom.tes.backends` instead: first the"
    " entries of `service_list`, in order, each with `type: ga4gh-tes` (and"
    " its `url`, `auth` and `basic`), then the registry of `discovery`, if"
    " any, as one entry with `type: ga4gh-service-registry`, `url` set to"
    " its `registry_url` and its other settings; entries with `http` URLs"
    " need `url_schemes: [https, http]` (see Upgrading in the docs)"
)


DEFAULT_API_PATH = "/ga4gh/tes/v1"
"""Path of the TES API, relative to the URL of a backend, as the TES
specification has it; that of the API of proTES too."""

# Absolute paths of unreserved characters (RFC 3986, section 2.3)
_API_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)+")


class TesServiceEntry(ServiceEntry):
    """TES instance of `custom.tes.backends`, with the settings of proTES.

    Args:
        api_path: Path of the TES API, relative to `url`, for TES instances
            that serve it elsewhere than the TES specification says; `/` if
            it is at `url` itself.
        **data: Fields of :py:class:`foca.plugins.backends.ServiceEntry`.

    Attributes:
        api_path: Path of the TES API, relative to `url`, without trailing
            slash (empty if it is at `url` itself).
    """

    api_path: str = DEFAULT_API_PATH

    @field_validator("api_path")
    @classmethod
    def check_api_path(cls, value: str) -> str:
        """Check the API path, and remove trailing slashes.

        Args:
            value: API path, as given.

        Returns:
            The API path.

        Raises:
            ValueError: The path is not absolute, has characters other than
                unreserved ones (e.g., a query), or segments `.` or `..`.
        """
        if not value.startswith("/"):
            raise ValueError("API path must be absolute")
        path = value.removesuffix("/")
        if path and (
            not _API_PATH.fullmatch(path) or {".", ".."} & set(path.split("/"))
        ):
            raise ValueError(
                "API path must have unreserved characters only, and no segments"
                " `.` or `..`"
            )
        return path


class Tes(ConfigModel):
    """TES backend configuration.

    Args:
        backends: TES backends that tasks may be forwarded to, in order:
            configured ones (`type: ga4gh-tes`) and service registries whose
            verified TES services are used (`type: ga4gh-service-registry`;
            cf. :py:mod:`foca.plugins.backends`; TES instances also with
            `api_path`, cf. :py:class:`TesServiceEntry`).
        url_schemes: URL schemes allowed for the identity provider of
            `client_credentials` (those of backends and registries are set
            per entry, `url_schemes`).
        client_credentials: Client of proTES at its identity provider, for
            the backends with `auth: client_credentials`.
        timeout: Timeout of requests to TES backends, in seconds (of
            connecting, at most 5 seconds); requests to create or cancel
            tasks are not sent again after timeouts, so it should be long
            enough for slow backends, but short enough that requests to
            create tasks (which may try several backends) stay below the
            read timeout of a reverse proxy in front of proTES.
        version_cache_ttl: Seconds that the TES version of a backend, as read
            from its service info, is cached (per process); also the API
            path of each discovered backend.
        probe_with_token: Whether the service info of a backend is read for
            its TES version (and API path, if discovered) with the
            credentials of the backend (e.g., proTES' access token); by
            default, it is read without credentials, as service infos are
            public, so that backends that tasks don't go to don't get them.

    Attributes:
        backends: TES backends that tasks may be forwarded to, in order.
        url_schemes: URL schemes allowed for the identity provider of
            `client_credentials`.
        client_credentials: Client of proTES at its identity provider, for
            the backends with `auth: client_credentials`.
        timeout: Timeout of requests to TES backends, in seconds.
        version_cache_ttl: Seconds that the TES version of a backend, as read
            from its service info, is cached (per process); also the API
            path of each discovered backend.
        probe_with_token: Whether the service info of a backend is read for
            its TES version (and API path, if discovered) with the
            credentials of the backend.
    """

    backends: BackendListConfig = BackendListConfig([])
    url_schemes: list[Literal["https", "http"]] = Field(default=["https"], min_length=1)
    client_credentials: ClientCredentials = ClientCredentials()
    timeout: PositiveInt = 15
    version_cache_ttl: PositiveInt = 3600
    probe_with_token: bool = False

    @field_validator("backends", mode="before")
    @classmethod
    def parse_tes_instances(cls, value: object) -> object:
        """Parse TES instances with the settings of proTES.

        FOCA's backend list keeps entries that are instances of its models as
        they are (also :py:class:`foca.plugins.backends.BackendList`), so the
        settings are available from the entries of its candidates. As the
        field has FOCA's type, `model_dump()` leaves them out: don't load the
        config from such a dump.

        Args:
            value: Backend list, as given.

        Returns:
            The backend list, with entries of TES instances parsed.
        """
        if not isinstance(value, list):
            return value
        return [
            TesServiceEntry.model_validate(entry)
            if isinstance(entry, dict) and entry.get("type") == "ga4gh-tes"
            else entry
            for entry in value
        ]

    @model_validator(mode="after")
    def check_version_labels(self) -> Self:
        """Refuse labels `tes.version` of versions that proTES doesn't support.

        Returns:
            The configuration.

        Raises:
            ValueError: An entry has such a label.
        """
        for entry in self.backends.root:
            label = entry.labels.get("tes.version")
            if label is not None and TesVersion.parse(label) not in SUPPORTED:
                raise ValueError(
                    f"Label `tes.version` of {entry.url}: {label!r} is none of the"
                    f" supported TES versions ({sorted(map(str, SUPPORTED))})"
                )
        return self

    @property
    def uses_tokens(self) -> bool:
        """Whether a backend has, or discovered ones get, proTES' token."""
        return any(entry.auth == "client_credentials" for entry in self.backends.root)

    @model_validator(mode="before")
    @classmethod
    def check_removed_keys(cls, data: object) -> object:
        """Refuse the keys replaced by `backends`, with the mapping.

        Args:
            data: TES backend configuration, as given.

        Returns:
            The configuration, unchanged.

        Raises:
            ValueError: The configuration has `service_list` or `discovery`.
        """
        if isinstance(data, dict):
            removed = [key for key in ("service_list", "discovery") if key in data]
            if removed:
                keys = " and ".join(f"`custom.tes.{key}`" for key in removed)
                raise ValueError(f"{keys} removed: {BACKENDS_HINT}")
        return data

    @model_validator(mode="after")
    def check_urls(self) -> "Tes":
        """Check the URLs of the identity provider if it is needed.

        Returns:
            The validated configuration.

        Raises:
            ValueError: A URL of the identity provider is invalid or has a
                scheme that is not allowed; or a backend has (or discovered
                backends get) `auth: client_credentials`, but neither the
                issuer nor the token endpoint is set.
        """
        idp = self.client_credentials
        for name, value in (
            ("issuer", idp.issuer),
            ("token endpoint", idp.token_endpoint),
        ):
            if value is None:
                continue
            setting = f"URL of the {name} of `client_credentials`"
            parts = split_server_url(value, setting)
            if parts.scheme not in self.url_schemes:
                raise ValueError(
                    f"Scheme of the {setting} not allowed (`url_schemes`:"
                    f" {self.url_schemes})"
                )
        if self.uses_tokens and idp.issuer is None and idp.token_endpoint is None:
            raise ValueError(
                "TES backends with `auth: client_credentials` need"
                " `client_credentials.issuer` or `token_endpoint`"
            )
        return self


class ServiceInfo(ConfigModel):
    """What the service info tells about the TES backends.

    Args:
        list_backends: Whether the service info, which anyone can read, lists
            the TES backends (`backends`); its field `gateway` stays either
            way.

    Attributes:
        list_backends: Whether the service info lists the TES backends.
    """

    list_backends: bool = True


class CustomConfig(ConfigModel):
    """Custom app configuration.

    Args:
        controllers: All controller-related config.
        tes: TES backends and how proTES calls them.
        service_info: What the service info tells about the TES backends.

    Attributes:
        controllers: All controller-related config.
        tes: TES backends and how proTES calls them.
        service_info: What the service info tells about the TES backends.
    """

    controllers: Controllers = Controllers()
    tes: Tes = Tes()
    service_info: ServiceInfo = ServiceInfo()

    @model_validator(mode="before")
    @classmethod
    def check_removed_keys(cls, data: object) -> object:
        """Refuse removed keys, naming what replaces them.

        Args:
            data: Custom config, as given.

        Returns:
            The custom config, unchanged.

        Raises:
            ValueError: The config has `access_control`, whose only setting
                (`require_admin`) moved to FOCA's
                `security.access_control.require_admin`, or `storeLogs`,
                whose only setting (`execution_trace`) set whether forwards
                were recorded in `logs[].metadata.forwarded_to`, which is
                no longer served (cf. the gateway tags of tasks).
        """
        if isinstance(data, dict) and "access_control" in data:
            raise ValueError(
                "`custom.access_control` was removed: set "
                "`security.access_control.require_admin` instead of "
                "`custom.access_control.require_admin` (see Upgrading in the "
                "docs)"
            )
        if isinstance(data, dict) and "storeLogs" in data:
            raise ValueError(
                "`custom.storeLogs` was removed: forwards are no longer "
                "recorded in `logs[].metadata.forwarded_to`; tasks have the "
                "gateway tags `gateway.backend` and `gateway.backend_task_id` "
                "instead (see Upgrading in the docs)"
            )
        return data
