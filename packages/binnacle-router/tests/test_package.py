from importlib.metadata import version

import binnacle_router


def test_package_is_importable_from_the_workspace_env() -> None:
    """Confirms the workspace/uv wiring actually installs this package,
    not just that Python syntax is valid -- the failure mode this guards
    against is a workspace members list or pyproject.toml typo that
    silently leaves the package unbuilt. Comparing against the installed
    distribution's own metadata rather than a literal also catches
    `__version__` drifting from pyproject.toml, and does not need editing
    on every release."""
    assert binnacle_router.__version__ == version("binnacle-router")
