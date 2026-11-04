"""Tests of the scripts of the recipe for plugin images, with a fake Docker.

The smoke test of CI runs them with Docker, with a single plugin; these tests
check what `resolve.sh` writes for more output of uv.
"""

from pathlib import Path
import re
import subprocess

import pytest

SCRIPTS = Path(__file__).parents[1] / "examples/plugin-image"
# What uv writes, with several hashes and a package that the image has
COMPILED = """\
maxminddb==3.1.1 \\
    --hash=sha256:aaa \\
    --hash=sha256:bbb
pkg-from-image==1.0 \\
    --hash=sha256:ccc
requests-toolbelt==1.0.0 \\
    --hash=sha256:ddd
"""


def _docker(path: Path, compiled: str, rc: int = 0) -> None:
    """Write a fake `docker` to a directory.

    Args:
        path: Directory.
        compiled: Output of `uv pip compile`.
        rc: Exit code of `uv pip compile`.
    """
    (path / "compiled").write_text(compiled)
    docker = path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {path}/calls\n'
        'case "$1 $*" in\n'
        "  create*) echo container ;;\n"
        '  cp*) touch "${@: -1}" ;;\n'
        "  rm*) ;;\n"
        f"  *\\ pip\\ compile*) cat {path}/compiled; exit {rc} ;;\n"
        "  run*) echo pkg-from-image==1.0; echo other==2.0 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)


def _resolve(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run `resolve.sh` with the fake `docker`.

    Args:
        tmp_path: Directory with the fake `docker`, and for the files.

    Returns:
        The finished process.
    """
    (tmp_path / "plugins.in").write_text("maxminddb\n")
    (tmp_path / "plugins.txt").write_text("stale\n")
    return _run(
        "resolve.sh", tmp_path, tmp_path / "plugins.in", tmp_path / "plugins.txt"
    )


def _run(script: str, tmp_path: Path, *args: Path) -> subprocess.CompletedProcess[str]:
    """Run a script of the recipe with the fake `docker`.

    Args:
        script: Name of the script.
        tmp_path: Directory with the fake `docker`, also for temporary files.
        args: Arguments of the script after the image.

    Returns:
        The finished process.
    """
    return subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [SCRIPTS / script, "image", *args],
        capture_output=True,
        check=False,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        text=True,
    )


def _calls(tmp_path: Path) -> list[str]:
    """Get the calls of the fake `docker`.

    Args:
        tmp_path: Directory with the fake `docker`.

    Returns:
        The arguments of each call.
    """
    return (tmp_path / "calls").read_text().splitlines()


def test_resolve_pins(tmp_path: Path) -> None:
    """Each distribution that the image lacks gets a line, with all its hashes."""
    _docker(tmp_path, COMPILED)
    assert _resolve(tmp_path).returncode == 0
    calls = _calls(tmp_path)
    assert "rm --force container" in calls
    (compile_call,) = [call for call in calls if "pip compile" in call]
    assert "--constraint /work/image.txt" in compile_call
    assert compile_call.endswith(" /work/plugins.in")
    assert (tmp_path / "plugins.txt").read_text().splitlines() == [
        "maxminddb==3.1.1 --hash=sha256:aaa --hash=sha256:bbb",
        "requests-toolbelt==1.0.0 --hash=sha256:ddd",
    ]


@pytest.mark.parametrize(
    "compiled",
    ["", "pkg-from-image==1.0 \\\n    --hash=sha256:ccc\n"],
    ids=["nothing", "only-of-image"],
)
def test_resolve_nothing_to_add(tmp_path: Path, compiled: str) -> None:
    """Without anything to add, `plugins.txt` is empty."""
    _docker(tmp_path, compiled)
    assert _resolve(tmp_path).returncode == 0
    assert (tmp_path / "plugins.txt").read_text() == ""


def test_resolve_fails(tmp_path: Path) -> None:
    """If uv can't resolve the plugins, the script fails, with no pins left."""
    _docker(tmp_path, "", rc=1)
    assert _resolve(tmp_path).returncode == 1
    assert "rm --force container" in _calls(tmp_path)
    assert not (tmp_path / "plugins.txt").exists()


def test_download(tmp_path: Path) -> None:
    """`download.sh` checks hashes, with a pinned pip, and removes the container."""
    _docker(tmp_path, "")
    (tmp_path / "plugins.txt").write_text("name==1.0 --hash=sha256:aaa\n")
    result = _run(
        "download.sh", tmp_path, tmp_path / "plugins.txt", tmp_path / "wheels"
    )
    assert result.returncode == 0
    calls = _calls(tmp_path)
    assert "rm --force container" in calls
    (download,) = [call for call in calls if "pip download" in call]
    assert re.search(r"--from pip==\d+(\.\d+)+ pip download", download)
    assert "--require-hashes --requirement /work/plugins.txt" in download
    assert "--only-binary :all:" in download
    assert (tmp_path / "wheels").is_dir()
