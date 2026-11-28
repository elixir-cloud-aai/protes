"""Owners of tasks.

The user creating a task (the subject of its access token, cf.
:py:func:`foca.security.access_control.get_current_subject`) becomes its
owner, or, with its own token, the service client creating it (cf.
:py:mod:`pro_tes.service_clients`); only owners may read and cancel it
(enforced by the policies, cf. `security.access_control` in `config.yaml`);
listings only show the tasks of the caller. Tasks are not shared, and admins
don't see the tasks of others.

Owners are identified by a string, as stored with the task (`user_id`): the
subject of a user, or the client ID of a service client with the prefix
`client:` (cf. :py:func:`owner_subject`), so that users and clients of the
same identifier are different owners.

Ownership is stored as resource-scoped roles of FOCA (`g2` rules) on the
request paths of a task, so that FOCA enforces it: the task itself (e.g.,
`/ga4gh/tes/v1/tasks/ABC123`) and its cancellation
(`/ga4gh/tes/v1/tasks/ABC123:cancel`), which is not a sub-path of the task,
so that roles on the task don't apply to it.
"""

from typing import Literal

from foca.security.access_control import (
    add_owner,
    get_current_client,
    get_current_subject,
    list_resource_roles,
    list_subject_roles,
    remove_resource_role,
)
from foca.security.access_control.constants import CLIENT_PREFIX, OWNER_ROLE

from pro_tes.exceptions import Unauthorized

# Suffix of the path of the cancellation of a task (`/tasks/{id}:cancel`)
CANCEL_SUFFIX = ":cancel"


def get_owner() -> str:
    """Get the owner of a task created by the current request.

    Returns:
        The service client of the current request, prefixed with `client:`
        (cf. :py:func:`foca.security.access_control.get_current_client`),
        or else its user (cf.
        :py:func:`foca.security.access_control.get_current_subject`).

    Raises:
        pro_tes.exceptions.Unauthorized: The request has no identified user,
            e.g., because its token lacks the identity claim, or the user's
            subject starts with `client:`, as owners that are clients do.
    """
    client = get_current_client()
    if client is not None:
        return f"{CLIENT_PREFIX}{client}"
    owner = get_current_subject()
    if owner is None or owner.startswith(CLIENT_PREFIX):
        raise Unauthorized("User could not be identified")
    return owner


def owner_subject(owner: str) -> tuple[Literal["user", "client"], str]:
    """Get the subject of the access control rules of an owner.

    Args:
        owner: Owner of a task (cf. :py:func:`get_owner`).

    Returns:
        The type of the subject (`client` for owners prefixed with
        `client:`, else `user`), and the subject, without prefix.
    """
    if owner.startswith(CLIENT_PREFIX):
        return "client", owner.removeprefix(CLIENT_PREFIX)
    return "user", owner


def task_resources(collection: str, task_id: str) -> list[str]:
    """Get the resources (request paths) owners of a task have a role on.

    Args:
        collection: Request path of the tasks (e.g., `/ga4gh/tes/v1/tasks`).
        task_id: Identifier of the task.

    Returns:
        The path of the task and of its cancellation.
    """
    task = f"{collection.rstrip('/')}/{task_id}"
    return [task, f"{task}{CANCEL_SUFFIX}"]


def task_has_roles(collection: str, task_id: str) -> bool:
    """Check whether roles are granted on a task (or its cancellation).

    Roles may be left by deleted tasks, or granted to a task being created
    concurrently; their identifiers must not be used for new tasks, so
    that their owners don't get access to them.

    Args:
        collection: Request path of the tasks (e.g., `/ga4gh/tes/v1/tasks`).
        task_id: Identifier of the task.

    Returns:
        Whether any subject (users, groups, clients and roles) has a role on
        the task or its cancellation.
    """
    return any(
        list_resource_roles(resource=resource)
        for resource in task_resources(collection=collection, task_id=task_id)
    )


def add_task_owner(collection: str, task_id: str, owner: str) -> None:
    """Make a user or service client the owner of a task.

    Args:
        collection: Request path of the tasks (e.g., `/ga4gh/tes/v1/tasks`).
        task_id: Identifier of the task.
        owner: Owner (cf. :py:func:`get_owner`): a user identifier, or a
            client ID prefixed with `client:`.
    """
    subject_type, subject = owner_subject(owner)
    for resource in task_resources(collection=collection, task_id=task_id):
        add_owner(resource=resource, subject=subject, subject_type=subject_type)


def remove_task_owner(collection: str, task_id: str, owner: str) -> None:
    """Revoke the owner role of an owner on a task (cf. :py:func:`add_task_owner`).

    Args:
        collection: Request path of the tasks (e.g., `/ga4gh/tes/v1/tasks`).
        task_id: Identifier of the task.
        owner: Owner (cf. :py:func:`get_owner`).
    """
    subject_type, subject = owner_subject(owner)
    for resource in task_resources(collection=collection, task_id=task_id):
        remove_resource_role(
            subject=subject,
            role=OWNER_ROLE,
            resource=resource,
            subject_type=subject_type,
        )


def owned_tasks_filter(collection: str) -> dict[str, dict[str, list[str]]]:
    """Get a MongoDB filter for the tasks of the caller (user or service client).

    Unlike :py:func:`foca.security.access_control.resource_filter`, the
    filter is not empty for admins: they only see their own tasks, too.

    Args:
        collection: Request path of the tasks (e.g., `/ga4gh/tes/v1/tasks`).

    Returns:
        A filter for the stored tasks (`task.id`) that the current user owns,
        in the order of their identifiers.
    """
    ids = {
        role.id
        for role in list_subject_roles(prefix=collection)
        if role.role == OWNER_ROLE and not role.id.endswith(CANCEL_SUFFIX)
    }
    return {"task.id": {"$in": sorted(ids)}}
