"""Service clients: apps that create and own tasks with their own tokens.

Off by default. Deployments allow service clients explicitly, by issuer and
client ID, in FOCA's `security.auth.service_clients`, and give each the role
`user` in `security.access_control.client_mapping`. A listed client may then
create tasks with its own client-credentials token (a service token, cf.
:py:func:`foca.security.auth.get_service_client`), and, as their owner, list,
read and cancel them; nothing beyond: it sees no tasks of others, and others
don't see its tasks (cf. :py:mod:`pro_tes.ga4gh.tes.ownership`). Its tasks
are relayed to the TES backends with proTES' own credentials, as all tasks;
its token is not passed on.

Identity providers may put only the client's own ID into the audience of its
service tokens, so deployments accept that ID as an audience
(`security.auth.audience`). As that would also accept other tokens for the
client (e.g., tokens of users obtained by it, by login or token exchange),
the audience of a service client is accepted for that client's own service
tokens only (cf. :py:func:`check_service_audience`), on all endpoints.

Clients acting for users (`on_behalf_of`) are not supported: tasks belong to
the caller of their token, and token exchange (RFC 8693) is the way to pass
users on to proTES.
"""

import logging
import os
from typing import Any

from foca import get_config
from foca.context import current_request
from foca.errors import Unauthorized
from foca.models.config import AuthConfig
from foca.security.auth import get_service_client
from foca.utils.logging import escape_log_value
import jwt

from pro_tes.ga4gh.tes.backends import CLIENT_ID_ENV

logger = logging.getLogger(__name__)

# The only role of service clients (`security.access_control.client_mapping`)
ROLE = "user"
# Challenge of rejected tokens, as FOCA's for invalid tokens (RFC 6750, §3.1)
_CHALLENGE = (
    'Bearer error="invalid_token", '
    'error_description="The access token could not be validated"'
)


def check_service_clients() -> None:
    """Check the service clients on startup, and log them.

    Must be called in an app context. Without service clients (the default),
    only the header naming the users they act for is refused. With service
    clients, the app also checks the audience of each request's token (cf.
    :py:func:`check_service_audience`).

    Raises:
        RuntimeError: The header naming users (`on_behalf_of_header`) is set,
            a service client may act for users (`on_behalf_of`), has no
            explicit `issuer`, is not mapped to the role `user` (and only
            to it) in `client_mapping`, or is the client of proTES itself
            (`PROTES_CLIENT_ID`), whose tokens TES backends get.
    """
    security = get_config().security
    auth = security.auth
    if auth.on_behalf_of_header is not None or any(
        client.on_behalf_of for client in auth.service_clients
    ):
        raise RuntimeError(
            "Service clients can't act for users in proTES: tasks belong to"
            " the caller of their token; remove"
            " `security.auth.on_behalf_of_header` and `on_behalf_of` of"
            " `security.auth.service_clients`"
        )
    if not auth.service_clients:
        return
    mapping = security.access_control.client_mapping or {}
    for client in auth.service_clients:
        if client.issuer is None:
            raise RuntimeError(
                f"Service client {client.client_id}: set its `issuer`"
                " (`security.auth.service_clients`), as client IDs are unique"
                " per issuer only"
            )
        if mapping.get(client.client_id) != ROLE:
            raise RuntimeError(
                f"Service client {client.client_id}: map it to the role"
                f" '{ROLE}' (`security.access_control.client_mapping`), its"
                " only role: service clients create and own tasks, nothing"
                " beyond"
            )
    own = os.environ.get(CLIENT_ID_ENV)
    if own and own in {client.client_id for client in auth.service_clients}:
        raise RuntimeError(
            f"proTES' own client ({CLIENT_ID_ENV}) can't be a service client:"
            " TES backends get its tokens, so they could use them at proTES"
        )
    _warn_missing_audiences(auth)
    logger.info(
        "Service clients that may create tasks and own them: %s",
        ", ".join(
            f"{client.client_id} (issuer {client.issuer})"
            for client in auth.service_clients
        ),
    )


def _warn_missing_audiences(auth: AuthConfig) -> None:
    """Warn of service clients whose ID is not an accepted audience of its issuer.

    Their service tokens are accepted only if they name another accepted
    audience, but identity providers may put only the client's own ID into
    them.

    Args:
        auth: Authentication settings, with service clients of explicit
            issuers.
    """
    if not auth.verify_audience:
        return
    for client in auth.service_clients:
        if client.client_id not in (auth.get_audience(str(client.issuer)) or []):
            logger.warning(
                "Service client %s is not an accepted audience of its issuer"
                " (`security.auth.audience`): its tokens are accepted only for"
                " the other audiences; add it if its identity provider puts"
                " only its own client ID into `aud`",
                client.client_id,
            )


def _audiences(claims: dict[str, Any]) -> set[str]:
    """Get the audiences of a token.

    Args:
        claims: Claims of the token.

    Returns:
        The values of `aud`, a string or a list (RFC 7519, §4.1.3); none if
        it is of another type. Strings of lists only.
    """
    aud = claims.get("aud")
    if isinstance(aud, str):
        return {aud}
    if isinstance(aud, list):
        return {value for value in aud if isinstance(value, str)}
    return set()


def check_service_audience() -> None:
    """Accept the audience of a service client for its own service tokens only.

    Runs before each request if there are service clients (cf.
    :py:func:`check_service_clients` and :py:func:`pro_tes.app.init_app`),
    on all endpoints. A token whose accepted audiences (those of its issuer,
    cf. :py:meth:`foca.models.config.AuthConfig.get_audience`) are only IDs
    of service clients must be the service token of that one client;
    otherwise the request is rejected as with an invalid token. Tokens for
    any other accepted audience are left to FOCA, as without service
    clients.

    The claims are read without verifying the token, which FOCA verifies
    afterwards, for the operations that take tokens: this check only
    rejects, so a forged token gains nothing from it. Requests without a
    bearer token, with a token that can't be decoded, or of an untrusted
    issuer are left to FOCA, too, and audiences are not checked at all
    without audience validation (`security.auth.verify_audience`).

    Raises:
        foca.errors.Unauthorized: The token names only audiences of service
            clients, but is not the service token of that client; with an
            `invalid_token` challenge, without the reason.
    """
    authorization = current_request().headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return
    try:
        # Read only to reject (see above); FOCA verifies the token
        # nosemgrep: jwt-decode-without-verification
        claims = jwt.decode(
            token,
            # nosemgrep: unverified-jwt-decode
            options={"verify_signature": False},
        )
    except jwt.PyJWTError:
        return
    auth = get_config().security.auth
    issuer = claims.get("iss")
    if not auth.verify_audience or not isinstance(issuer, str):
        return
    if issuer not in auth.issuer_urls:
        return
    matched = _audiences(claims) & set(auth.get_audience(issuer) or [])
    clients = {client.client_id for client in auth.service_clients}
    if not matched or matched - clients:
        return
    client = get_service_client(claims=claims, conf=auth)
    if client is not None and matched == {client.client_id}:
        return
    # Names the audiences (client IDs of the config), not the token
    logger.info(
        "Token rejected: accepted only for the audience of service clients"
        " (%s), but no service token of that client",
        escape_log_value(", ".join(sorted(matched))),
    )
    # FOCA's class, not Connexion's of `pro_tes.exceptions`, as it takes the
    # challenge (`WWW-Authenticate`) as headers
    raise Unauthorized(
        "Token for the audience of a service client, but no service token of"
        " that client",
        headers={"WWW-Authenticate": _CHALLENGE},
    )
