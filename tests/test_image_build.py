"""Tests of the image build (`Dockerfile`): locked, hash-checked installs.

The image build downloads packages only with hash checks: the runtime
dependencies and the build requirements of the app (`build-system.requires`)
are both exported from the lock (`uv.lock`), with their hashes. The only
exception is the build argument `PLUGINS` for local builds, empty for the
published images. The build context is tested in `tests/test_build.py`.
"""

import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

from uv import find_uv_bin

ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "Dockerfile"
# Dependency group that locks the build requirements of the app
GROUP = "build-system"


def _export(*args: str) -> str:
    """Export locked requirements with uv, as the image build does.

    Args:
        *args: Arguments that select the requirements (e.g., `--no-dev`).

    Returns:
        The exported requirements file.
    """
    # uv comes with the dev dependencies
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [
            find_uv_bin(),
            "export",
            "--frozen",
            "--no-emit-project",
            "--no-header",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={**os.environ, "UV_NO_CACHE": "1", "UV_OFFLINE": "1"},
    )
    return result.stdout


def _pinned(requirements: str) -> dict[str, list[str]]:
    """Get the pinned packages of a requirements file, with their hashes.

    Args:
        requirements: Requirements file, as `uv export` writes it.

    Returns:
        Hashes by package name.
    """
    lines = re.sub(r"\\\n\s*", " ", requirements).splitlines()
    packages = {}
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        name, _, rest = line.partition("==")
        packages[name.strip()] = re.findall(r"--hash=sha256:([0-9a-f]{64})", rest)
    return packages


def _runs() -> list[tuple[list[str], list[str]]]:
    """Get the `RUN` instructions of the Dockerfile.

    Continued lines are joined, and comments are left out. The instruction
    that installs the plugins of the build argument `PLUGINS` is left out,
    too: empty by default, as for the published images, and documented as
    not pinned by hash (for local builds only, see `docs/plugin-images.md`).

    Returns:
        The options of each instruction (e.g., `--network=none`), and its
        shell commands, one per command of a list or pipeline (separated by
        `&&`, `||`, `;` or `|`).
    """
    lines = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    text = "\n".join(lines).replace("\\\n", " ")
    runs = []
    for run in re.findall(r"^RUN\s+(.+)$", text, flags=re.MULTILINE):
        # Heredocs (`RUN <<EOF`) would hide their commands from the checks
        assert "<<" not in run, run
        if "${PLUGINS}" in run:
            continue
        match = re.fullmatch(r"((?:--\S+\s+)*)(.*)", run)
        assert match, run
        options, commands = match.groups()
        runs.append(
            (options.split(), [c.strip() for c in re.split(r"&&|\|\||;|\|", commands)])
        )
    return runs


def _run_commands() -> list[str]:
    """Get the shell commands of the `RUN` instructions of the Dockerfile.

    Returns:
        Commands, one per command of a list or pipeline.
    """
    return [command for _, commands in _runs() for command in commands]


def _uv_args(command: str) -> list[str] | None:
    """Get the arguments of uv in a shell command.

    Args:
        command: Shell command (e.g., of a `RUN` instruction).

    Returns:
        The arguments after `uv`, starting with its command (e.g., `pip`), or
        `None` if the command doesn't run uv.
    """
    args = shlex.split(command)
    if args and Path(args[0]).name == "uv":
        return args[1:]
    return None


def _runs_pip(command: str) -> bool:
    """Tell whether a shell command runs pip itself (not `uv pip`).

    Args:
        command: Shell command (e.g., of a `RUN` instruction).

    Returns:
        Whether the command runs `pip`, `pip3`, `pip3.13`, `<venv>/bin/pip`
        or `python -m pip`.
    """
    args = shlex.split(command)
    if _uv_args(command) is not None:
        return False
    return any(re.fullmatch(r"pip(3(\.\d+)?)?", Path(arg).name) for arg in args)


