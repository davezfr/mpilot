from __future__ import annotations

import pathlib
import re
import subprocess
import tomllib


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".ico"}


def test_compose_can_render_prowlarr_onboarding_before_api_key_exists():
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker/docker-compose.yml",
            "--env-file",
            ".env.example",
            "config",
            "prowlarr",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_readme_is_mpilot_product_surface_with_assets():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    screenshot_paths = [
        "assets/readme/telegram-download-and-subtitle-one-shot.jpg",
        "assets/readme/bilingual-ass-his-girl-friday.jpg",
        "assets/readme/telegram-imdb-release-picker.jpg",
        "assets/readme/telegram-subtitle-after-download-ready.jpg",
    ]

    assert readme.startswith("# MPilot")
    assert "mpilot-mcp" in readme
    assert "media_request" in readme
    assert "acquisition_*" in readme
    assert "## Migration From qBitlarr And Babelarr" in readme
    for screenshot_path in screenshot_paths:
        assert screenshot_path in readme
        assert (REPO_ROOT / screenshot_path).exists()


def test_pyproject_exposes_only_mpilot_console_scripts():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"] == {
        "mpilot": "mpilot.cli:main",
        "mpilot-mcp": "mpilot.mcp.server:main",
        "mpilot-daemon": "mpilot.daemon.cli:main",
    }
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["mpilot*"]


def test_dependency_groups_and_requirements_stay_in_sync():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = pyproject["project"]["optional-dependencies"]
    requirements = {
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert set(optional["all"]) == set(optional["download"]) | set(optional["mcp"])
    assert requirements == set(optional["all"])


def test_legacy_entrypoints_and_shim_packages_are_not_tracked():
    forbidden_paths = {
        "app",
        "babel" + "arr",
        "mcp_server",
        "media_subtitle_translator",
        "media_workflow_runtime",
        "docs/legacy",
        "bin/" + "qbitlarr",
        "bin/" + "qbit" + "larr-mcp",
        "bin/" + "babel" + "arr-mcp",
        "bin/" + "babel" + "arr-runtime-mcp",
        "bin/mst-mcp",
        "bin/mwr-mcp",
    }
    result = subprocess.run(
        ["git", "ls-files", *sorted(forbidden_paths)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_tracked_text_files_do_not_reintroduce_old_public_entrypoints():
    forbidden_patterns = [
        r"\bbin/" + "qbitlarr" + r"\b",
        r"\bbin/" + "qbit" + "larr-mcp" + r"\b",
        r"\bbin/" + "babel" + "arr-mcp" + r"\b",
        r"\bbin/" + "babel" + "arr-runtime-mcp" + r"\b",
        r"\b" + "qbit" + "larr-mcp" + r"\b",
        r"\b" + "babel" + "arr-mcp" + r"\b",
        r"\b" + "babel" + "arr-runtime-mcp" + r"\b",
        r"\b" + "media-" + "subtitle-translator" + r"\b",
        r"\b" + "media-" + "workflow-runtime" + r"\b",
        r"(?m)^from " + "app" + r"\b",
        r"(?m)^import " + "app" + r"\b",
        r"(?m)^from " + "babelarr" + r"\b",
        r"(?m)^import " + "babelarr" + r"\b",
        r"(?m)^from " + "media_workflow" + "_runtime" + r"\b",
        r"(?m)^import " + "media_workflow" + "_runtime" + r"\b",
    ]
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

    for relative_path in result.stdout.splitlines():
        path = REPO_ROOT / relative_path
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in forbidden_patterns:
            assert not re.search(pattern, content), f"{pattern!r} found in {relative_path}"


def test_dockerfile_runs_api_as_mpilot_non_root_user():
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert 'pip install --no-cache-dir ".[download,mcp]"' in dockerfile
    assert "adduser --system --ingroup mpilot mpilot" in dockerfile
    assert "USER mpilot" in dockerfile


def test_compose_uses_pinned_third_party_image_tags():
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")

    assert ":latest" not in compose
    assert "lscr.io/linuxserver/prowlarr:2.4.0.5397-ls149" in compose
    assert "ghcr.io/flaresolverr/flaresolverr:v3.5.0" in compose
    assert "container_name: mpilot-api" in compose


def test_compose_binds_public_services_to_loopback_only():
    compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:8000:8000"' in compose
    assert '"127.0.0.1:9696:9696"' in compose
    assert '"8000:8000"' not in compose
    assert '"9696:9696"' not in compose
    assert "8191:8191" not in compose


def test_github_actions_runs_pytest_on_push_and_pull_request():
    workflow = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    assert workflow.exists()
    content = workflow.read_text(encoding="utf-8")
    assert "pull_request:" in content
    assert "push:" in content
    assert "python -m pytest -q" in content
    assert "python -m pip_audit -r requirements.txt" in content
    assert "Verify dependency-free base CLI" in content
    assert "/tmp/mpilot-base/bin/mpilot --help" in content
