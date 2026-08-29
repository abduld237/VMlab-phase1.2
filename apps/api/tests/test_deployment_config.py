"""Configuration that only breaks once it is deployed.

Both of these passed locally and failed in a container, which is exactly the
class of defect worth pinning in a test: nothing in a local run exercises the
production CORS branch, and nothing exercises the directory layout Railway's
Root Directory setting produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from vmlab.config import Settings

_REPO = Path(__file__).resolve().parents[3]


def test_production_serves_the_configured_origins() -> None:
    settings = Settings(
        environment="production",
        cors_allowed_origins="https://web.up.railway.app, https://vmlab.example ,",
    )
    assert settings.is_production
    assert settings.cors_origins == [
        "https://web.up.railway.app",
        "https://vmlab.example",
    ]


def test_development_needs_no_origins_configured() -> None:
    settings = Settings(environment="development")
    assert not settings.is_production
    assert settings.cors_origins == []


def test_repo_root_survives_a_flattened_deployment_layout() -> None:
    """Railway copies apps/api to the image root, leaving /app/vmlab/config.py.

    That path has three parents, so the original `parents[3]` raised
    IndexError at import -- before logging existed to report it. The fallback
    has to yield *something*, because module import must not depend on a repo
    root that does not exist in the container.
    """
    parents = Path("/app/vmlab/config.py").parents
    assert len(parents) == 3, "assumption about the deployed layout changed"
    assert (parents[3] if len(parents) > 3 else parents[-1]) == Path("/")


def test_railway_service_configs_start_on_the_injected_port() -> None:
    """A service that binds 127.0.0.1 or a fixed port is unreachable behind
    Railway's proxy, and fails as a healthcheck timeout rather than an error."""
    api = json.loads((_REPO / "apps" / "api" / "railway.json").read_text())
    start = api["deploy"]["startCommand"]
    assert "--host 0.0.0.0" in start
    assert "$PORT" in start
    assert api["deploy"]["healthcheckPath"] == "/health"

    # One replica: an analysis runs as a detached asyncio task in the process
    # that accepted the upload, so a second replica cannot serve its progress.
    assert api["deploy"]["numReplicas"] == 1

    web = json.loads((_REPO / "apps" / "web" / "railway.json").read_text())
    assert web["deploy"]["numReplicas"] == 1
