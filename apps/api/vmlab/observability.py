"""LangSmith tracing, switched on from configuration rather than the shell.

The tracing client reads `os.environ` directly at run time. Our settings come
from pydantic-settings, which parses `.env` into a Settings object and does
*not* export anything back to the process environment -- so putting
LANGSMITH_API_KEY in `.env` alone traces nothing, silently. This module is the
bridge: it copies the four values across before the first graph runs.

What a trace contains, and what it does not: the graph is LangGraph, but the
model calls underneath are plain httpx to OpenRouter, not LangChain runnables.
So a trace shows node order, the three-way fan-out, per-node duration and the
state each node returned -- the orchestration. It does not show prompts,
completions or token counts as LLM spans, because nothing in the call path is
a LangChain model object. Those numbers already come from our own telemetry
(`stage_timings_ms`, `stage_attempts`, `cost_usd`) and land on the analysis row.

Tracing is off by default and must stay that way outside local debugging. The
state flowing through these nodes carries retrieved excerpts from the client's
confidential corpus; switching this on sends them to a third party.
"""

from __future__ import annotations

import logging
import os

from vmlab.config import get_settings

logger = logging.getLogger(__name__)

_TRACING_VARS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def configure_tracing() -> bool:
    """Export LangSmith configuration to the environment. Returns whether on.

    Safe to call more than once, and safe to call when tracing is disabled --
    in which case it actively clears the flag, so a stale shell export cannot
    quietly turn tracing on for a process whose configuration says otherwise.
    """
    settings = get_settings()

    if not settings.langsmith_tracing:
        for var in _TRACING_VARS:
            os.environ.pop(var, None)
        return False

    if not settings.langsmith_api_key:
        # Wrong in a way worth saying out loud: tracing was asked for and will
        # not happen. The alternative is a silent no-op and an empty project.
        logger.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is empty; not tracing")
        for var in _TRACING_VARS:
            os.environ.pop(var, None)
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project

    logger.info("langsmith tracing enabled, project %s", settings.langsmith_project)
    return True
