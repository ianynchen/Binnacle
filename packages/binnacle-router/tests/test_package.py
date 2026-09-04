import binnacle_router


def test_package_is_importable_from_the_workspace_env() -> None:
    """Confirms the workspace/uv wiring actually installs this package,
    not just that Python syntax is valid -- the failure mode this guards
    against is a workspace members list or pyproject.toml typo that
    silently leaves the package unbuilt."""
    assert binnacle_router.__version__ == "0.1.0"
