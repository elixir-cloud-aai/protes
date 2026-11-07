"""Class implementing TES API-server-side controller methods."""

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from functools import cached_property
from http import HTTPStatus
from itertools import zip_longest
import logging
import re
import time
from typing import Any, NamedTuple

from bson.errors import InvalidId
from bson.objectid import ObjectId
from celery import uuid
from foca import get_collection, get_custom_config
from foca.plugins import (
    Attempt,
    Backend,
    BackendResponse,
    GatewayError,
    InboundRequest,
    NoBackendAvailable,
    Principal,
    ProvenanceStore,
)
from foca.plugins.engine import PipelineRun
from foca.plugins.runtime import get_host
from foca.plugins.tags import creation_tags, routing_tags, without_server_tags
from foca.utils.misc import generate_id
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError
import requests
from tes.models import Task

from pro_tes.config_models import CustomConfig
from pro_tes.exceptions import (
    BackendAuthError,
    BackendOutcomeUnknown,
    BackendRefused,
    BadRequest,
    NoEligibleBackend,
    ProvenanceNotFound,
    TaskNotCancelable,
    TaskNotFound,
)
from pro_tes.ga4gh.tes.backends import (
    TesClient,
    backend_version,
    backend_versions,
    client_for,
    create_client,
    eligible_backends,
    forward_reason,
    get_backends,
)
from pro_tes.ga4gh.tes.models import (
    DbDocument,
    TesEndpoint,
    TesState,
    TesTask,
    TesTaskLog,
)
from pro_tes.ga4gh.tes.ownership import (
    add_task_owner,
    get_owner,
    owned_tasks_filter,
    remove_task_owner,
    task_has_roles,
)
from pro_tes.ga4gh.tes.states import States
from pro_tes.ga4gh.tes.versions import incompatibilities
from pro_tes.gateway import (
    check_inline_content,
    handle_url_credentials,
    relayed_task,
    validate_task,
)
from pro_tes.limits import check_task_limits
from pro_tes.tasks.track_task_progress import (
    CANCELED_AT_FIELD,
    task__track_task_progress,
)
from pro_tes.utils.db import (
    FAILED_RUNS_COLLECTION,
    PIPELINES_COLLECTION,
    PLUGIN_BUILDS_COLLECTION,
    TASK_COLLECTION,
    TASK_DB,
    TASK_PROJECTION,
    DbDocumentConnector,
)
from pro_tes.utils.misc import utc_now
from pro_tes.utils.models import TaskModelConverter

logger = logging.getLogger(__name__)

# view of `GET /tasks` and `GET /tasks/{id}` if none is given, as in the spec
DEFAULT_VIEW = "MINIMAL"


def tags_filter(keys: list[str], values: list[str]) -> dict[str, Any]:
    """Get the database filter of the tag filters of `GET /tasks` (TES 1.1).

    Keys and values are zipped, in order: a missing or empty value matches
    any value of the tag, also an empty one. Tag keys are taken literally by
    `$getField` (MongoDB 5.0 or later): they may contain dots (e.g.,
    `gateway.backend`) and start with `$`, unlike the keys of dotted paths.

    Args:
        keys: Keys of the tags (`tag_key`).
        values: Values of the tags (`tag_value`).

    Returns:
        The filter; empty without keys.

    Raises:
        pro_tes.exceptions.BadRequest: There are more values than keys, or a
            key or value has null characters.
    """
    if len(values) > len(keys):
        raise BadRequest("More values (`tag_value`) than keys (`tag_key`) of tags")
    if any("\x00" in item for item in (*keys, *values)):
        raise BadRequest("Invalid tag filter: null character")
    conditions: list[dict[str, Any]] = []
    for key, value in zip_longest(keys, values, fillvalue=""):
        tag = {"$getField": {"field": {"$literal": key}, "input": "$task.tags"}}
        conditions.append(
            {"$eq": [tag, {"$literal": value}]}
            if value
            else {"$eq": [{"$type": tag}, "string"]}
        )
    return {"$expr": {"$and": conditions}} if conditions else {}


def _creation_tags(run: PipelineRun, provenance_path: str) -> dict[str, str]:
    """Get FOCA's gateway tags of a new task, without failing its creation.

    Args:
        run: Pipeline run of the request.
        provenance_path: Path of the task's provenance.

    Returns:
        The tags (cf. :py:func:`foca.plugins.tags.creation_tags`); none if
        they can't be built (e.g., if the provenance of the run can't be,
        which :py:meth:`foca.plugins.ProvenanceStore.save` records as
        `error: internal`).
    """
    try:
        return creation_tags(run, provenance_path=provenance_path)
    except Exception:
        logger.exception("Gateway tags of the task not built")
        return {}


class _Accepted(NamedTuple):
    """Attempt of the backend that accepted a task.

    Attributes:
        attempt: Attempt of the backend.
        cli: Client of the backend.
        response: Response of the backend.
        remote_task_id: Identifier of the task at the backend.
        tes_version: TES version that the task was relayed in.
    """

    attempt: Attempt
    cli: TesClient
    response: BackendResponse
    remote_task_id: str
    tes_version: str


# Answers of backends to the cancel of a task that they no longer have
GONE = (HTTPStatus.NOT_FOUND, HTTPStatus.GONE)
REFUSED = "TES instance refused to cancel a task"


