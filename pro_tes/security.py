"""Security checks on startup."""

import logging
import os
from typing import NamedTuple

from foca import get_config, get_custom_config
from foca.models.config import Config
from foca.plugins.backends import BackendList, RegistryEntry, ServiceEntry

from pro_tes.config_models import CustomConfig, Tes, TesServiceEntry
from pro_tes.exceptions import ApiKeyNotSet, BackendAuthError
from pro_tes.ga4gh.tes.backends import CLIENT_ID_ENV, CLIENT_SECRET_ENV, read_api_key

logger = logging.getLogger(__name__)


def _groups_of_role(mapping: dict[str, str], role: str) -> list[str]:
    """Get the groups mapped to a role.

    Args:
        mapping: Group mapping (`security.access_control.group_mapping`).
        role: Role.

    Returns:
        The groups mapped to the role, sorted.
    """
    return sorted(group for group, value in mapping.items() if value == role)


def check_task_ownership() -> None:
    """Check that only the owners of tasks can access them.

    Owners are enforced by access control (cf.
    :py:mod:`pro_tes.ga4gh.tes.ownership`), so it must be enabled and
    enforced. Tasks are owned by the callers of their access tokens: users,
    or service clients with their own tokens, which are checked separately
    (cf. :py:func:`pro_tes.service_clients.check_service_clients`). Must be
    called in an app context.

    Raises:
        RuntimeError: Access control is not enabled and enforced.
    """
    conf = get_config().security.access_control
    if not (conf.enabled and conf.enforce):
        raise RuntimeError(
            "Access control must be enabled and enforced "
            "(`security.access_control.enabled` and `enforce`), so that only "
            "the owners of tasks can access them"
        )


RENAMED_RULES = {
    "users-create-list-tasks": "users-create-tasks` and `authenticated-list-tasks",
}


def check_renamed_rules(config: Config) -> None:
    """Refuse rules under the names of rules that were renamed.

    A config overlay that names a renamed rule would add it next to the new
    rules, not change them: e.g., the overlay that earlier docs gave to
    stop task creation (`users-create-list-tasks` for listing only) would
    let users create tasks again. Must be called before the app is created,
    which stores the rules in the database for all instances.

    Args:
        config: The app's config.

    Raises:
        RuntimeError: A rule has the name of a renamed rule.
    """
    policies = config.security.access_control.named_policies or {}
    for old, new in RENAMED_RULES.items():
        if old in policies:
            raise RuntimeError(
                f"Access control rule `{old}` was renamed to `{new}`, so "
                "a rule under the old name would be added next to them, not "
                "change them: rename it in your config (see the upgrade "
                "notes in `docs/upgrading.md`)"
            )


def check_user_groups() -> None:
    """Log the groups mapped to the role `user`, or warn if there are none.

    Nobody can create tasks without such groups. FOCA checks the
    groups of the role `admin` (`security.access_control.require_admin`) and
    that groups are only mapped with a group claim (`claim_groups`). Must be
    called in an app context.
    """
    mapping = get_config().security.access_control.group_mapping or {}
    users = _groups_of_role(mapping, "user")
    if users:
        logger.info("Groups mapped to the role 'user': %s", ", ".join(users))
    else:
        logger.warning(
            "No group is mapped to the role 'user', so nobody can create tasks"
        )


def _missing_env(*names: str) -> list[str]:
    """Get the environment variables that are not set (or empty).

    Args:
        *names: Names of environment variables.

    Returns:
        The names of the variables that are not set, or empty.
    """
    return [name for name in names if not os.environ.get(name)]