def test_build_group_matches_build_system() -> None:
    """The dependency group lists exactly the build requirements of the app."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    group = pyproject["dependency-groups"][GROUP]
    requires = pyproject["build-system"]["requires"]
    assert group == requires


def test_build_requirements_hashed() -> None:
    """The locked build requirements are complete, each with hashes."""
    packages = _pinned(_export("--only-group", GROUP))
    assert {"setuptools", "setuptools-scm"} <= packages.keys()
    for name, hashes in packages.items():
        assert hashes, name


def test_runtime_requirements_without_build_requirements() -> None:
    """The runtime requirements of the image leave out the build group."""
    packages = _pinned(_export("--no-dev"))
    assert "connexion" in packages
    assert "setuptools" not in packages
    assert "setuptools-scm" not in packages


def test_downloads_hash_checked() -> None:
    """Each install of the image build checks hashes or downloads nothing.

    uv installs and builds everything; pip isn't run. uv commands that may
    download packages (`uv pip install`) take hash-pinned requirements only
    (`--require-hashes`, `-r`), as wheels (`--only-binary :all:`), without
    resolving anything else (`--no-deps`), or no index at all (`--no-index`,
    `--offline`), and then wheels only. The app is built offline with the
    hash-checked build requirements, not in an isolated build environment,
    which would be filled with unchecked downloads.
    """
    commands = []
    for command in _run_commands():
        assert not _runs_pip(command), command
        uv_args = _uv_args(command)
        if uv_args is not None:
            commands.append(uv_args)
    subcommands = [args[0] for args in commands]
    assert set(subcommands) <= {"export", "venv", "pip", "build"}, subcommands
    # The app is built as a wheel (checked below), not by an install of its
    # sources
    assert subcommands.count("build") == 1, subcommands
    for args in commands:
        if args[0] == "pip":
            assert args[1] == "install", args
            if "--no-index" in args:
                assert "--offline" in args, args
                targets = [arg for arg in args[2:] if not arg.startswith("-")]
                assert targets, args
                assert all(target.endswith(".whl") for target in targets), args
            else:
                assert "--require-hashes" in args, args
                assert args[args.index("--only-binary") + 1] == ":all:", args
                assert "--no-deps" in args, args
                assert "-r" in args, args
            assert "--no-deps" in args, args
        elif args[0] == "build":
            assert "--offline" in args, args
            assert "--no-build-isolation" in args, args
            assert "--wheel" in args, args
        elif args[0] == "export":
            assert "--frozen" in args, args


def test_builds_offline() -> None:
    """The steps that build and install the app have no network.

    So that a download (e.g., of a build requirement missing from the lock)
    fails the image build, instead of only being refused by `--offline`.
    """
    offline = [
        options
        for options, commands in _runs()
        if any("--offline" in (_uv_args(command) or []) for command in commands)
    ]
    # At least the build and the install of the wheel
    assert len(offline) >= 2, offline
    for options in offline:
        assert "--network=none" in options, options


def test_uv_mounted_only() -> None:
    """The uv image is only mounted into the steps that use it, not copied.

    The steps also keep no uv cache (`UV_NO_CACHE=1`) in their layers and
    download no Python (`UV_PYTHON_DOWNLOADS=never`): they use the one of
    the base image.
    """
    text = DOCKERFILE.read_text()
    assert not re.search(r"^COPY\s+--from=uv\b", text, flags=re.MULTILINE)
    uses = [
        (options, commands)
        for options, commands in _runs()
        if any(_uv_args(command) is not None for command in commands)
    ]
    assert uses
    for options, commands in uses:
        assert "--mount=from=uv,source=/uv,target=/usr/local/bin/uv" in options
        assert commands[0] == "export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never"


def test_plugins_only_on_request() -> None:
    """The unpinned install of `PLUGINS` runs only if the argument is set.

    It is the only step left out of the checks above (see `_runs()`), so it
    must stay a single, conditional step with an empty default.
    """
    text = DOCKERFILE.read_text().replace("\\\n", " ")
    assert re.search(r'^ARG PLUGINS=""$', text, flags=re.MULTILINE)
    runs = [
        run
        for run in re.findall(r"^RUN\s+(.+)$", text, flags=re.MULTILINE)
        if "PLUGINS" in run
    ]
    assert len(runs) == 1, runs
    assert re.match(r'if \[ -n "\$\{PLUGINS\}" \]; then\s', runs[0]), runs[0]
