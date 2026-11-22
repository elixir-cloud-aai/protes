"""Tests for the GitHub Actions workflows (names, gate, release checks, installs)."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from typing import Any

from packaging.requirements import Requirement
from packaging.version import Version
import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GATE = "passed"
# Jobs that don't block pull requests, until they have proven reliable: they
# depend on the registries and packages of third parties
NOT_IN_GATE = {"integration-tesk"}
# Funnel's URL in the integration configs, and the TES versions that backends
# report in their service info, where not TES 1.0 (TESP-API and TESK; see the
# notes on TES implementations)
FUNNEL = "http://funnel:8000"
REPORTED = {FUNNEL: "1.1"}
# The last release signed by the release workflow before its file was renamed
# from `release.yaml` to `release.yml`
LAST_RELEASE_YAML = Version("0.3.0")
# The last release signed before the repository was renamed from `proTES` to
# `protes` (the identities of signatures keep the case); a separate fact from
# `LAST_RELEASE_YAML`: raise it if a release is published before the rename
LAST_RELEASE_PROTES = Version("0.3.0")
# A chart file name as the release workflow packages it
CHART = "protes-1.2.3.tgz"
# The digest of a release run's image, and another one
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _events(workflow: dict[str, Any]) -> dict[str, Any]:
    """Get the events that trigger a workflow.

    Args:
        workflow: The workflow.

    Returns:
        The value of its key `on` (which PyYAML parses as `True`).
    """
    events = {str(key): value for key, value in workflow.items()}.get("True")
    result: dict[str, Any] = events or workflow["on"]
    return result


def _load(path: Path) -> dict[str, Any]:
    """Load a workflow file.

    Args:
        path: The path of the workflow file.

    Returns:
        The workflow.
    """
    workflow: dict[str, Any] = yaml.safe_load(path.read_text())
    return workflow


def test_workflow_files() -> None:
    """The workflows are `ci.yml`, `release.yml` and `scan.yml`."""
    names = {path.name: _load(path)["name"] for path in WORKFLOWS.iterdir()}
    assert names == {"ci.yml": "CI", "release.yml": "Release", "scan.yml": "Scan"}


def test_job_names_unique() -> None:
    """Job names are unique across all workflows (they name the checks)."""
    names = [
        job["name"]
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job in _load(path)["jobs"].values()
    ]
    assert len(names) == len(set(names))


def test_gate_needs_all_jobs() -> None:
    """The gate of `ci.yml` (the required check) needs every other job.

    The exceptions, `NOT_IN_GATE`, are listed on purpose.
    """
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    gate = jobs[GATE]
    assert gate["name"] == "CI passed"
    assert gate["if"] == "always()"
    assert sorted(gate["needs"]) == sorted(set(jobs) - {GATE} - NOT_IN_GATE)


def test_gated_jobs_run_on_edits() -> None:
    """The jobs that the gate needs don't skip edits of pull requests.

    A run for an edit reports the gate for the head commit, so a job that it
    skipped would pass unchecked, e.g. after it failed in the run for the
    commit.
    """
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    for job_id in jobs[GATE]["needs"]:
        assert "edited" not in jobs[job_id].get("if", ""), job_id


def test_ci_concurrency() -> None:
    """Only a new head commit of a pull request cancels its CI runs.

    A run cancelled by a newer run on the same commit (e.g. for an edit of
    the description) leaves a failed gate as the newest result on the
    commit, which blocks the merge. So only the first attempt of a run for
    `synchronize` cancels runs in progress, and runs for pushes get a group
    of their own (see "Cancelled runs" in `actions/gate/README.md` of
    `elixir-cloud-aai/.github`).
    """
    concurrency = _load(WORKFLOWS / "ci.yml")["concurrency"]
    cancel = " ".join(concurrency["cancel-in-progress"].split())
    assert cancel == (
        "${{ github.event_name == 'pull_request'"
        " && github.event.action == 'synchronize'"
        " && github.run_attempt == 1 }}"
    )
    # The whole expression, so that no other branch can give pushes a shared
    # group (pushes first, by commit; edits that keep the title; the ref)
    group = " ".join(concurrency["group"].split())
    assert group == (
        "${{ github.workflow }}-${{ github.event_name == 'push' && github.sha"
        " || github.event.action == 'edited' && !github.event.changes.title"
        " && format('{0}-edited', github.ref) || github.ref }}"
    )


def test_tesk_job() -> None:
    """The TESK job serves what the config of proTES and the check expect.

    The job starts the cluster before TESK and proTES, and the node port of TESK
    is the port that proTES relays to and that the job checks.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"]["integration-tesk"]
    tesk = ROOT / "tests" / "integration" / "tesk"
    assert "tests/integration/tesk/compose.yaml" in job["env"]["COMPOSE_FILE"]
    runs = [step["run"] for step in job["steps"] if "run" in step]
    install = next(i for i, run in enumerate(runs) if "tesk/install.sh" in run)
    assert any("k3s" in run for run in runs[:install])
    assert any("protes-worker" in run for run in runs[install + 1 :])
    values = yaml.safe_load((tesk / "values.yaml").read_text())
    backend = f"http://k3s:{values['service']['node_port']}"
    config = yaml.safe_load((tesk / "config.yaml").read_text())
    assert [item["url"] for item in config["custom"]["tes"]["backends"]] == [backend]
    assert any(backend in run for run in runs if "check.sh" in run)


def _integration_configs() -> list[Path]:
    """Return the config overlays of the entries of the job `integration`.

    Returns:
        The paths of the files in `tests/integration/`, by entry of the matrix.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"]["integration"]
    assert job["env"]["INTEGRATION_CONFIG"] == "${{ matrix.config }}"
    return [
        ROOT / "tests" / "integration" / entry["config"]
        for entry in job["strategy"]["matrix"]["include"]
    ]


def test_integration_matrix() -> None:
    """The job `integration` gets Funnel's TES version from a label and not.

    Its entries `label` (`config.yaml`) and `service info`
    (`config-service-info.yaml`) differ in the config overlay of proTES only,
    which `tests/integration/compose.yaml` takes from `INTEGRATION_CONFIG`
    (by default `config.yaml`), and the second file is the first without the
    label `tes.version` of Funnel, so that proTES reads its version from
    Funnel's service info.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"]["integration"]
    assert job["name"] == "Integration (${{ matrix.version-from }})"
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {"version-from": "label", "config": "config.yaml"},
        {"version-from": "service info", "config": "config-service-info.yaml"},
    ]
    compose = yaml.safe_load(
        (ROOT / "tests" / "integration" / "compose.yaml").read_text()
    )
    for service in ("protes", "protes-worker"):
        overlay = compose["services"][service]["environment"]["FOCA_CONFIG_OVERLAY"]
        assert overlay == "/integration/${INTEGRATION_CONFIG:-config.yaml}"
    labeled_path, unlabeled_path = _integration_configs()
    labeled = yaml.safe_load(labeled_path.read_text())
    funnel = next(
        backend
        for backend in labeled["custom"]["tes"]["backends"]
        if backend["url"] == FUNNEL
    )
    assert funnel.pop("labels") == {"tes.version": "1.1"}
    assert yaml.safe_load(unlabeled_path.read_text()) == labeled


