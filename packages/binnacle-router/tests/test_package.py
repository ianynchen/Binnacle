import tomllib
from pathlib import Path

import binnacle_router


def test_installed_version_matches_the_declared_one() -> None:
    """Confirms the workspace/uv wiring actually installs this package, not
    just that Python syntax is valid. `__version__` is read from the
    installed distribution's metadata, so a workspace members list or
    pyproject.toml typo that left the package unbuilt raises
    PackageNotFoundError at import; comparing it against the version
    pyproject.toml declares additionally catches a stale editable install,
    where the environment still reports the previous release."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared: str = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert binnacle_router.__version__ == declared