def _check_registry(entry: RegistryEntry) -> None:
    """Log how the backends of a registry entry are discovered and called.

    Args:
        entry: Registry entry of `custom.tes.backends`.
    """
    logger.info(
        "Discovering TES backends at registry %s every %d seconds, with `auth: %s`",
        entry.url,
        entry.refresh_interval,
        entry.auth,
    )
    if entry.auth == "none":
        logger.warning(
            "TES backends discovered at registry %s get no credentials (`auth: none`)",
            entry.url,
        )
    if entry.allow_private_addresses:
        logger.warning(
            "TES backends discovered at registry %s may have private"
            " addresses (`allow_private_addresses`); for local setups only",
            entry.url,
        )
    if entry.registry_allow_private_addresses:
        logger.warning(
            "Registry %s may have a private address"
            " (`registry_allow_private_addresses`)",
            entry.url,
        )


class _MissingCredentials(NamedTuple):
    """Credentials of a configured backend that are not set.

    Attributes:
        env: Environment variables with its HTTP Basic credentials that are
            not set.
        api_key: Whether its API key is not set (named by the URL of the
            backend, not by the variable, which may be a key pasted as
            `key_env` by mistake).
    """

    env: list[str]
    api_key: bool = False


def _check_service(entry: ServiceEntry) -> _MissingCredentials:
    """Log how a configured backend is called.

    Args:
        entry: Configured backend of `custom.tes.backends`.

    Returns:
        Its credentials that are not set, if any.

    Raises:
        RuntimeError: The backend has `auth: basic`, but no `basic`; or its
            API key can't be used (cf. :py:func:`_check_api_key`).
    """
    if entry.auth == "basic":
        logger.warning(
            "TES backend %s: HTTP Basic credentials (`auth: basic`), a"
            " transitional mode for backends that don't accept tokens",
            entry.url,
        )
        if entry.basic is None:
            # prevented by validation, unless the config was changed later
            raise RuntimeError(
                f"TES backend {entry.url}: `auth: basic` without `basic`"
            )
        return _MissingCredentials(
            env=_missing_env(entry.basic.username_env, entry.basic.password_env)
        )
    if entry.auth == "api_key":
        return _MissingCredentials(env=[], api_key=not _check_api_key(entry))
    if entry.auth == "none":
        logger.warning("TES backend %s: no authentication (`auth: none`)", entry.url)
    return _MissingCredentials(env=[])


def _check_api_key(entry: ServiceEntry) -> bool:
    """Log that a configured backend gets an API key, and check the key.

    Args:
        entry: Configured backend of `custom.tes.backends`, with `auth:
            api_key`.

    Returns:
        Whether the key is set: in the environment variable `key_env`, or in
        the file named in `<key_env>_FILE` (not if that is empty).

    Raises:
        RuntimeError: The backend has no `api_key`, or its key can't be used
            (both variables are set, the file can't be read, or the key has
            characters that headers can't carry); the message names the
            backend, not the variables, nor the key (a key pasted as
            `key_env` by mistake may be a valid variable name).
    """
    api_key = entry.api_key if isinstance(entry, TesServiceEntry) else None
    if api_key is None:
        # prevented by validation, unless the config was changed later
        raise RuntimeError(
            f"TES backend {entry.url}: `auth: api_key` without `api_key`"
        )
    # names the header (config), not the key
    # nosemgrep  # logger-credential-disclosure: see above
    logger.warning(
        "TES backend %s: API key (`auth: api_key`, header %s), a transitional"
        " mode for backends that don't accept tokens of proTES; tasks there are"
        " owned by the account of the key at the backend",
        entry.url,
        api_key.header,
    )
    try:
        read_api_key(api_key.key_env)
    except ApiKeyNotSet:
        # also if the file is empty
        return False
    except BackendAuthError as exc:
        # the messages name neither the variables nor the key
        raise RuntimeError(f"TES backend {entry.url}: {exc}") from None
    return True