def _cancel_at_backend(cli: Any, task_id: str, tracked: bool) -> bool:
    """Cancel a task at its backend.

    A refusal may mean that the task has finished, or that the backend no longer
    has it, or not (e.g., a `404` of a proxy), so the task is read once, unless
    nothing tracks it, which would have to store the result.

    Args:
        cli: Client of the backend.
        task_id: Identifier of the task at the backend.
        tracked: Whether polls track the task (not those of early versions).

    Returns:
        Whether the task is being canceled: also if the backend is doing so
        already, or no longer has it, which tracking handles; not if it has
        finished, whose final state is left to tracking.

    Raises:
        pro_tes.exceptions.BackendOutcomeUnknown: The backend wasn't reached, or
            may have canceled the task, but did not confirm it.
        pro_tes.exceptions.BackendRefused: The backend refused the request, and
            the task hasn't finished.
    """
    try:
        cli.cancel_task(task_id=task_id)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is None or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise BackendOutcomeUnknown(
                f"TES instance failed to cancel a task: {status}",
                reason=f"http_{status}",
            ) from exc
        return _after_refusal(cli=cli, task_id=task_id, tracked=tracked, cause=exc)
    except requests.RequestException as exc:
        raise BackendOutcomeUnknown(
            f"TES instance not reached to cancel a task: {type(exc).__name__}"
        ) from exc
    return True


def _after_refusal(
    cli: Any, task_id: str, tracked: bool, cause: requests.HTTPError
) -> bool:
    """Check what a backend's refusal to cancel a task means.

    Args:
        cli: Client of the backend.
        task_id: Identifier of the task at the backend.
        tracked: Whether polls track the task.
        cause: The refusal.

    Returns:
        Whether the task is being canceled (cf. :py:func:`_cancel_at_backend`).

    Raises:
        pro_tes.exceptions.BackendRefused: The task hasn't finished, or nothing
            tracks it.
    """
    if not tracked:
        raise BackendRefused(REFUSED) from cause
    try:
        state = cli.get_task(task_id=task_id).state
    except Exception as exc:
        # only if the refusal said so as well: a backend may also hide tasks
        response = getattr(exc, "response", None)
        if (
            getattr(response, "status_code", None) in GONE
            and cause.response is not None
            and cause.response.status_code in GONE
        ):
            return True
        logger.warning(
            "State of a task to cancel not available from its TES instance: %s",
            type(exc).__name__,
        )
        raise BackendRefused(REFUSED) from cause
    if state in States.FINISHED:
        return False
    if state == TesState.CANCELING.value:
        return True
    raise BackendRefused(REFUSED) from cause