@pytest.mark.parametrize(
    ("job_id", "config_paths"),
    [
        ("integration", _integration_configs()),
        (
            "integration-tesk",
            [ROOT / "tests" / "integration" / "tesk" / "config.yaml"],
        ),
    ],
)
def test_integration_versions(job_id: str, config_paths: list[Path]) -> None:
    """Each integration job checks the TES version path of each of its backends.

    `tests/integration/versions.sh` runs once per backend of the job's config,
    with the version that proTES must find: that of the label `tes.version`,
    else the one that the backend reports (`REPORTED`, else TES 1.0). The
    steps are the same in every entry of the job's matrix, so each entry's
    config must give the same versions. No step overrides the final state of
    the task that ignores an executor's error, as it ends in `COMPLETE` on TES
    1.1.

    Args:
        job_id: The ID of the job in `ci.yml`.
        config_paths: The config overlays of proTES in the job, by entry.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"][job_id]
    expected = []
    for path in config_paths:
        backends = yaml.safe_load(path.read_text())["custom"]["tes"]["backends"]
        expected.append(
            {
                backend["url"]: backend.get("labels", {}).get(
                    "tes.version", REPORTED.get(backend["url"], "1.0")
                )
                for backend in backends
            }
        )
    checked: dict[str, str] = {}
    for step in job["steps"]:
        args = shlex.split(step.get("run", ""))
        if args[:1] == ["tests/integration/versions.sh"]:
            # Arguments: API, backend URL, backend API path, TES version
            checked[args[2]] = args[4]
            # Runs unless the job is cancelled, and its failure fails the job
            assert step.get("if", "${{ !cancelled() }}") == "${{ !cancelled() }}"
            assert "continue-on-error" not in step
            assert "IGNORE_ERROR_STATE" not in step.get("env", {})
    for versions in expected:
        assert checked == versions, f"versions.sh steps of {job_id}: {checked}"


def test_integration_funnel_version() -> None:
    """The docs name the version of Funnel that the integration job runs.

    `tests/integration/compose.yaml` builds Funnel's image from the official
    one, pinned by tag and digest, as the final stage. The testing docs
    (`docs/development.md`) name that version, and the notes on TES
    implementations (`docs/configuration.md`) say that CI runs it and have notes on it.
    """
    compose = yaml.safe_load(
        (ROOT / "tests" / "integration" / "compose.yaml").read_text()
    )
    build = compose["services"]["funnel"]["build"]["dockerfile_inline"]
    stages = re.findall(r"^\s*FROM\s+(.+?)\s*$", build, flags=re.MULTILINE)
    assert stages, build
    pin = re.fullmatch(
        r"quay\.io/ohsu-comp-bio/funnel:(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}",
        stages[-1],
    )
    assert pin, build
    version = pin[1]
    development = " ".join((ROOT / "docs" / "development.md").read_text().split())
    assert re.search(rf"\bFunnel {re.escape(version)}(?!\.?\d)", development)
    configuration = " ".join((ROOT / "docs" / "configuration.md").read_text().split())
    for pattern in (
        rf"CI runs tasks on real instances of Funnel {re.escape(version)}(?!\.?\d)",
        rf"\[Funnel\]\(https://github\.com/calypr/funnel\) {re.escape(version)}:",
    ):
        assert re.search(pattern, configuration), pattern


def test_verification_signer_workflow() -> None:
    """The verification commands name the workflow that signed the release.

    They are for the version in `CITATION.cff` (`{{ version }}`): releases up
    to 0.3.0 were signed by `release.yaml` of the repository `proTES`, later
    ones by `release.yml` of the repository `protes`. Fails on purpose once
    `CITATION.cff` has a later version, until the commands are updated.
    """
    match = re.search(
        r"^version:\s*[\"']?([^\"'\s]+)",
        (ROOT / "CITATION.cff").read_text(),
        flags=re.MULTILINE,
    )
    assert match
    version = Version(match[1])
    workflow = "release.yaml" if version <= LAST_RELEASE_YAML else "release.yml"
    repository = "proTES" if version <= LAST_RELEASE_PROTES else "protes"
    text = (ROOT / "docs" / "operations.md").read_text()
    signer = f"elixir-cloud-aai/{repository}/.github/workflows/{workflow}"
    # Every value of each option, so that none is skipped (the commands for
    # images of `dev` use regular expressions instead)
    values = {
        option: re.findall(rf"--{option} (\S+)", text)
        for option in ("repo", "signer-workflow", "certificate-identity")
    }
    assert values["repo"]
    assert set(values["repo"]) == {f"elixir-cloud-aai/{repository}"}
    assert values["signer-workflow"]
    assert set(values["signer-workflow"]) == {signer}
    assert values["certificate-identity"]
    assert set(values["certificate-identity"]) == {
        f"https://github.com/{signer}@refs/tags/v{{{{"
    }


def test_publish_condition() -> None:
    """`push` and `publish` run exactly where `PUBLISH` of `ci.yml` is "true".

    The gate allows them to be skipped only where `PUBLISH` is "false", and
    `scan` builds the image locally there, so the three must agree.
    """
    workflow = _load(WORKFLOWS / "ci.yml")
    publish = workflow["env"]["PUBLISH"]
    assert publish.startswith("${{ github.event_name == 'push' && ")
    jobs = workflow["jobs"]
    assert jobs["push"]["if"] == publish
    assert jobs["publish"]["if"] == publish


def test_upstream_owner() -> None:
    """`ci.yml` marks upstream runs by the owner `elixir-cloud-aai` only.

    A typo in, or an inverted, owner check would quietly change upstream runs
    (e.g., release readiness findings to warnings), so every check compares
    with `==`.
    """
    text = (WORKFLOWS / "ci.yml").read_text()
    checks = re.findall(r"github\.repository_owner\s*(\S+)\s*'([^']*)'", text)
    assert checks
    assert len(checks) == text.count("github.repository_owner")
    assert set(checks) == {("==", "elixir-cloud-aai")}


def test_gate_uses_shared_action() -> None:
    """The gate of `ci.yml` uses the shared `gate` action.

    It passes the results of all needed jobs, allows `title` to be skipped
    and `push` and `publish` only where `PUBLISH` is "false", and needs no
    permissions.
    """
    gate = _load(WORKFLOWS / "ci.yml")["jobs"][GATE]
    assert gate["permissions"] == {}
    (step,) = gate["steps"]
    assert step["uses"].startswith("elixir-cloud-aai/.github/actions/gate@")
    assert step["with"]["needs"] == "${{ toJSON(needs) }}"
    assert step["with"]["allow-skipped"] == (
        "${{ env.PUBLISH == 'false' && 'title push publish' || 'title' }}"
    )


def test_title_job() -> None:
    """The `title` job checks pull request titles with the shared action.

    It runs only on pull requests, needs no permissions, and `ci.yml` also
    runs when a pull request is edited.
    """
    workflow = _load(WORKFLOWS / "ci.yml")
    events = _events(workflow)
    assert "edited" in events["pull_request"]["types"]
    job = workflow["jobs"]["title"]
    assert job["name"] == "PR title"
    assert job["permissions"] == {}
    assert "github.event_name == 'pull_request'" in job["if"]
    (step,) = job["steps"]
    assert step["uses"].startswith("elixir-cloud-aai/.github/actions/pr-title@")


def test_shared_actions_pinned() -> None:
    """The shared actions of `elixir-cloud-aai/.github` are pinned to a commit.

    All pins name the same full commit SHA (bumped together), and no
    workflow uses a local copy of an action. The YAML drops
    comments, so the test below checks the release comments (`# vX.Y.Z`) in
    the files' text.
    """
    pinned = re.compile(r"elixir-cloud-aai/\.github/actions/[a-z-]+@([0-9a-f]{40})")
    shas: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job in _load(path)["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                assert not uses.startswith("./"), (path.name, uses)
                if uses.startswith("elixir-cloud-aai/.github/"):
                    match = pinned.fullmatch(uses)
                    assert match, (path.name, uses)
                    shas.add(match.group(1))
    assert len(shas) == 1


def test_shared_actions_release_comments() -> None:
    """Every pin of a shared action names its release in a comment.

    Dependabot bumps a pin `@<sha> # vX.Y.Z` to new releases only if the
    pinned commit carries a version tag, and updates the comment; all pins
    name the same release. Whether the commit is the release's commit isn't
    checked here (no network in the tests).
    """
    pin = re.compile(r"uses: elixir-cloud-aai/\.github/actions/[a-z-]+@\S+(.*)$")
    comment = re.compile(r" # (v\d+\.\d+\.\d+)")
    versions: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := pin.search(line):
                version = comment.fullmatch(match.group(1))
                assert version, (path.name, line.strip())
                versions.add(version.group(1))
    assert len(versions) == 1, versions


def test_release_chart_checked() -> None:
    """The release workflow checks the chart before attesting and signing it.

    `publish` downloads the chart artifact, which any job of the run could
    replace, so it checks that `dist/` holds exactly the file that `prepare`
    packaged (its name and SHA-256, as outputs of `prepare`) before anything,
    the image included, is attested or signed. Only that file is attested and
    signed. The wiring and order of the steps are checked here, the script
    itself by `test_release_chart_check_runs`.
    """
    release = _load(WORKFLOWS / "release.yml")["jobs"]
    prepare = release["prepare"]
    assert prepare["outputs"]["chart_file"] == "${{ steps.chart.outputs.file }}"
    assert prepare["outputs"]["chart_sha256"] == "${{ steps.chart.outputs.sha256 }}"
    packaging = {step["name"]: step for step in prepare["steps"]}
    assert packaging["Package chart"]["id"] == "chart"
    # Re-runs of all jobs replace the chart, so its checksum is the new one
    assert packaging["Upload chart"]["with"]["overwrite"] is True
    steps = {step["name"]: step for step in release["publish"]["steps"]}
    assert steps["Download chart"]["with"] == {"name": "chart", "path": "dist/"}
    check = steps["Check chart"]
    # A failed check stops the job
    assert "continue-on-error" not in check
    assert "if" not in check
    assert check["env"]["FILE"] == "${{ needs.prepare.outputs.chart_file }}"
    assert check["env"]["SHA256"] == "${{ needs.prepare.outputs.chart_sha256 }}"
    chart = "dist/${{ needs.prepare.outputs.chart_file }}"
    assert steps["Attest chart provenance"]["with"]["subject-path"] == chart
    assert steps["Sign chart"]["env"]["CHART"] == chart
    sign = steps["Sign chart"]["run"]
    assert sign == 'cosign sign-blob --yes --bundle "${CHART}.sigstore.json" "${CHART}"'
    names = list(steps)
    assert names.index("Download chart") < names.index("Check chart")
    # A rejected chart leaves the image (pushed untagged by `push`) without
    # GitHub attestation, signature and tags too
    assert names.index("Check chart") < names.index("Attest build provenance")
    assert names.index("Check chart") < names.index("Sign image")
    assert names.index("Check chart") < names.index("Attest chart provenance")
    assert names.index("Check chart") < names.index("Sign chart")
    assert names.index("Sign chart") < names.index("Create release")


def test_release_tag_checked_first() -> None:
    """The release tag is checked before any script of the tagged commit runs.

    The step that checks the tag (annotated, signature verified by GitHub,
    commit on `main`) holds a token and runs no code of the tagged commit;
    the scripts run in later steps, the version's without the token.
    """
    prepare = _load(WORKFLOWS / "release.yml")["jobs"]["prepare"]
    steps = {step["name"]: step for step in prepare["steps"]}
    # From the list, so that a second step of the same name is seen
    names = [step["name"] for step in prepare["steps"]]
    assert len(names) == len(steps)
    # Only the checkout and Python come first; Python with a fixed version
    # only (no `python-version-file` or cache, which read files of the commit)
    assert names[:3] == [
        "Check out repository",
        "Set up Python",
        "Check release tag and commit",
    ]
    assert set(steps["Set up Python"]["with"]) == {"python-version"}
    check = steps["Check release tag and commit"]
    assert check["id"] == "commit"
    assert "GH_TOKEN" in check["env"]
    assert ".verification.verified" in check["run"]
    # Neither scripts nor Python nor other files of the commit
    for code in ("scripts/", "python", "./"):
        assert code not in check["run"]
    assert prepare["outputs"]["sha"] == "${{ steps.commit.outputs.sha }}"
    derive = steps["Derive release version"]
    assert derive["id"] == "version"
    assert "scripts/release_version.py outputs" in derive["run"]
    assert "GH_TOKEN" not in derive["env"]
    assert "GH_TOKEN" not in prepare.get("env", {})


@pytest.mark.parametrize(
    ("extra", "rename", "tamper", "empty", "file", "sha256", "passes"),
    [
        pytest.param(None, None, False, False, CHART, None, True, id="good"),
        pytest.param(None, None, True, False, CHART, None, False, id="wrong-hash"),
        # Sorts before the chart
        pytest.param("other.tgz", None, False, False, CHART, None, False, id="extra"),
        # Sorts after the chart, so only the count rejects it
        pytest.param("zzz", None, False, False, CHART, None, False, id="extra-after"),
        pytest.param(".hidden", None, False, False, CHART, None, False, id="hidden"),
        pytest.param(
            None, "protes-9.9.9.tgz", False, False, CHART, None, False, id="renamed"
        ),
        pytest.param(None, None, False, True, CHART, None, False, id="empty"),
        pytest.param(None, None, False, False, "../x.tgz", None, False, id="bad-name"),
        pytest.param(
            None,
            "protes-1.tar",
            False,
            False,
            "protes-1.tar",
            None,
            False,
            id="not-tgz",
        ),
        pytest.param(None, None, False, False, CHART, "abc", False, id="bad-hash"),
        pytest.param(None, None, False, False, CHART, "", False, id="no-hash"),
        pytest.param(None, None, False, False, CHART, "A" * 64, False, id="upper-hash"),
    ],
)
def test_release_chart_check_runs(
    tmp_path: Path,
    extra: str | None,
    rename: str | None,
    tamper: bool,
    empty: bool,
    file: str,
    sha256: str | None,
    passes: bool,
) -> None:
    """The chart check of `publish` passes only for the chart of `prepare`.

    The step's script runs with the options of GitHub's default shell for
    steps without `shell:` (`bash -e`, no `pipefail`) in a directory whose
    `dist/` holds the downloaded artifact. It needs `bash`
    and GNU `sha256sum`, as on the GitHub runners.

    Args:
        tmp_path: The working directory of the step.
        extra: A file added to the artifact, if any.
        rename: The name the chart is renamed to in the artifact, if any.
        tamper: Whether the chart's content is changed after packaging.
        empty: Whether the artifact holds no file at all.
        file: The chart file name output by `prepare`.
        sha256: The checksum output by `prepare` (that of the chart if None).
        passes: Whether the check passes.
    """
    bash = shutil.which("bash")
    assert bash
    steps = _load(WORKFLOWS / "release.yml")["jobs"]["publish"]["steps"]
    script = {step["name"]: step for step in steps}["Check chart"]["run"]
    dist = tmp_path / "dist"
    dist.mkdir()
    chart = dist / CHART
    chart.write_bytes(b"chart")
    digest = hashlib.sha256(chart.read_bytes()).hexdigest()
    if tamper:
        chart.write_bytes(b"other chart")
    if rename:
        chart.rename(dist / rename)
    if extra:
        (dist / extra).write_bytes(b"extra")
    if empty:
        chart.unlink()
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "FILE": file,
            "SHA256": digest if sha256 is None else sha256,
        },
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


def test_release_state_checked_first() -> None:
    """A re-run that a published release rejects fails before it signs.

    `publish` checks the state of the release (`release_assets.py
    precheck`) and, for a published release, the digest of the `X.Y.Z`
    image tag right after the chart, before it logs in to Docker Hub,
    attests, signs or tags anything, so that a re-run of all jobs (new
    digest, new chart) leaves the tags of the published release alone.
    "Create release" checks the release again (`state`). The script itself
    is tested by `test_release_state_check_runs`.
    """
    publish = _load(WORKFLOWS / "release.yml")["jobs"]["publish"]
    steps = {step["name"]: step for step in publish["steps"]}
    names = [step["name"] for step in publish["steps"]]
    assert len(names) == len(steps)
    check = steps["Check release state"]
    assert "if" not in check
    assert "continue-on-error" not in check
    assert publish["env"]["RELEASE_TAG"] == "${{ needs.prepare.outputs.release_tag }}"
    assert check["env"]["GH_REPO"] == "${{ github.repository }}"
    assert "release_assets.py precheck" in check["run"]
    assert '--releases "${releases}" dist/*' in check["run"]
    assert '"${IMAGE_REF}:${VERSION}"' in check["run"]
    assert '"${actual}" != "${DIGEST}"' in check["run"]
    assert names.index("Check chart") < names.index("Check release state")
    for later in (
        "Log in to Docker Hub",
        "Attest build provenance",
        "Sign image",
        "Tag image",
        "Attest chart provenance",
        "Sign chart",
    ):
        assert names.index("Check release state") < names.index(later), later
    assert "release_assets.py state" in steps["Create release"]["run"]


@pytest.mark.parametrize(
    ("release", "chart", "tagged", "passes"),
    [
        pytest.param(None, True, None, True, id="none"),
        pytest.param("draft", False, None, True, id="draft"),
        pytest.param("published", True, DIGEST, True, id="rerun-failed-jobs"),
        pytest.param("published", True, OTHER_DIGEST, False, id="new-digest"),
        pytest.param("published", True, None, False, id="no-image-tag"),
        pytest.param("published", False, DIGEST, False, id="new-chart"),
        pytest.param("no-bundle", True, DIGEST, False, id="no-bundle"),
        pytest.param("mutable", True, DIGEST, True, id="mutable-rerun-failed"),
        pytest.param("mutable", False, DIGEST, False, id="mutable-new-chart"),
    ],
)
def test_release_state_check_runs(
    tmp_path: Path,
    release: str | None,
    chart: bool,
    tagged: str | None,
    passes: bool,
) -> None:
    """The release state check of `publish` rejects re-runs of all jobs.

    The step's script runs as in `test_release_chart_check_runs`, with
    stand-ins for `gh` (the releases) and `docker` (the digest of the image
    tag); `python` runs `scripts/release_assets.py`, and `jq` is needed, as
    on the GitHub runners.

    Args:
        tmp_path: The working directory of the step.
        release: The release of the tag, if any: `draft`, `published`
            (immutable), `no-bundle` (published without the chart's
            Sigstore bundle) or `mutable` (published, not immutable).
        chart: Whether the release has this run's chart (else another).
        tagged: The digest that the image tag points to; no tag if `None`.
        passes: Whether the check passes.
    """
    bash = shutil.which("bash")
    assert bash and shutil.which("jq")
    steps = _load(WORKFLOWS / "release.yml")["jobs"]["publish"]["steps"]
    script = {step["name"]: step for step in steps}["Check release state"]["run"]
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "release_assets.py", tmp_path / "scripts")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / CHART).write_bytes(b"chart")
    digest = hashlib.sha256(b"chart" if chart else b"other chart").hexdigest()
    assets = [
        {"name": CHART, "digest": f"sha256:{digest}", "state": "uploaded"},
        {"name": f"{CHART}.sigstore.json", "digest": None, "state": "uploaded"},
    ]
    releases = [{"tag_name": "v1.2.2", "draft": False, "immutable": True, "assets": []}]
    if release is not None:
        draft = release == "draft"
        releases.append(
            {
                "tag_name": "v1.2.3",
                "draft": draft,
                "immutable": release in ("published", "no-bundle"),
                "assets": assets[:1] if release == "no-bundle" else assets,
            }
        )
    (tmp_path / "releases.json").write_text(json.dumps([releases]))
    (tmp_path / "manifest.json").write_text(json.dumps({"digest": tagged}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    stubs = {
        # Only the request of the step gets the releases (`$GH_REPO` is
        # this test's, see below)
        "gh": (
            '[[ "$*" == "api --paginate --slurp '
            'repos/$GH_REPO/releases?per_page=100" ]] && '
            f'cat "{tmp_path}/releases.json"'
        ),
        "docker": (
            '[[ "$*" == "buildx imagetools inspect $IMAGE_REF:1.2.3 '
            '--format {{ json .Manifest }}" ]] && '
            + (f'cat "{tmp_path}/manifest.json"' if tagged else "exit 1")
        ),
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/usr/bin/env bash\n{body}\n")
        stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_REPO": "elixir-cloud-aai/protes",
            "RELEASE_TAG": "v1.2.3",
            "VERSION": "1.2.3",
            "DIGEST": DIGEST,
            "IMAGE_REF": "docker.io/elixircloud/protes",
        },
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if release in ("published", "no-bundle") and not passes:
        assert "release a new patch version" in result.stdout + result.stderr
    if release == "mutable" and not passes:
        assert "gh release delete v1.2.3" in result.stdout + result.stderr
    if tagged is None and release == "published":
        assert "can't be inspected" in result.stdout + result.stderr
    # Not the error of a failed inspection (e.g., wrong `docker` arguments)
    if tagged == OTHER_DIGEST:
        assert f"points to {OTHER_DIGEST}" in result.stdout + result.stderr


# Conditions of the steps of the release workflow: tag pushes (releases), and
# all other events (dry runs)
RELEASE = "github.event_name == 'push'"
DRY_RUN = "github.event_name != 'push'"
# The steps that use the Docker Hub credentials, attest or sign, per job of
# the release workflow: in releases only
RELEASE_ONLY = {
    "push": ["Log in to Docker Hub"],
    "publish": [
        "Log in to Docker Hub",
        "Attest build provenance",
        "Sign image",
        "Attest chart provenance",
        "Sign chart",
    ],
}
# A release commit, for the scripts of the release workflow
SHA = "c" * 40


def _release_steps(job_id: str) -> dict[str, dict[str, Any]]:
    """Get the steps of a job of the release workflow by name.

    Args:
        job_id: The ID of the job.

    Returns:
        The steps of the job, by name (which are unique).
    """
    steps = _load(WORKFLOWS / "release.yml")["jobs"][job_id]["steps"]
    named = {step["name"]: step for step in steps}
    assert len(named) == len(steps)
    return named


def test_release_triggers() -> None:
    """Releases run for version tags, dry runs on demand and on changes.

    Dry runs run for `workflow_dispatch` (with an optional version) and for
    pull requests that change the release workflow, its scripts or the
    release metadata; their mode follows from the event, never from an
    input.
    """
    workflow = _load(WORKFLOWS / "release.yml")
    events = _events(workflow)
    assert set(events) == {"push", "workflow_dispatch", "pull_request"}
    assert events["push"] == {"tags": ["v*"]}
    # No inputs: the version is the one in `CITATION.cff`
    assert not events["workflow_dispatch"]
    paths = events["pull_request"]["paths"]
    assert ".github/workflows/release.yml" in paths
    assert "scripts/release_*.py" in paths
    assert "CITATION.cff" in paths
    assert "docs/releases/**" in paths
    for path in paths:
        if "*" not in path:
            assert (ROOT / path).exists(), path
    assert workflow["env"]["DRY_RUN"] == "${{ " + DRY_RUN + " }}"
    # Pull requests from forks can't create the draft release
    prepare = workflow["jobs"]["prepare"]
    assert "head.repo.full_name == github.repository" in prepare["if"]


def test_dry_run_environment() -> None:
    """Dry runs never enter the `release` environment.

    Only `push` and `publish` run in an environment, `release`, and only for
    tag pushes.
    """
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    environments = {
        job_id: job["environment"]
        for job_id, job in jobs.items()
        if "environment" in job
    }
    assert set(environments) == {"push", "publish"}
    expected = "${{ " + RELEASE + " && 'release' || '' }}"
    assert environments["push"] == expected
    assert environments["publish"]["name"] == expected


def test_dry_run_skips_release_steps() -> None:
    """Dry runs don't log in to Docker Hub, attest or sign.

    These steps run for tag pushes only, and no other step of any job uses
    a secret or attests or signs.
    """
    jobs = _load(WORKFLOWS / "release.yml")["jobs"]
    for job_id in jobs:
        names = RELEASE_ONLY.get(job_id, [])
        steps = _release_steps(job_id)
        for name in names:
            assert steps[name]["if"] == RELEASE, (job_id, name)
        for name, step in steps.items():
            if name in names:
                continue
            text = yaml.safe_dump(step)
            assert "secrets." not in text, (job_id, name)
            assert "attest-build-provenance" not in text, (job_id, name)
            assert "cosign sign" not in text, (job_id, name)


@pytest.mark.parametrize(
    ("dry_run", "passes"),
    [
        pytest.param("true", True, id="dry-run"),
        pytest.param("false", False, id="release"),
    ],
)
def test_ci_check_dry_run(tmp_path: Path, dry_run: str, passes: bool) -> None:
    """Without a CI run for a push of the commit, only dry runs go on.

    Args:
        tmp_path: The working directory of the step.
        dry_run: The value of `DRY_RUN`.
        passes: Whether the step passes.
    """
    bash = shutil.which("bash")
    assert bash
    step = _release_steps("prepare")["Check that CI passed for the release commit"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    log = tmp_path / "gh.log"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\necho 0\n')
    stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", step["run"]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DRY_RUN": dry_run,
            "REPOSITORY": "elixir-cloud-aai/protes",
            "SHA": SHA,
        },
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    level = "::warning::" if passes else "::error::"
    assert level in result.stdout
    # The CI runs of the commit were queried
    (query,) = log.read_text().splitlines()
    assert query.startswith(
        "api repos/elixir-cloud-aai/protes/actions/workflows/ci.yml/runs?"
    )
    assert f"head_sha={SHA}" in query


def test_dry_run_image_ref() -> None:
    """Images are pushed, scanned and tagged at `IMAGE_REF`.

    For tag pushes it is the published image (`IMAGE_NAME`, which the chart
    names in dry runs too), otherwise the registry on the runner. Every step
    that reaches the registry uses it, and dry runs start the registry, with
    the storage of `push`, in each of these jobs.
    """
    workflow = _load(WORKFLOWS / "release.yml")
    env = workflow["env"]
    published = "docker.io/${{ vars.DOCKERHUB_ORG || 'elixircloud' }}/protes"
    assert env["IMAGE_NAME"] == published
    assert env["IMAGE_REF"] == (
        "${{ " + RELEASE + " && format('docker.io/{0}/protes', "
        "vars.DOCKERHUB_ORG || 'elixircloud') || 'localhost:5000/protes' }}"
    )
    assert re.fullmatch(
        r"docker\.io/library/registry@sha256:[0-9a-f]{64}", env["REGISTRY_IMAGE"]
    )
    push = _release_steps("push")
    build = push["Build and push image by digest"]["with"]["outputs"]
    assert build.startswith("type=image,name=${{ env.IMAGE_REF }},push-by-digest=true")
    assert push["Check app version of image"]["env"]["IMAGE"].startswith(
        "${{ env.IMAGE_REF }}@"
    )
    scan = _release_steps("scan")
    assert scan["Scan image"]["with"]["image"].startswith("${{ env.IMAGE_REF }}@")
    publish = _release_steps("publish")
    assert "${IMAGE_NAME}" not in publish["Tag image"]["run"]
    assert '"${IMAGE_REF}@${DIGEST}"' in publish["Tag image"]["run"]
    assert publish["Sign image"]["run"] == 'cosign sign --yes "${IMAGE_REF}@${DIGEST}"'
    start = push["Start local registry"]
    assert start["if"] == DRY_RUN
    for job_id in ("scan", "publish"):
        steps = _release_steps(job_id)
        assert steps["Start local registry"] == start, job_id
        download = steps["Download local registry"]
        assert download["if"] == DRY_RUN
        assert download["with"]["name"] == "registry"
        assert download["with"]["path"] == "${{ runner.temp }}/registry"
        names = list(steps)
        assert names.index("Download local registry") < names.index(
            "Start local registry"
        )
    upload = push["Upload local registry"]
    assert upload["if"] == DRY_RUN
    assert upload["with"]["name"] == "registry"
    assert upload["with"]["path"] == "${{ runner.temp }}/registry"
    # The directory that the registry stores its data in
    assert '"${RUNNER_TEMP}/registry:/var/lib/registry"' in start["run"]
    names = list(push)
    assert names.index("Check app version of image") < names.index(
        "Stop local registry"
    )
    assert names.index("Stop local registry") < names.index("Upload local registry")


def _run_tag_check(
    tmp_path: Path, citation: str, *, tagged: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the tag check of `prepare` as in a dry run.

    Args:
        tmp_path: The working directory of the step (a Git repository).
        citation: The version in `CITATION.cff`.
        tagged: Whether the commit has the tag of that version.

    Returns:
        The completed process; `GITHUB_OUTPUT` is `tmp_path / "output"`.
    """
    bash = shutil.which("bash")
    assert bash and shutil.which("yq") and shutil.which("git")
    script = _release_steps("prepare")["Check release tag and commit"]["run"]
    (tmp_path / "CITATION.cff").write_text(f"cff-version: 1.2.0\nversion: {citation}\n")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    git += ["-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)  # noqa: S603
    if tagged:
        subprocess.run([*git, "tag", f"v{citation}"], check=True)  # noqa: S603
    return subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "DRY_RUN": "true",
            "TAG": "dev",
            "RUN_ID": "42",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
        check=False,
    )


