"""Configuration of the accounting of the resources that tasks used."""

from typing import Annotated

from foca.utils.urls import normalize_url
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from pro_tes.accounting.tool import TAG_KEYS, IdentifierTag

# IRIs (RFC 3987): a scheme, then characters that IRIs may have (no spaces,
# controls or delimiters such as `<` and `"`); not fully checked
IRI_PATTERN = r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`\x00-\x1f\x7f]+$"
IRI = Annotated[str, StringConstraints(pattern=IRI_PATTERN, max_length=2048)]
# Non-empty texts
Text = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
# Namespaces of images: repositories or their prefixes, with an optional
# registry (host and port) first, without tag, digest or trailing slash
ImageNamespace = Annotated[
    str,
    StringConstraints(
        pattern=r"^([A-Za-z0-9.-]+(:[0-9]+)?/)?[a-z0-9._-]+(/[a-z0-9._-]+)*$",
        max_length=255,
    ),
]
# Names of Docker Hub as registry of image references
DOCKER_HUB = "docker.io"
DOCKER_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})


def _is_registry(component: str) -> bool:
    """Check whether the first component of an image name is a registry.

    As Docker reads image references: hosts with a dot or a port, and
    `localhost`.

    Args:
        component: First path component of the image name.

    Returns:
        Whether it names a registry.
    """
    return "." in component or ":" in component or component == "localhost"


def canonical_image(name: str, official: bool = True) -> str:
    """Get the canonical form of an image repository or namespace.

    With its registry, lowercase (Docker Hub as `docker.io`, also for its
    aliases); Docker Hub's official images (one component, e.g., `ubuntu`)
    in `library`. A path after Docker Hub is always one of Docker Hub, also
    if its first component looks like a host (`docker.io/quay.io/x`).

    Args:
        name: Image repository (without tag and digest) or namespace.
        official: Whether names of one component are official images of
            Docker Hub (`library/<name>`); false for namespaces.

    Returns:
        The canonical name, e.g., `docker.io/library/ubuntu` for `ubuntu`.
    """
    first, _, rest = name.partition("/")
    if rest and _is_registry(first) and first.lower() not in DOCKER_HUB_ALIASES:
        return f"{first.lower()}/{rest}"
    if rest and first.lower() in DOCKER_HUB_ALIASES:
        name = rest
    if official and "/" not in name:
        name = f"library/{name}"
    return f"{DOCKER_HUB}/{name}"


# Countries (ISO 3166-1 alpha-2)
Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class ReportModel(BaseModel):
    """Base of the models of execution reports: unknown keys are errors.

    Keys are those of the Tool Execution Report format (cf.
    :py:mod:`pro_tes.accounting.reports`), also its camelCase ones (aliases).
    """

    model_config = ConfigDict(extra="forbid", validate_by_name=True)


class Address(ReportModel):
    """Address of the physical location of computations (schema.org).

    Args:
        address_country: Country, as ISO 3166-1 alpha-2 code
            (`addressCountry`).
        postal_code: Postal code (`postalCode`).
        address_region: Region in the country (`addressRegion`).
        address_locality: Locality in the region (`addressLocality`).

    Attributes:
        address_country: Country, as ISO 3166-1 alpha-2 code.
        postal_code: Postal code.
        address_region: Region in the country.
        address_locality: Locality in the region.
    """

    address_country: Country | None = Field(default=None, alias="addressCountry")
    postal_code: Text | None = Field(default=None, alias="postalCode")
    address_region: Text | None = Field(default=None, alias="addressRegion")
    address_locality: Text | None = Field(default=None, alias="addressLocality")


class Location(ReportModel):
    """Physical location of computations (`location` of execution reports).

    Args:
        name: Name of the location (e.g., the data centre).
        address: Address of the location.

    Attributes:
        name: Name of the location.
        address: Address of the location.
    """

    name: Text | None = None
    address: Address | None = None


class Provider(ReportModel):
    """Infrastructure provider (an item of `infra` of execution reports).

    Args:
        infra_name: Name of the infrastructure (who gets credit).
        infra_identifier: Identifiers of the infrastructure (IRIs, e.g., of
            ROR or an RRID); at least one.
        infra_version: Version of software-based infrastructure.

    Attributes:
        infra_name: Name of the infrastructure.
        infra_identifier: Identifiers of the infrastructure.
        infra_version: Version of software-based infrastructure, if any.
    """

    infra_name: Text
    infra_identifier: list[IRI] = Field(min_length=1)
    infra_version: Text | None = None


class ReportedBackend(Provider):
    """A TES instance, as named in execution reports.

    Args:
        location: Physical location of the TES instance; by default, that
            of `ExecutionReports.location`.
        **data: Fields of :py:class:`Provider`.

    Attributes:
        location: Physical location of the TES instance, if set.
    """

    location: Location | None = None


class ExecutionReports(ReportModel):
    """Execution reports of the usage records (`custom.accounting.reports`).

    Args:
        max_records: Maximum number of records (tasks) that one request for
            reports may cover; requests for more are rejected, so that their
            time range must be narrowed.
        infra: Providers named in each report, before the TES instance and
            proTES (e.g., the operator of the deployment).
        location: Physical location of the computations, unless set for
            their TES instance.
        backends: TES instances, by URL (normalized, as in
            `custom.tes.backends`); others are named by their URL.
        images: Trusted namespaces of images, whose tasks are reported:
            repositories or their prefixes (whole path components), with
            their registry, or without for Docker Hub, whose official
            images are `library`; not a registry alone. By default, Docker
            Hub's official images and BioContainers (on Docker Hub and
            Quay).
        tags: Tags of tasks whose values are reported as identifiers of the
            tool (`tool_identifier`), if valid; values of tags that are not
            listed are not reported. By default, `biotools_id` and `rrid`
            (the only ones known).

    Attributes:
        max_records: Maximum number of records of a request for reports.
        infra: Providers named in each report.
        location: Physical location of the computations, if set.
        backends: TES instances, by normalized URL.
        images: Trusted namespaces of images, in canonical form (cf.
            :py:func:`canonical_image`, e.g., `docker.io/library`).
        tags: Tags of tasks whose values are reported as identifiers.
    """

    max_records: Annotated[int, Field(gt=0, le=1000000)] = 10000
    infra: list[Provider] = []
    location: Location | None = None
    backends: dict[str, ReportedBackend] = {}
    images: list[ImageNamespace] = Field(
        default=["library", "biocontainers", "quay.io/biocontainers"],
        validate_default=True,
    )
    tags: list[IdentifierTag] = list(TAG_KEYS)

    @field_validator("images")
    @classmethod
    def canonical_images(cls, value: list[str]) -> list[str]:
        """Get the canonical form of the trusted namespaces of images.

        Args:
            value: Namespaces, as given.

        Returns:
            The namespaces, with their registry (cf.
            :py:func:`canonical_image`).

        Raises:
            ValueError: A namespace is a registry only (e.g., `ghcr.io`),
                which would trust the namespaces of all its users.
        """
        for image in value:
            if "/" not in image and (
                _is_registry(image) or image.lower() in DOCKER_HUB_ALIASES
            ):
                raise ValueError(
                    f"Image namespace {image!r} is a registry: name a namespace in it"
                )
        return [canonical_image(image, official=False) for image in value]

    @field_validator("backends")
    @classmethod
    def normalize_backends(
        cls, value: dict[str, ReportedBackend]
    ) -> dict[str, ReportedBackend]:
        """Normalize the URLs of the TES instances.

        Args:
            value: TES instances, by URL as given.

        Returns:
            The TES instances, by normalized URL.

        Raises:
            ValueError: A URL is invalid, or two URLs are the same once
                normalized.
        """
        backends: dict[str, ReportedBackend] = {}
        for url, backend in value.items():
            normalized = normalize_url(url, name="URLs of `reports.backends`")
            if normalized in backends:
                raise ValueError(f"TES instance {normalized!r} is listed twice")
            backends[normalized] = backend
        return backends


class Accounting(BaseModel):
    """Accounting of the resources that tasks used (`custom.accounting`).

    Unknown keys are errors, as in the rest of the custom configuration.

    Args:
        enabled: Whether the Celery workers record the resources of each
            task that finished at its TES instance; stored records are
            served either way.
        reports: What the execution reports of the records name (cf.
            :py:mod:`pro_tes.accounting.reports`).

    Attributes:
        enabled: Whether the resources of finished tasks are recorded.
        reports: What the execution reports of the records name.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    reports: ExecutionReports = ExecutionReports()