class TaskRuns:
    """Class for TES API server-side controller methods.

    Attributes:
        custom_config: Custom configuration of the app.
        db_client: Database collection storing task objects.
    """

    def __init__(self) -> None:
        """Construct object instance."""
        self.custom_config: CustomConfig = get_custom_config(CustomConfig)
        self.db_client: Collection[dict[str, Any]] = get_collection(
            TASK_DB, TASK_COLLECTION
        )

    @cached_property
    def provenance(self) -> ProvenanceStore:
        """Store of the provenance of pipeline runs, in the database of the tasks.

        Created on first use (by `POST /tasks` only), so that the other
        endpoints don't depend on its collections.

        Returns:
            The store (cf. :py:class:`foca.plugins.ProvenanceStore`).
        """
        return ProvenanceStore(
            plugin_builds=get_collection(TASK_DB, PLUGIN_BUILDS_COLLECTION),
            pipelines=get_collection(TASK_DB, PIPELINES_COLLECTION),
            failures=get_collection(TASK_DB, FAILED_RUNS_COLLECTION),
        )

    def create_task(
        self,
        task: dict[str, Any] | None,
        collection: str,
        inbound: InboundRequest,
        principal: Principal,
        request_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[Any, dict[str, str]]:
        """Start task.

        The user of the request becomes the owner of the task (cf.
        :py:mod:`pro_tes.ga4gh.tes.ownership`). The task is checked, then
        the plugin pipeline of the request runs (cf.
        :py:mod:`pro_tes.gateway`): its `request` phase may change the task,
        and its `route` phase chooses the TES backends, in order, from those
        of `custom.tes.backends` that can run the task unchanged (cf.
        :py:func:`pro_tes.ga4gh.tes.backends.eligible_backends`). Tasks that
        fail validation, are rejected by a plugin, or have no backend are not
        stored.

        The task is then stored, and relayed to the backends in turn, after
        the `forward` phase of each attempt, until one accepts the task. It
        is relayed to the next one only if a backend certainly did not
        accept it, never if it may have (cf.
        :py:class:`pro_tes.ga4gh.tes.backends.TesClient`). Tasks that no
        backend accepts are not kept: they are deleted, with the owner's
        roles on them. Tasks that a backend may have accepted are kept (as
        `SYSTEM_ERROR`).

        The provenance of the pipeline run (cf.
        :py:class:`foca.plugins.ProvenanceStore`) is stored with the task
        (`provenance`): the pipeline, plugins and route when the task is
        stored, with the attempts once a backend accepted the task (or may
        have). Runs that fail after they started (e.g., tasks rejected by
        a plugin, or accepted by no backend) are recorded only if
        `plugins.provenance.record_failed` is on.

        Args:
            task: Task of the request (its JSON body); not changed.
            collection: Request path of the tasks (e.g.,
                `/ga4gh/tes/v1/tasks`), the resource of the access control
                policies of the task's owner.
            inbound: Inbound request (cf.
                :py:func:`foca.plugins.adapter.inbound_from_request`); its
                payload is replaced by the checked task.
            principal: Caller of the request (cf.
                :py:func:`foca.plugins.adapter.current_principal`).
            request_id: Identifier of the request, for logs.
            **kwargs: Additional keyword arguments passed along with request.

        Returns:
            Response to the client: by default, the task identifier; and its
            headers (none by default; plugins may change both in the
            `response` phase).

        Raises:
            foca.plugins.GatewayError: The pipeline run failed (e.g., a
                plugin rejected the task, a step failed, the tags choose an
                invalid pipeline or backends, the request passed proTES
                before), or no backend accepted the task (in time); in the
                latter case, the task is not kept.
            pro_tes.exceptions.BackendOutcomeUnknown: A TES instance may
                have accepted the task, but did not confirm it; the task is
                stored with the state `SYSTEM_ERROR`, and with the TES
                instance, without remote identifier.
            pro_tes.exceptions.BadRequest: The request has no JSON body,
                or the task is invalid.
            pro_tes.exceptions.InlineContentTooLarge: The inline contents of
                the inputs of the task exceed the limit, also after a plugin
                changed the task.
            pro_tes.exceptions.Unauthorized: The user of the request could
                not be identified.
            pro_tes.limits.TaskLimitReached: The user has as many tasks as
                a limit of `custom.controllers.post_task.limits` allows.
        """
        # create task document
        start_time = utc_now()
        db_document: DbDocument = DbDocument()
        if task is None:
            raise BadRequest("Request body is missing")
        owner = get_owner()
        payload_original: dict[str, Any] = deepcopy(task)
        # credentials in URLs are neither stored nor forwarded; removed before
        # the plugins, so that they don't get them either
        handle_url_credentials(
            payload=payload_original,
            mode=self.custom_config.controllers.post_task.url_credentials,
        )
        max_inline_content = self.custom_config.controllers.post_task.max_inline_content
        check_inline_content(payload=payload_original, limit=max_inline_content)
        db_document.task_original = TesTask(**payload_original)
        try:
            validate_task(payload_original)
        except (TypeError, ValueError) as exc:
            raise BadRequest(
                f"Task could not be validated. Original error message:"
                f" '{type(exc).__name__}: {exc}'"
            ) from exc

        # limits of the tasks per user (off by default), before the pipeline
        check_task_limits(
            collection=self.db_client,
            owner=owner,
            roles=principal.groups,
            limits=self.custom_config.controllers.post_task.limits,
        )

        # run the plugins of the request and route phases; the engine
        # sanitizes and validates the task after each plugin that changed it
        run = get_host().start(
            replace(inbound, payload=deepcopy(payload_original)),
            principal,
            request_id=request_id,
        )
        try:
            accepted, db_document, db_connector = self._run_pipeline(
                run=run,
                db_document=db_document,
                start_time=start_time,
                owner=owner,
                collection=collection,
            )
        except GatewayError as exc:
            # e.g., tasks rejected by plugins or accepted by no backend,
            # which are not kept; recorded only if
            # `plugins.provenance.record_failed` (never raises)
            self.provenance.save_failure(run, exc)
            raise
        # errors of the `response` phase don't discard the task: it runs at
        # the backend, so it is kept, tracked and cancelable, with its
        # provenance; it is no failed run
        return self._forwarded(
            run=run,
            accepted=accepted,
            db_document=db_document,
            db_connector=db_connector,
        )

    def _run_pipeline(
        self,
        run: PipelineRun,
        db_document: DbDocument,
        start_time: str,
        owner: str,
        collection: str,
    ) -> tuple[_Accepted, DbDocument, DbDocumentConnector]:
        """Run the pipeline of a new task, store the task, and relay it.

        Args:
            run: Pipeline run of the request, started.
            db_document: Database document of the task, with the task as
                received (`task_original`).
            start_time: Time of the request.
            owner: Owner of the task.
            collection: Request path of the tasks.

        Returns:
            The attempt of the backend that accepted the task (cf.
            :py:meth:`_forward`), and the stored task and its connector.

        Raises:
            foca.plugins.GatewayError: The pipeline run failed, or no backend
                accepted the task (in time); the task is not kept.
            pro_tes.exceptions.BackendOutcomeUnknown: A TES instance may
                have accepted the task (cf. :py:meth:`create_task`).
        """
        payload: dict[str, Any] = run.request().payload
        # plugins rank only the backends that can run the task, and get it
        # in the canonical version
        backends = get_backends()
        eligible = eligible_backends(payload, backends)
        candidates = run.route(eligible) if eligible or not backends else []
        # before the task is stored: no task is left
        # (a backend without an answer may be fine again soon: `503` below)
        if (
            not candidates
            and len(eligible) < len(backends)
            and all(backend_versions.confirmed(backend) for backend in backends)
        ):
            run.final_reason = "no_eligible_backend"
            found = ", ".join(map(str, incompatibilities(payload)))
            raise NoEligibleBackend(
                f"No selected TES instance can run this task unchanged: {found}",
                run=run,
            )
        if not candidates:
            run.final_reason = "no_backend"
            raise NoBackendAvailable("No TES backend available", run=run)

        # update task document; set by the server (TES), not by clients
        payload = deepcopy(payload)
        payload["creation_time"] = start_time
        # tags under the prefix other than the client's choices are dropped;
        # the gateway's (FOCA's) are added once the identifier is known
        payload["tags"] = without_server_tags(
            payload.get("tags"), run.host.conf.tags.prefix
        )
        db_document.task = TesTask(**payload)
        # the prospective part of the provenance (pipeline, plugins, route),
        # stored with the task; the attempts are added once a backend
        # accepted the task, or may have (cf. `_update_records`)
        db_document.provenance = self.provenance.save(run)

        # create database document
        db_document = self._update_task(
            db_document=db_document,
            start_time=start_time,
            owner=owner,
            collection=collection,
            tags=lambda task_id: _creation_tags(
                run, provenance_path=f"{collection.rstrip('/')}/{task_id}/provenance"
            ),
        )
        run.task_id = db_document.task.id
        db_connector = DbDocumentConnector(
            collection=self.db_client,
            worker_id=db_document.worker_id,
        )
        logger.info(
            "Created task record with task identifier '%s' and worker job"
            " identifier '%s'",
            db_document.task.id,
            db_document.worker_id,
        )

        # relay request
        logger.info(
            "Attempting to forward task '%s' to the TES instances, in the"
            " following order: %s",
            db_document.task.id,
            [backend.id for backend in candidates],
        )
        try:
            accepted = self._forward(
                run=run,
                candidates=candidates,
                prefix=run.host.conf.tags.prefix,
                db_document=db_document,
                db_connector=db_connector,
            )
        except GatewayError:
            # no backend accepted the task (in time), or a plugin of the
            # `forward` phase rejected it
            self._discard_task(
                db_document=db_document, owner=owner, collection=collection
            )
            raise
        return accepted, db_document, db_connector

    def _forward(
        self,
        run: PipelineRun,
        candidates: list[Backend],
        prefix: str,
        db_document: DbDocument,
        db_connector: DbDocumentConnector,
    ) -> _Accepted:
        """Relay a stored task to the backends in turn, until one accepts it.

        Args:
            run: Pipeline run of the request.
            candidates: Backends to try, in order (from the `route` phase).
            prefix: Prefix of the tags reserved for the gateway.
            db_document: Database document of the task.
            db_connector: Connector to the database document.

        Returns:
            The attempt of the backend that accepted the task, its client,
            response and identifier of the task.

        Raises:
            foca.plugins.GatewayError: No backend accepted the task (in
                time), or a plugin of the `forward` phase rejected it.
            pro_tes.exceptions.BackendOutcomeUnknown: A backend may have
                accepted the task; it is stored as `SYSTEM_ERROR`.
            RuntimeError: The attempts ended without outcome (a bug of the
                plugin engine).
        """
        for attempt in run.attempts(candidates):
            tes_url = attempt.backend.url
            db_document.tes_endpoint = TesEndpoint(host=tes_url)
            version = backend_version(attempt.backend)
            try:
                # checked by `check_outbound` before the attempt, but not
                # against the version, if a plugin of the `forward` phase
                # changed the task
                task = relayed_task(
                    attempt.outbound.payload, prefix=prefix, version=version
                )
                cli = client_for(attempt.backend)
                remote_task_id, response = cli.forward(
                    attempt.outbound,
                    task=task,
                    connect_timeout=attempt.remaining(),
                )
            except BackendOutcomeUnknown as exc:
                # the backend may run the task, so it is not sent elsewhere
                attempt.unknown(exc.reason)
                logger.error(
                    "Task '%s' may or may not have been accepted by TES"
                    " endpoint hosted at: %s; not sent to other TES"
                    " endpoints. Original error message: '%s: %s'",
                    db_document.task.id,
                    tes_url,
                    type(exc).__name__,
                    exc,
                )
                # the loop is left before the engine sets it
                run.final_reason = "outcome_unknown"
                try:
                    db_connector.upsert_fields_in_root_object(
                        root="tes_endpoint",
                        host=tes_url,
                        base_path="",
                        tes_version=str(version),
                    )
                    db_connector.update_task_state(state=TesState.SYSTEM_ERROR.value)
                finally:
                    self._update_records(run=run, db_document=db_document)
                raise
            except (requests.RequestException, BackendAuthError, ValueError) as exc:
                # the backend did not accept the task (cf. `TesClient`), has
                # no client (e.g., it is no longer configured,
                # `BackendNotConfigured`), or its entry is invalid
                attempt.failed(forward_reason(exc))
                logger.warning(
                    "Task '%s' could not be sent to TES endpoint hosted at:"
                    " %s. Original error message: '%s: %s'",
                    db_document.task.id,
                    tes_url,
                    type(exc).__name__,
                    exc,
                )
                continue
            return _Accepted(
                attempt=attempt,
                cli=cli,
                response=response,
                remote_task_id=remote_task_id,
                tes_version=str(version),
            )
        # the attempts end with a success, or raise
        raise RuntimeError("Forwarding ended without outcome")

    def _forwarded(
        self,
        run: PipelineRun,
        accepted: _Accepted,
        db_document: DbDocument,
        db_connector: DbDocumentConnector,
    ) -> tuple[Any, dict[str, str]]:
        """Store and track a task that a backend accepted.

        Args:
            run: Pipeline run of the request.
            accepted: The attempt of the backend that accepted the task, its
                client, response and identifier of the task.
            db_document: The stored task.
            db_connector: Connector of the stored task.

        Returns:
            Response to the client and its headers, after the `response`
            phase.

        Raises:
            foca.plugins.GatewayError: A plugin of the `response` phase
                rejected the response, or a step of it failed; the task is
                kept (and tracked).
        """
        attempt, cli, response, remote_task_id, tes_version = accepted
        tes_url = attempt.backend.url
        logger.info(
            "Task '%s' successfully forwarded to TES endpoint hosted at:"
            " %s. Remote task identifier: %s",
            db_document.task.id,
            tes_url,
            remote_task_id,
        )
        # the task is stored with its backend first, so that it is
        # cancelable even if the rest fails (e.g., the instance stops)
        db_document = self._update_doc_in_db(
            db_connector=db_connector,
            tes_url=tes_url,
            remote_task_id=remote_task_id,
            tes_version=tes_version,
        )
        response.client_payload = {"id": db_document.task.id}
        failed: Exception | None = None
        try:
            # reported once the backend is stored, so that the provenance
            # has the attempt whatever fails later
            response = attempt.succeeded(response, remote_id=remote_task_id)
        except Exception as exc:
            # raised once the task is tracked
            failed = exc
        # before the tracking starts, which writes the task back
        self._update_records(run=run, db_document=db_document)
        try:
            remote_task: Task = cli.get_task(remote_task_id)
            task_model_converter = TaskModelConverter(task=remote_task)
            task_converted: TesTask = task_model_converter.convert_task()
            state = TesState(task_converted.state)
            # unless a request to cancel the task changed it meanwhile
            if db_connector.update_task_state(
                state=state.value, if_state=TesState.UNKNOWN.value
            ):
                db_document.task.state = state
        except Exception as exc:
            # the task is tracked anyway, also if its backend serves it
            # invalid (e.g., with an unknown state)
            logger.error(
                "Task '%s' info could not be retrieved from TES endpoint"
                " hosted at: %s. Original error message: '%s: %s'",
                db_document.task.id,
                tes_url,
                type(exc).__name__,
                exc,
            )
        task__track_task_progress.apply_async(
            None,
            {
                "worker_id": db_document.worker_id,
                "remote_host": db_document.tes_endpoint.host,
                "remote_base_path": db_document.tes_endpoint.base_path,
                "remote_task_id": remote_task_id,
            },
        )
        if failed is not None:
            raise failed
        return response.client_payload, dict(response.client_headers)

    def _update_records(self, run: PipelineRun, db_document: DbDocument) -> None:
        """Store the provenance and the routing tags of a stored task's run.

        Replaces the provenance stored when the task was created (cf.
        :py:meth:`foca.plugins.ProvenanceStore.save`), now with the
        attempts, and adds FOCA's routing tags (e.g., `gateway.backend`, cf.
        :py:func:`foca.plugins.tags.routing_tags`) to the task's tags.
        Errors are logged, not raised: a backend has (or may have) the task
        already.

        Args:
            run: Pipeline run of the request that created the task.
            db_document: The stored task, with its tags.
        """
        fields: dict[str, Any] = {}
        try:
            fields["task.tags"] = {
                **(db_document.task.tags or {}),
                **routing_tags(run),
            }
        except Exception:
            # the attempts are stored nevertheless
            logger.exception("Routing tags of the task not built")
        try:
            fields["provenance"] = self.provenance.save(run)
            self.db_client.update_one(
                {"worker_id": db_document.worker_id}, {"$set": fields}
            )
        except Exception as exc:
            logger.error(
                "Provenance of task with worker ID '%s' not updated: %s",
                db_document.worker_id,
                type(exc).__name__,
            )

    def get_task_provenance(self, id: str) -> dict[str, Any]:
        """Get the provenance of a task, with its references expanded.

        Only owners of the task and admins may get it (enforced by the
        policies). The builds of the plugins that ran and default and
        preset pipelines are stored once, by hash (cf.
        :py:class:`foca.plugins.ProvenanceStore`); they are added to the
        record (`builds`, `pipeline.resolved`), unless it has them
        already (e.g., if they could not be stored). References that can't
        be resolved are left out.

        Args:
            id: Task identifier.

        Returns:
            The provenance of the task.

        Raises:
            pro_tes.exceptions.ProvenanceNotFound: The task has no
                provenance (e.g., it was created by an earlier version).
            pro_tes.exceptions.TaskNotFound: The task is not available.
        """
        document = self.db_client.find_one(
            filter={"task.id": id}, projection={"_id": False, "provenance": True}
        )
        if document is None:
            logger.error("Task '%s' not found.", id)
            raise TaskNotFound
        # records may lack parts (e.g., if building them failed, cf.
        # `ProvenanceStore.save`: `error: internal`), so they are checked
        record: dict[str, Any] | None = document.get("provenance")
        if not isinstance(record, dict):
            raise ProvenanceNotFound(f"No provenance recorded for task '{id}'")
        pipeline = record.get("pipeline")
        if isinstance(pipeline, dict) and "resolved" not in pipeline:
            stored = get_collection(TASK_DB, PIPELINES_COLLECTION).find_one(
                {"_id": pipeline.get("resolved_hash")}
            )
            if stored is not None:
                pipeline["resolved"] = stored["resolved"]
        builds: dict[str, Any] = dict(record.get("builds") or {})
        missing = [key for key in record.get("plugins") or {} if key not in builds]
        if missing:
            for build in get_collection(TASK_DB, PLUGIN_BUILDS_COLLECTION).find(
                {"_id": {"$in": missing}}
            ):
                builds[build.pop("_id")] = build
        record["builds"] = builds
        return record

    def list_tasks(self, collection: str, **kwargs: Any) -> dict[str, Any]:
        """Return list of the tasks of the user of the request.

        Args:
            collection: Request path of the tasks (e.g.,
                `/ga4gh/tes/v1/tasks`), the resource of the access control
                policies of their owners.
            **kwargs: Keyword arguments passed along with request.

        Returns:
            Response object according to TES API schema . Cf.
            https://github.com/ga4gh/task-execution-schemas/blob/117cd927aa119469ee80cedbd8940c968b81c5d6/openapi/task_execution_service.openapi.yaml;
            only the tasks that the user owns are listed (also for admins),
            newest first, and `next_page_token` is empty on the last page.

        Raises:
            pro_tes.exceptions.BadRequest: The page size is not positive, or
                the page token or the tag filters are invalid.
        """
        view = kwargs.get("view", DEFAULT_VIEW)
        page_size = self._page_size(page_size=kwargs.get("page_size"), view=view)
        filter_dict: dict[str, Any] = {**owned_tasks_filter(collection=collection)}

        # an empty token (of the last page) is taken as no token
        page_token = kwargs.get("page_token")
        if page_token:
            try:
                filter_dict["_id"] = {"$lt": ObjectId(page_token)}
            except (InvalidId, TypeError) as exc:
                raise BadRequest(f"Invalid page token: {page_token!r}") from exc
        projection = self._set_projection(view=view)

        # matched literally, as a prefix
        name_prefix = kwargs.get("name_prefix")
        if name_prefix is not None:
            # MongoDB rejects regular expressions with null characters
            if "\x00" in name_prefix:
                raise BadRequest("Invalid name prefix: null character")
            pattern = "^" + re.escape(name_prefix)
            filter_dict["task_original.name"] = {"$regex": pattern}

        # validated by the specification; what clients get back, as for tags
        state = kwargs.get("state")
        if state is not None:
            filter_dict["task.state"] = state
        filter_dict.update(
            tags_filter(
                keys=kwargs.get("tag_key") or [], values=kwargs.get("tag_value") or []
            )
        )

        # one more task than the page size tells whether there is a next
        # page; documents are processed one by one, as the cursor yields them
        cursor = (
            self.db_client.find(filter=filter_dict, projection=projection)
            .sort("_id", -1)
            .limit(page_size + 1)
        )
        tasks: list[dict[str, Any]] = []
        next_page_token = ""
        last_id: Any = None
        for document in cursor:
            if len(tasks) == page_size:
                next_page_token = str(last_id)
                break
            last_id = document["_id"]
            if view == "MINIMAL":
                tasks.append(
                    {"id": document["task"]["id"], "state": document["task"]["state"]}
                )
            else:
                tasks.append(self._string_metadata(document["task"]))
        cursor.close()
        logger.debug("Listed %s tasks", len(tasks))
        return {"next_page_token": next_page_token, "tasks": tasks}

    def get_task(self, id: str, **kwargs: Any) -> dict[str, Any]:
        """Return detailed information about a task.

        Only owners of the task may get it (enforced by the policies).

        Args:
            id: Task identifier.
            **kwargs: Additional keyword arguments passed along with request.

        Returns:
            Response object according to TES API schema . Cf.
                https://github.com/ga4gh/task-execution-schemas/blob/117cd927aa119469ee80cedbd8940c968b81c5d6/openapi/task_execution_service.openapi.yaml

        Raises:
            pro_tes.exceptions.TaskNotFound: The requested task is not
                available.
        """
        projection = self._set_projection(view=kwargs.get("view", DEFAULT_VIEW))
        document = self.db_client.find_one(
            filter={"task.id": id}, projection=projection
        )
        if document is None:
            logger.error("Task '%s' not found.", id)
            raise TaskNotFound
        return self._string_metadata(document["task"])

    @staticmethod
    def _string_metadata(task: dict[str, Any]) -> dict[str, Any]:
        """Drop the log metadata of a stored task that is not strings.

        The TES specification allows only strings, but tasks stored by
        earlier versions may have other values (e.g., their forwards).

        Args:
            task: Stored task, as served (changed in place).

        Returns:
            The task, with string log metadata only.
        """
        logs = task.get("logs")
        for log in logs if isinstance(logs, list) else []:
            metadata = log.get("metadata") if isinstance(log, dict) else None
            if isinstance(metadata, dict):
                log["metadata"] = {
                    key: value
                    for key, value in metadata.items()
                    if isinstance(value, str)
                }
        return task

    def cancel_task(self, id: str, **kwargs: Any) -> dict[str, Any]:
        """Cancel task.

        Only owners of the task may cancel it (enforced by the policies).
        Tasks that have not finished (also those in state `UNKNOWN`, e.g.,
        not tracked yet) are canceled at their TES instance, and are then in
        state `CANCELING`, until their tracking stores the final state that
        their TES instance reports; finished tasks and tasks being canceled
        are left as they are, also if their state changed while they were
        canceled (e.g., a final state stored by their tracking).

        Args:
            id: Task identifier.
            **kwargs: Additional keyword arguments passed along with request.

        Returns:
            Task identifier.

        Raises:
            pro_tes.exceptions.BackendAuthError: No credentials for the TES
                instance of the task are available.
            pro_tes.exceptions.BackendNotConfigured: The TES instance of the
                task is no longer configured.
            pro_tes.exceptions.BackendOutcomeUnknown: The TES instance may
                have canceled the task, but did not confirm it.
            pro_tes.exceptions.BackendRefused: The TES instance refused to
                cancel the task, which has not finished.
            pro_tes.exceptions.TaskNotCancelable: The task has not
                finished, but its identifier at its TES instance is unknown
                (e.g., it was not forwarded yet).
            pro_tes.exceptions.TaskNotFound: The requested task is not
                available.
        """
        document, db_document = self._find_task(id=id)
        task_id = self._remote_task_id(document=document, db_document=db_document)
        if db_document.task.state in States.TO_CANCEL and task_id is None:
            # forwarding may have stored it since
            document, db_document = self._find_task(id=id)
            task_id = self._remote_task_id(document=document, db_document=db_document)

        if db_document.task.state in States.TO_CANCEL:
            db_connector = DbDocumentConnector(
                collection=self.db_client,
                worker_id=db_document.worker_id,
            )
            url: str = (
                f"{db_document.tes_endpoint.host.rstrip('/')}/"
                f"{db_document.tes_endpoint.base_path.strip('/')}"
            )

            if task_id is None:
                # e.g., tasks that are being forwarded, or that a TES
                # instance may have accepted without confirming it
                raise TaskNotCancelable(
                    f"Task '{id}' can't be canceled: its identifier at its TES"
                    " instance is unknown"
                )

            logger.info(
                "Trying to cancel task with task identifier '%s' and worker job"
                " identifier '%s' running at TES endpoint hosted at: %s",
                task_id,
                db_document.worker_id,
                url,
            )
            cli = create_client(
                host=db_document.tes_endpoint.host,
                base_path=db_document.tes_endpoint.base_path,
            )
            if not _cancel_at_backend(
                cli=cli,
                task_id=task_id,
                tracked=isinstance(db_document.tes_endpoint.task_id, str),
            ):
                logger.info(
                    "Task '%s' with worker ID '%s' has finished at its TES instance.",
                    id,
                    db_document.worker_id,
                )
                return {}
            # unless tracking stored another state meanwhile: the backend
            # may accept cancels of finished tasks (as no-ops); tracking keeps
            # `CANCELING` until the task is final
            # with the time of the cancel, after which tracking gives up
            # (`polling.max_cancel_time`)
            if db_connector.update_task_state(
                state=TesState.CANCELING.value,
                if_state=States.TO_CANCEL,
                fields={CANCELED_AT_FIELD: time.time()},
            ):
                logger.info(
                    "Task '%s' with worker ID '%s' being canceled.",
                    id,
                    db_document.worker_id,
                )
            else:
                logger.info(
                    "Task '%s' with worker ID '%s' canceled at its TES"
                    " instance; its stored state is kept, as it changed"
                    " meanwhile.",
                    id,
                    db_document.worker_id,
                )
        return {}

    def _find_task(self, id: str) -> tuple[dict[str, Any], DbDocument]:
        """Get a stored task.

        Args:
            id: Task identifier.

        Returns:
            The task as stored, and as model.

        Raises:
            pro_tes.exceptions.TaskNotFound: The task is not available.
        """
        document = self.db_client.find_one(
            filter={"task.id": id},
            projection=TASK_PROJECTION,
        )
        if document is None:
            logger.error("Task '%s' not found.", id)
            raise TaskNotFound
        return document, DbDocument(**document)

    def _write_doc_to_db(
        self,
        document: DbDocument,
        owner: str,
        collection: str,
        tags: Callable[[str], dict[str, str]] | None = None,
    ) -> tuple[str, str]:
        """Create database entry for task, and make the owner its owner.

        Identifiers of stored tasks, and identifiers that roles are granted
        on (e.g., left by deleted tasks), are not used again, so that nobody
        gets access to the task of someone else. Concurrent requests may
        still draw the same unused identifier: the unique index on task
        identifiers (cf. `db` in `config.yaml`) lets only one of them store
        its task, and the others try again with another identifier. The
        owner is granted the role only once the task is stored, so that
        requests losing the race never get a role on the task of someone
        else; if the role can't be granted, the task is deleted, and roles
        granted partly are revoked.

        Args:
            document: Document to be written to database.
            owner: Owner of the task (cf.
                :py:func:`pro_tes.ga4gh.tes.ownership.get_owner`).
            collection: Request path of the tasks (e.g.,
                `/ga4gh/tes/v1/tasks`).
            tags: Function getting the tags of the gateway for a task
                identifier (e.g., with the path of the task's provenance),
                added to the task's tags, if any.

        Returns:
            Tuple of task id and worker id.

        Raises:
            pymongo.errors.DuplicateKeyError: No unused task identifier was
                found.
        """
        controller_config = self.custom_config.controllers.post_task
        charset = controller_config.task_id.charset
        length = controller_config.task_id.length

        # try inserting until unused task id found
        for _ in range(controller_config.db.insert_attempts):
            task_id = generate_id(
                charset=charset,
                length=length,
            )
            if self.db_client.count_documents(
                {"task.id": task_id}, limit=1
            ) or task_has_roles(collection=collection, task_id=task_id):
                continue
            document.task.id = task_id
            document.worker_id = uuid()
            if tags is not None:
                document.task.tags = {**(document.task.tags or {}), **tags(task_id)}
            try:
                self.db_client.insert_one(
                    document.model_dump(mode="json", exclude_none=True)
                )
            except DuplicateKeyError:
                # another request stored a task with the identifier first
                continue
            try:
                add_task_owner(collection=collection, task_id=task_id, owner=owner)
            except Exception:
                # deleted first: roles left on deleted tasks give no access
                self.db_client.delete_one({"worker_id": document.worker_id})
                remove_task_owner(collection=collection, task_id=task_id, owner=owner)
                raise
            return task_id, document.worker_id
        raise DuplicateKeyError("Could not find an unused task identifier")

    def _discard_task(
        self, db_document: DbDocument, owner: str, collection: str
    ) -> None:
        """Delete a stored task that was not forwarded, with its owner roles.

        The task is deleted first, as roles left on deleted tasks give no
        access (cf. :py:meth:`_write_doc_to_db`). Errors are logged, not
        raised, so that the error of the request is raised; tasks that
        could not be deleted are marked as `SYSTEM_ERROR`, if possible.

        Args:
            db_document: The stored task.
            owner: Owner of the task.
            collection: Request path of the tasks (e.g.,
                `/ga4gh/tes/v1/tasks`).
        """
        try:
            self.db_client.delete_one({"worker_id": db_document.worker_id})
            remove_task_owner(
                collection=collection, task_id=str(db_document.task.id), owner=owner
            )
        except Exception:
            logger.exception("Task '%s' could not be discarded", db_document.task.id)
            with suppress(Exception):
                self.db_client.update_one(
                    {"worker_id": db_document.worker_id},
                    {"$set": {"task.state": TesState.SYSTEM_ERROR.value}},
                )
            return
        logger.info("Task '%s' not kept: it was not forwarded", db_document.task.id)

    def _set_projection(self, view: str) -> dict[str, bool]:
        """Set database projection for selected view.

        Args:
            view: View path parameter.

        Returns:
            Database projection for selected view.

        Raises:
            pro_tes.exceptions.BadRequest: Invalid view parameter.
        """
        if view == "MINIMAL":
            projection = {
                "task.id": True,
                "task.state": True,
            }
        elif view == "BASIC":
            projection = {
                "task.inputs.content": False,
                "task.logs.system_logs": False,
                "task.logs.logs.stdout": False,
                "task.logs.logs.stderr": False,
                "task_original": False,
                "tes_endpoint": False,
                "provenance": False,
                # backend references in the logs of tasks stored before
                # 0.4.0 (newer tasks have them in their gateway tags)
                "task.logs.metadata.forwarded_to": False,
                "task.logs.metadata.remote_task_id": False,
                "task.logs.metadata.tes_uri": False,
            }
        elif view == "FULL":
            projection = {
                "task_original": False,
                "worker_id": False,
                "tes_endpoint": False,
                "provenance": False,
                # backend references in the logs of tasks stored before
                # 0.4.0 (newer tasks have them in their gateway tags)
                "task.logs.metadata.forwarded_to": False,
                "task.logs.metadata.remote_task_id": False,
                "task.logs.metadata.tes_uri": False,
            }
        else:
            raise BadRequest
        return projection

    def _update_task(
        self,
        db_document: DbDocument,
        start_time: str,
        owner: str,
        collection: str,
        tags: Callable[[str], dict[str, str]] | None = None,
    ) -> DbDocument:
        """Update the task object, store it, and make the owner its owner.

        Args:
            db_document: The document in the database to be updated.
            start_time: The starting time of the incoming TES request.
            owner: Owner of the task (cf.
                :py:func:`pro_tes.ga4gh.tes.ownership.get_owner`).
            collection: Request path of the tasks (e.g.,
                `/ga4gh/tes/v1/tasks`).
            tags: Function getting the tags of the gateway for a task
                identifier, if any (cf. :py:meth:`_write_doc_to_db`).

        Returns:
            DbDocument: The updated database document.
        """
        logs = self._set_logs(start_time=start_time)
        db_document.task.logs = [TesTaskLog(**log) for log in logs]
        db_document.task.state = TesState.UNKNOWN
        db_document.user_id = owner

        (task_id, worker_id) = self._write_doc_to_db(
            document=db_document,
            owner=owner,
            collection=collection,
            tags=tags,
        )
        db_document.task.id = task_id
        db_document.worker_id = worker_id
        return db_document

    @staticmethod
    def _set_logs(start_time: str) -> list[dict[str, Any]]:
        """Create the logs of a task (`TesTask.logs`).

        Args:
            start_time: The starting time of the incoming TES request.

        Returns:
            Task logs with start time set.
        """
        return [
            {
                "logs": [],
                "metadata": {},
                "start_time": start_time,
                "end_time": None,
                "outputs": [],
                "system_logs": [],
            }
        ]

    def _update_doc_in_db(
        self,
        db_connector: DbDocumentConnector,
        tes_url: str,
        remote_task_id: str,
        tes_version: str,
    ) -> DbDocument:
        """Store the TES instance of a task, and the end time of its logs.

        Args:
            db_connector: The database connector.
            tes_url: The TES URL where the task is forwarded.
            remote_task_id: Task identifier at the remote TES instance.
            tes_version: TES version that the task was relayed in.

        Returns:
            The updated database document.

        Raises:
            ValueError: The task has no logs.
        """
        time_now = utc_now()
        tes_endpoint_dict: dict[str, Any] = {
            "host": tes_url,
            "base_path": "",
            "task_id": remote_task_id,
            "tes_version": tes_version,
        }
        db_document = db_connector.upsert_fields_in_root_object(
            root="tes_endpoint",
            **tes_endpoint_dict,
        )
        if db_document.task.logs is None:
            raise ValueError(f"Task '{db_document.task.id}' has no logs")
        task_logs = db_document.task.logs
        # updating the end time in TesTask logs
        for logs in task_logs:
            logs.end_time = time_now

        # without the state, which a request to cancel the task may have
        # changed in the meantime
        task = db_document.model_dump(mode="json", exclude_none=True)["task"]
        task.pop("state", None)
        db_document = db_connector.upsert_fields_in_root_object(root="task", **task)
        logger.debug("Task '%s' inserted to database", db_document.task)
        return db_document

    def _page_size(self, page_size: int | None, view: str = DEFAULT_VIEW) -> int:
        """Get the size of a page of tasks.

        Args:
            page_size: Page size of the request, if any.
            view: View of the tasks.

        Returns:
            The page size of the request, or the default page size of the
            config if none is requested; at most the maximum page size of
            the config, of the view `FULL` for that view.

        Raises:
            pro_tes.exceptions.BadRequest: The page size is not positive.
        """
        config = self.custom_config.controllers.list_tasks
        maximum = config.max_page_size
        if view == "FULL":
            maximum = min(maximum, config.max_page_size_full)
        if page_size is None:
            return min(config.default_page_size, maximum)
        if page_size < 1:
            raise BadRequest(f"Page size must be positive: {page_size}")
        return min(page_size, maximum)

    @staticmethod
    def _remote_task_id(
        document: dict[str, Any], db_document: DbDocument
    ) -> str | None:
        """Get the identifier of a task at the TES backend it was relayed to.

        Tasks stored by earlier versions have it only in their logs: in the
        last of their forwards (`forwarded_to`, a list, or a nested object
        before 0.3.0), else as `remote_task_id`. They are read as stored, as
        the model drops the forwards. Keep this in sync with the query of
        stored forwards in `track_task_progress`.

        Args:
            document: Stored task, as stored.
            db_document: Stored task, as model.

        Returns:
            The identifier of the task at its TES backend, or `None` if the
            task was not forwarded.
        """
        if db_document.tes_endpoint.task_id is not None:
            return db_document.tes_endpoint.task_id
        stored_logs = document.get("task", {}).get("logs") or [{}]
        stored_metadata = stored_logs[0].get("metadata") or {}
        forwards: Any = stored_metadata.get("forwarded_to")
        if forwards is None:
            remote_task_id = stored_metadata.get("remote_task_id")
            return remote_task_id if isinstance(remote_task_id, str) else None
        # nested forwards have the next one in `forwarded_to`; malformed
        # forwards (at any level) are no remote identifier, rather than a
        # stale or upstream one
        while isinstance(forwards, dict) and forwards.get("forwarded_to") is not None:
            forwards = forwards["forwarded_to"]
        if isinstance(forwards, dict):
            forwards = [forwards]
        if not isinstance(forwards, list) or not forwards:
            return None
        forward = forwards[-1]
        if isinstance(forward, dict) and isinstance(forward.get("id"), str):
            return str(forward["id"])
        return None