@pytest.mark.parametrize(
    ("citation", "tagged", "tag"),
    [
        pytest.param("1.2.3", False, "v1.2.3", id="release"),
        pytest.param("2.0.0rc1", False, "v2.0.0rc1", id="pre-release"),
        pytest.param("1.2.3", True, "v1.2.3", id="tagged"),
        pytest.param("X.Y.Z", False, None, id="no-version"),
        pytest.param("'1.2.3\n::warning::x'", False, None, id="injected"),
        pytest.param("v1.2.3", False, None, id="prefixed"),
    ],
)
def test_dry_run_tag(
    tmp_path: Path, citation: str, tagged: bool, tag: str | None
) -> None:
    """Dry runs take the version of `CITATION.cff`.

    The version tag is `v<version>`, the release tag a throwaway one with the
    run ID; invalid versions fail and aren't echoed, and an existing tag is
    noted (its release's notes are generated). The tag checks are skipped,
    so the step needs no token here.

    Args:
        tmp_path: The working directory of the step.
        citation: The version in `CITATION.cff` (YAML).
        tagged: Whether the commit has the tag of that version.
        tag: The expected version tag, or None if the step fails.
    """
    result = _run_tag_check(tmp_path, citation, tagged=tagged)
    assert (result.returncode == 0) == (tag is not None), result.stdout + result.stderr
    assert "::warning::x" not in result.stdout + result.stderr
    assert ("is tagged already" in result.stdout) == tagged
    if tag is None:
        return
    output = dict(
        line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines()
    )
    assert output["tag"] == tag
    assert output["release_tag"] == f"dry-run-{tag}-42"
    head = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    assert output["sha"] == head.stdout.strip()