def check_backends(conf: Tes | None = None, public_url: str | None = None) -> None:
    """Check the TES backends and their credentials, and log how they are used.

    A warning is logged if no backend is configured, for each backend
    without token authentication (`auth: basic` or `none`), for registries
    whose backends get no tokens or may have private addresses, and for
    entries that allow URLs with scheme `http`.

    Args:
        conf: TES backend configuration; by default, that of the current app
            (in an app context).
        public_url: Public URL of proTES (`server.public_url`); by default,
            that of the current app if `conf` is not given (callers that
            give `conf` give the public URL of the same config).

    Raises:
        RuntimeError: The list has registries, but proTES has no public URL
            (so that it might discover itself), or configured backends at or
            below the public URL (which are never used). Or environment
            variables with credentials are not set: the client ID and secret
            of proTES if a backend has (or discovered backends get) `auth:
            client_credentials`, the HTTP Basic credentials of a backend
            with `auth: basic`, or the API key of a backend with `auth:
            api_key`, which is named by the URL of the backend (not by the
            variable, which may be a key pasted as `key_env`). Or the API
            key of a backend can't be used.
    """
    if conf is None:
        conf = get_custom_config(CustomConfig).tes
        if public_url is None:
            public_url = get_config().server.public_url
    entries = conf.backends.root
    if not entries:
        logger.warning(
            "No TES backends are configured (`custom.tes.backends`), so tasks"
            " can't be forwarded"
        )
    registries = [entry for entry in entries if isinstance(entry, RegistryEntry)]
    if registries and public_url is None:
        raise RuntimeError(
            "TES backends are discovered in registries (`custom.tes.backends`),"
            " but `server.public_url` is not set: set it to the public URL of"
            " proTES, so that proTES never forwards tasks to itself"
        )
    http = [entry.id for entry in entries if "http" in entry.url_schemes]
    if http:
        logger.warning(
            "URLs with scheme 'http' are allowed (`url_schemes`) for %s:"
            " credentials are sent unencrypted; for local tests only",
            ", ".join(http),
        )
    if "http" in conf.url_schemes:
        logger.warning(
            "URLs with scheme 'http' are allowed for the identity provider"
            " (`custom.tes.url_schemes`): credentials are sent unencrypted; for"
            " local tests only"
        )
    missing: list[str] = []
    missing_keys: list[str] = []
    for entry in entries:
        if isinstance(entry, RegistryEntry):
            _check_registry(entry)
            continue
        credentials = _check_service(entry)
        missing.extend(credentials.env)
        if credentials.api_key:
            missing_keys.append(entry.url)
    # after the checks of the entries, which the backend list validates again
    own = [
        entry.url
        for entry in entries
        if isinstance(entry, ServiceEntry)
        and not BackendList([entry], api="tes", public_url=public_url).expand()
    ]
    if own:
        raise RuntimeError(
            f"TES backends at or below the public URL of proTES ({public_url},"
            " `server.public_url`) would never be used, as proTES never"
            f" forwards tasks to itself: {', '.join(own)}; serve proTES under a"
            " host or path prefix of its own (and set `server.public_url` to"
            " it), or move the backends to other hosts"
        )
    # registries too: the backends that their admins verify get the token
    token_recipients = [
        entry.url
        if isinstance(entry, ServiceEntry)
        else f"those verified by registry {entry.url}"
        for entry in entries
        if entry.auth == "client_credentials"
    ]
    if token_recipients:
        parameter = conf.client_credentials.audience_parameter
        audiences = (
            ""
            if parameter is None
            else f" (each for its own audience, requested with `{parameter}`)"
        )
        # logs the URLs of the backends, not tokens
        # nosemgrep  # logger-credential-disclosure: see above
        logger.info(
            "TES backends with access tokens of proTES%s: %s",
            audiences,
            ", ".join(token_recipients),
        )
    if conf.uses_tokens:
        missing.extend(_missing_env(CLIENT_ID_ENV, CLIENT_SECRET_ENV))
    if missing or missing_keys:
        parts = []
        if missing:
            parts.append(f"the environment variables {', '.join(sorted(set(missing)))}")
        if missing_keys:
            parts.append(
                "the API keys of TES backends"
                f" {', '.join(sorted(set(missing_keys)))} (the variables that"
                " their `key_env` names, or their `_FILE` variables)"
            )
        raise RuntimeError(
            f"Credentials of TES backends missing: set {'; and '.join(parts)}"
        )
