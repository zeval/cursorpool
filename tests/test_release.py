from __future__ import annotations

import re
import tomllib
from pathlib import Path

import cursorpool


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"


def test_release_version_matches_project_and_runtime() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert re.fullmatch(r"\d+\.\d+\.\d+", project["version"])
    assert cursorpool.__version__ == project["version"]


def test_release_metadata_uses_spdx_and_includes_license() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert (ROOT / "LICENSE").is_file()


def test_pypi_release_has_install_docs_and_project_links() -> None:
    metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    readme = README.read_text(encoding="utf-8")

    assert metadata["project"]["urls"] == {
        "Homepage": "https://github.com/zeval/cursorpool",
        "Repository": "https://github.com/zeval/cursorpool",
        "Issues": "https://github.com/zeval/cursorpool/issues",
    }
    assert 'pipx install "git+ssh://git@github.com/zeval/cursorpool.git' in readme
    assert "not published to PyPI" in readme


def test_build_workflow_smoke_tests_machine_api_from_built_wheel() -> None:
    workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert "--force-reinstall --no-deps dist/*.whl" in workflow
    assert "cursorpool --home" in workflow
    assert "stats --json" in workflow
    assert 'payload["schema_version"] == 2' in workflow
    assert "actions/upload-artifact@" in workflow
    assert "contents: read" in workflow


def test_build_workflow_pins_actions_to_commit_shas() -> None:
    uses = re.findall(r"uses: ([^\s]+)", BUILD_WORKFLOW.read_text())
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in uses)