def _run_create_release(
    tmp_path: Path, *, dry_run: bool, release_tag: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the "Create release" step of `publish` with a stand-in for `gh`.

    The repository has no releases, the previous release is `v1.2.2`, and
    `dist/` holds the chart; the curated notes are absent.

    Args:
        tmp_path: The working directory of the step.
        dry_run: Whether the run is a dry run.
        release_tag: The tag of the GitHub release.

    Returns:
        The completed process and the `gh` commands it ran.
    """
    bash = shutil.which("bash")
    assert bash
    script = _release_steps("publish")["Create release"]["run"]
    (tmp_path / "scripts").mkdir()
    for name in ("release_assets.py", "release_notes.py"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / CHART).write_bytes(b"chart")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    log = tmp_path / "gh.log"
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [[ "$1 $2" == "api --paginate" ]]; then echo "[[]]"; fi\n'
        'if [[ "$*" == *generate-notes* ]]; then echo "notes"; fi\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_REPO": "elixir-cloud-aai/protes",
            "DRY_RUN": "true" if dry_run else "false",
            "TAG": "v1.2.3",
            "RELEASE_TAG": release_tag,
            "VERSION": "1.2.3",
            "SHA": SHA,
            "LATEST": "true",
            "PRERELEASE": "false",
            "PREVIOUS": "v1.2.2",
        },
        check=False,
    )
    commands = log.read_text().splitlines() if log.exists() else []
    return result, commands


def test_create_release_runs(tmp_path: Path) -> None:
    """Releases create a draft of the tag, upload the assets and publish it."""
    result, commands = _run_create_release(
        tmp_path, dry_run=False, release_tag="v1.2.3"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (notes,) = [command for command in commands if "generate-notes" in command]
    assert "tag_name=v1.2.3" in notes
    assert "target_commitish" not in notes
    (create,) = [c for c in commands if c.startswith("release create")]
    assert create.startswith("release create v1.2.3 --draft --verify-tag ")
    assert commands[-1].startswith("release edit v1.2.3 ")
    assert commands[-1].endswith(" --draft=false")
    assert "--latest=true" in commands[-1]


def test_dry_run_release_never_published(tmp_path: Path) -> None:
    """Dry runs create a draft of the dry-run tag at the commit, unpublished.

    The notes are generated for the commit, as the version tag doesn't
    exist; the draft is updated, but neither published nor marked latest.
    """
    tag = "dry-run-v1.2.3-42"
    result, commands = _run_create_release(tmp_path, dry_run=True, release_tag=tag)
    assert result.returncode == 0, result.stdout + result.stderr
    (notes,) = [command for command in commands if "generate-notes" in command]
    assert "tag_name=v1.2.3" in notes
    assert f"target_commitish={SHA}" in notes
    (create,) = [c for c in commands if c.startswith("release create")]
    assert create.startswith(f"release create {tag} --draft --target {SHA} ")
    assert "--verify-tag" not in create
    assert f"release upload {tag} dist/{CHART} --clobber" in commands
    assert commands[-1].startswith(f"release edit {tag} ")
    assert "--draft" not in commands[-1]
    assert "--latest" not in commands[-1]
    assert all("--draft=false" not in command for command in commands)


def test_dry_run_release_tag_checked(tmp_path: Path) -> None:
    """Dry runs refuse to create a release of a version tag."""
    result, commands = _run_create_release(tmp_path, dry_run=True, release_tag="v1.2.3")
    assert result.returncode != 0
    assert not [c for c in commands if c.startswith("release ")]


@pytest.mark.parametrize(
    ("release_tag", "deleted"),
    [
        pytest.param("dry-run-v1.2.3-42", ["7"], id="dry-run"),
        pytest.param("v1.2.3", None, id="version-tag"),
    ],
)
def test_dry_run_release_deleted(
    tmp_path: Path, release_tag: str, deleted: list[str] | None
) -> None:
    """Dry runs delete only the drafts of their own release tag, always.

    Args:
        tmp_path: The working directory of the step.
        release_tag: The tag of the GitHub release.
        deleted: The IDs of the releases deleted, or None if the step fails.
    """
    bash = shutil.which("bash")
    assert bash and shutil.which("jq")
    step = _release_steps("publish")["Delete dry-run release"]
    assert step["if"] == "always() && " + DRY_RUN
    releases = [
        [
            {"id": 7, "tag_name": "dry-run-v1.2.3-42", "draft": True},
            {"id": 8, "tag_name": "dry-run-v1.2.3-42", "draft": False},
            {"id": 9, "tag_name": "dry-run-v1.2.3-41", "draft": True},
            {"id": 10, "tag_name": "v1.2.3", "draft": True},
        ]
    ]
    (tmp_path / "releases.json").write_text(json.dumps(releases))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [[ "$1 $2" == "api --paginate" ]]; then '
        f'cat "{tmp_path}/releases.json"; fi\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", step["run"]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_REPO": "elixir-cloud-aai/protes",
            "RELEASE_TAG": release_tag,
        },
        check=False,
    )
    assert (result.returncode == 0) == (deleted is not None), (
        result.stdout + result.stderr
    )
    commands = log.read_text().splitlines() if log.exists() else []
    deletes = [
        c.rsplit("/", 1)[1] for c in commands if c.startswith("api --method DELETE")
    ]
    assert deletes == (deleted or [])


def _credential_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """List the jobs that hold publishing credentials.

    These are the jobs of all workflows that run in an environment
    (`environment:`, which holds the Docker Hub secrets), use a Docker Hub
    secret directly, or may sign or write (`id-token`, `contents`,
    `packages` or `attestations` permission `write`, or `write-all`, also
    inherited from the workflow's permissions). No workflow passes a
    secret to all its jobs (workflow-level `env:`).

    Returns:
        The workflow file name, job ID and job of each such job.
    """
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _load(path)
        assert "secrets." not in yaml.safe_dump(workflow.get("env", {})), path
        for job_id, job in workflow["jobs"].items():
            # Jobs without permissions of their own inherit the workflow's
            permissions = job.get("permissions", workflow.get("permissions", {}))
            writes = permissions == "write-all" or (
                isinstance(permissions, dict)
                and any(
                    permissions.get(scope) == "write"
                    for scope in ("id-token", "contents", "packages", "attestations")
                )
            )
            if (
                "environment" in job
                or "secrets.DOCKERHUB" in yaml.safe_dump(job)
                or writes
            ):
                jobs.append((path.name, job_id, job))
    return jobs


def _commands(step: dict[str, Any]) -> list[str]:
    """Split the script of a step into commands.

    Args:
        step: A step of a job.

    Returns:
        The commands of the step's `run` script: its lines, with
        continuation lines joined, split at `;`, `&&`, `||` and `|`, with
        whitespace normalised.
    """
    lines = step.get("run", "").replace("\\\n", " ").splitlines()
    return [
        " ".join(command.split())
        for line in lines
        for command in re.split(r"&&|\|\||;|\|", line)
        if command.strip()
    ]


# pip (also `pip3`, `pip3.13`, `"<venv>/bin/pip"`, with global options and
# their values) commands that install or build packages; the latter runs
# their build backends
PIP = re.compile(r"\bpip[0-9.]*[\"']?(\s+\S+)*?\s+(install|wheel|download)\b")
# Other installers (uv also with global options before the subcommand),
# and builds that install build requirements
INSTALLERS = re.compile(
    r"\b(pipx|pipenv|easy_install|uvx|pip-sync|poetry|pdm|hatch|conda|mamba"
    r"|micromamba|uv(\s+\S+)*?\s+(pip|tool|sync|run|add|build|venv)"
    r"|python[0-9.]*\s+-m\s+build)\b"
)
# `uv sync`, also with options before the subcommand
UV_SYNC = re.compile(r"\buv\s+(\S+\s+)*?sync\b")
# The jobs of `ci.yml` that install the app's dependencies, with their extra
LOCKED_JOBS = {"lint": "dev", "test": "dev", "chart": "dev", "docs": "docs"}


def test_credential_jobs_found() -> None:
    """The jobs that publish images are found as credential jobs."""
    found = {(name, job_id) for name, job_id, _ in _credential_jobs()}
    assert {
        ("ci.yml", "push"),
        ("ci.yml", "publish"),
        ("release.yml", "push"),
        ("release.yml", "publish"),
    } <= found


@pytest.mark.parametrize(
    ("command", "found"),
    [
        pytest.param("pip install foo", True, id="pip"),
        pytest.param("pip3.13 install foo", True, id="versioned"),
        pytest.param('"${VENV}/bin/pip" install foo', True, id="venv"),
        pytest.param("python -m pip wheel .", True, id="wheel"),
        pytest.param("pip download foo", True, id="download"),
        pytest.param("pip -q install foo", True, id="global-option"),
        pytest.param("pip --log pip.log install foo", True, id="option-value"),
        pytest.param("pip list", False, id="list"),
        pytest.param("echo pipeline install", False, id="other-word"),
    ],
)
def test_pip_pattern(command: str, found: bool) -> None:
    """The pattern finds pip commands that install or build packages.

    Args:
        command: A command.
        found: Whether the pattern finds it.
    """
    assert bool(PIP.search(command)) == found


@pytest.mark.parametrize(
    "command",
    [
        "pipx run foo",
        "pipenv install",
        "uv sync",
        "uv build",
        "python -m build",
        "poetry install",
        "conda install foo",
        "micromamba install foo",
        "uv --offline run pytest",
        "uv --directory x pip install foo",
    ],
)
def test_installers_pattern(command: str) -> None:
    """The pattern finds other installers and builds.

    Args:
        command: A command.
    """
    assert INSTALLERS.search(command)


def test_commands_split() -> None:
    """Commands chained on one line are checked one by one."""
    step = {"run": "pip install -r r.txt && pip install foo; echo a | tee b\n"}
    assert _commands(step) == [
        "pip install -r r.txt",
        "pip install foo",
        "echo a",
        "tee b",
    ]


@pytest.mark.parametrize(("job_id", "extra"), LOCKED_JOBS.items())
def test_jobs_on_lock(job_id: str, extra: str) -> None:
    """The jobs that install the app's dependencies install the locked ones.

    They run on the versions of `uv.lock`, so that pull requests that change
    only the lock (e.g., Dependabot's) are tested on it, with uv of a pinned
    `astral-sh/setup-uv`; no pip command installs anything.

    Args:
        job_id: The ID of the job.
        extra: The extra that the job installs.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"][job_id]
    steps = {step.get("name"): step for step in job["steps"]}
    uv = steps["Set up uv"]
    assert re.fullmatch(r"astral-sh/setup-uv@[0-9a-f]{40}", uv["uses"])
    commands = _commands(steps["Install dependencies"])
    (sync,) = [shlex.split(command) for command in commands if UV_SYNC.search(command)]
    assert "--locked" in sync, (job_id, sync)
    assert "--frozen" not in sync, (job_id, sync)
    extras = {
        value
        for option, value in zip(sync, [*sync[1:], ""], strict=True)
        if option == "--extra"
    } | {
        token.removeprefix("--extra=") for token in sync if token.startswith("--extra=")
    }
    assert extras == {extra}, (job_id, sync)
    assert "--all-extras" not in sync, (job_id, sync)
    # Later steps run the tools of `.venv`
    assert 'echo "${PWD}/.venv/bin" >> "$GITHUB_PATH"' in commands, job_id
    names = list(steps)
    assert names.index("Set up Python") < names.index("Set up uv")
    assert names.index("Set up uv") < names.index("Install dependencies")


def test_lock_checked() -> None:
    """The lint job checks that the lock matches `pyproject.toml`."""
    steps = _load(WORKFLOWS / "ci.yml")["jobs"]["lint"]["steps"]
    names = [step.get("name") for step in steps]
    check = {step.get("name"): step for step in steps}["Check lock file"]
    assert _commands(check) == ["uv lock --check"]
    assert names.index("Check lock file") < names.index("Install dependencies")


def _allowed_install(command: str) -> bool:
    """Check whether a command of a job without credentials may install.

    Allowed are `uv sync --locked`, `pipx run` of a pinned tool (`--spec
    <name>==<version>`, e.g., cffconvert in `lint`, in its own environment)
    and `python -m build` (the build of the distributions in `lint`, with
    build requirements from the index, as the editable install of the app).

    Args:
        command: A command.

    Returns:
        Whether the command is allowed.
    """
    tokens = shlex.split(command)
    if UV_SYNC.search(command):
        # Locked, from the lock's sources and indexes only
        overrides = {"--frozen", "--no-sources", "--index", "--default-index"}
        overrides |= {"--index-url", "--extra-index-url", "--find-links"}
        return tokens[:3] == ["uv", "sync", "--locked"] and not any(
            token.split("=")[0] in overrides for token in tokens
        )
    if command.startswith("pipx run --spec "):
        return "==" in tokens[3]
    return command == "python -m build"


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        pytest.param("uv sync --locked --extra dev", True, id="sync-locked"),
        pytest.param("uv sync --extra dev", False, id="sync"),
        pytest.param("uv sync --locked --frozen", False, id="sync-frozen"),
        pytest.param("uv sync --locked --index-url=x", False, id="sync-index"),
        pytest.param("uv sync --locked --no-sources", False, id="sync-sources"),
        pytest.param("env X=1 uv sync --locked", False, id="sync-env"),
        pytest.param("uv --quiet sync --locked", False, id="sync-option"),
        pytest.param("pipx run --spec foo==1.0 foo", True, id="pipx-pinned"),
        pytest.param("pipx run --spec foo>=1 foo", False, id="pipx-range"),
        pytest.param("pipx run foo", False, id="pipx"),
        pytest.param("uvx ruff", False, id="uvx"),
        pytest.param("python -m build", True, id="build"),
    ],
)
def test_allowed_install(command: str, allowed: bool) -> None:
    """Only locked syncs, pinned `pipx run` and the build may install.

    Args:
        command: A command.
        allowed: Whether it is allowed.
    """
    assert _allowed_install(command) == allowed


def test_no_unlocked_installs() -> None:
    """No job without credentials installs Python packages but from the lock.

    No pip command installs anything there, and other installers are used
    only as `_allowed_install` allows. Jobs with publishing credentials are
    checked on their own.
    """
    credential = {(name, job_id) for name, job_id, _ in _credential_jobs()}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_id, job in _load(path)["jobs"].items():
            if (path.name, job_id) in credential:
                continue
            for step in job.get("steps", []):
                for command in _commands(step):
                    where = (path.name, job_id, step.get("name"), command)
                    assert not PIP.search(command), where
                    if INSTALLERS.search(command) or UV_SYNC.search(command):
                        assert _allowed_install(command), where


def test_uv_version_locked() -> None:
    """Every `astral-sh/setup-uv` step installs the uv locked in `uv.lock`.

    The action reads the version from the lock (`version-file`), where uv is
    a dev dependency; it fails if the lock has none. Dependabot bumps it
    with the other locked packages.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert [package for package in lock["package"] if package["name"] == "uv"]
    setups = [
        (path.name, job_id, step)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in _load(path)["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert setups
    for name, job_id, step in setups:
        assert step["with"] == {"version-file": "uv.lock"}, (name, job_id)


# The only options of an install in a job with credentials, besides `-r`
HASH_CHECKED = ["--require-hashes", "--only-binary", ":all:", "--no-deps"]


def test_credential_jobs_install_hash_checked() -> None:
    """Jobs with publishing credentials install Python packages hash-checked.

    A compromised release of a package installed unpinned would run code in
    a job that holds registry credentials. So every pip command there that
    installs or builds packages is a `pip install` of exported locked
    requirements (`-r`) with hash checks, as wheels only and without
    resolving anything else, and no other installer is used. Actions
    (`uses:`) are pinned by commit and not checked here.
    """
    for name, job_id, job in _credential_jobs():
        for step in job["steps"]:
            for command in _commands(step):
                where = (name, job_id, step.get("name"), command)
                assert not INSTALLERS.search(command), where
                assert not UV_SYNC.search(command), where
                if match := PIP.search(command):
                    assert match.group(2) == "install", where
                    # Exactly these options and one requirements file (no
                    # other files, packages or options)
                    options = shlex.split(command[match.end() :])
                    assert options.count("-r") == 1, where
                    file = options.index("-r")
                    assert options[file + 1].startswith("$"), where
                    rest = options[:file] + options[file + 2 :]
                    assert rest == HASH_CHECKED, where


def _derive_version() -> dict[str, Any]:
    """Get the step of the `push` job of `ci.yml` that derives the version.

    Returns:
        The step.
    """
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    names = [step["name"] for step in jobs["push"]["steps"]]
    # Unique, so that the order checked is that of the step returned
    assert len(names) == len(set(names))
    steps = dict(zip(names, jobs["push"]["steps"], strict=True))
    # Before the job logs in to Docker Hub
    assert names.index("Derive version") < names.index("Log in to Docker Hub")
    return steps["Derive version"]


def test_dev_version_from_locked_build_requirements() -> None:
    """The `push` job of `ci.yml` derives the version with the locked tools.

    setuptools-scm and its dependencies are the `build-system` group of
    `uv.lock`, exported by the uv image of the `Dockerfile` (pinned by
    digest) and run from the virtual environment they are installed into
    (with hash checks, see `test_credential_jobs_install_hash_checked`).
    """
    script = " ".join(_commands(_derive_version()))
    assert '"${uv_image}" export --frozen --only-group build-system' in script
    assert "--no-emit-project" in script
    assert "--network none" in script
    # pip installs exactly the exported requirements
    assert '--project /src > "${requirements}"' in script
    assert '--no-deps -r "${requirements}"' in script
    assert re.search(r"build-env/bin/python\"? -m setuptools_scm", script)


def test_dev_version_uv_image() -> None:
    """The step finds the uv image of the `Dockerfile`, with its digest.

    The step's `sed` command runs against the `Dockerfile`, as in the step
    (GNU `sed`, as on the GitHub runners).
    """
    sed = shutil.which("sed")
    assert sed
    (command,) = [
        command
        for command in _commands(_derive_version())
        if command.startswith('uv_image="$(sed ')
    ]
    args = shlex.split(command.removeprefix('uv_image="$(').removesuffix(')"'))
    assert args[0] == "sed"
    assert args[-1] == "Dockerfile"
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [sed, *args[1:]],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^FROM (ghcr\.io/astral-sh/uv:\S+) AS uv$", dockerfile, re.M)
    assert match
    assert re.fullmatch(r"ghcr\.io/astral-sh/uv:[^@]+@sha256:[0-9a-f]{64}", match[1])
    assert result.stdout == match[1] + "\n"


def test_build_system_group() -> None:
    """The `build-system` group is `build-system.requires`, and locked."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["dependency-groups"]["build-system"]
    assert group == pyproject["build-system"]["requires"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (project,) = [
        package for package in lock["package"] if package["name"] == "pro-tes"
    ]
    locked = [entry["name"] for entry in project["dev-dependencies"]["build-system"]]
    assert sorted(locked) == sorted(Requirement(spec).name for spec in group)
