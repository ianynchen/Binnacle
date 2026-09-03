import subprocess
from pathlib import Path


def test_import_linter_layering_passes() -> None:
    repo_root = Path(__file__).parents[2]
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
