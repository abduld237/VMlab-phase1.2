"""Upload, analysis and feedback endpoints.

Every handler reaches the database through the RLS-scoped session, so tenant
scoping is not re-implemented here. A row that belongs to another tenant is not
forbidden -- it is invisible, and the handler returns 404 because it found
nothing. That is deliberate: a 403 would confirm the id exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from vmlab import storage as storage_module
from vmlab.api.deps import CurrentUser, current_user, db, not_found
from vmlab.config import get_settings
from vmlab.graph.nodes.validate import ImageRejected, validate_and_normalise
from vmlab.graph.pipeline import run_analysis
from vmlab.graph.schemas import AnalysisResult
from vmlab.models.openrouter import OpenRouterClient
from vmlab.retrieval.retriever import Retriever
from vmlab.tenancy.session import service_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["analysis"])


class PerQueryRetriever:
    """Retrieves on a fresh connection per query rather than holding one open.

    The graph spends minutes inside model calls. A pooled connection held across
    that goes idle long enough for Supabase's pooler to drop it, and psycopg
    does not notice: the next query then waits forever on a socket whose reply
    will never come. A live analysis hung for thirteen minutes that way, past a
    180-second timeout that could not fire because the coroutine was parked
    below it.

    Knowledge base chunks are universal rather than tenant-owned, so this runs
    under the service role; tenant_id still scopes any tenant-private knowledge.
    """

    def __init__(self, tenant_id: str | None = None):
        self.tenant_id = tenant_id

    async def search(self, **kwargs):
        kwargs.setdefault("tenant_id", self.tenant_id)
        async with service_session() as connection:
            return await Retriever(connection).search(**kwargs)


class UploadResponse(BaseModel):
    upload_id: str
    width: int
    height: int
    quality_flags: list[str]
    low_confidence: bool


class AnalysisRequest(BaseModel):
    upload_id: uuid.UUID


class BrandRequest(BaseModel):
    brand_name: str | None = Field(default=None, max_length=200)
    tone_of_voice: str | None = Field(default=None, max_length=2000)
    guidelines: str | None = Field(default=None, max_length=8000)
    colours: list[str] = Field(default_factory=list, max_length=20)
    fonts: list[str] = Field(default_factory=list, max_length=10)
    categories: list[str] = Field(default_factory=list, max_length=50)


class FeedbackRequest(BaseModel):
    verdict: str = Field(pattern="^(useful|partly_useful|not_useful)$")
    perspective: str | None = Field(default=None, pattern="^(creative_vm|retail_psychology|commercial)$")
    item_index: int | None = Field(default=None, ge=0)
    action_rank: int | None = Field(default=None, ge=1, le=3)
    correction: str | None = None


async def _audit(
    connection: AsyncConnection,
    user: CurrentUser,
    action: str,
    resource_type: str,
    resource_id: str,
) -> None:
    await connection.execute(
        """
        insert into public.audit_log
            (tenant_id, actor_id, action, resource_type, resource_id)
        values (%s, %s, %s, %s, %s)
        """,
        (user.tenant_id, user.user_id, action, resource_type, resource_id),
    )


@router.post("/uploads", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def create_upload(
    file: UploadFile = File(...),
    display_type: str | None = Form(default=None),
    campaign_objective: str | None = Form(default=None),
    hero_product: str | None = Form(default=None),
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> UploadResponse:
    """Validate, normalise and store a display image (FR-01..FR-05)."""
    settings = get_settings()
    raw = await file.read()

    try:
        validated = validate_and_normalise(
            raw,
            long_edge=settings.image_long_edge_px,
            max_bytes=settings.max_upload_bytes,
        )
    except ImageRejected as exc:
        # A rejected image never becomes a row. The message is written for the
        # user, not the log -- it tells them what to do differently.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # Path is derived from the session's tenant, never from the request.
    path = storage_module.build_path(user.tenant_id)
    store = storage_module.for_settings(settings)
    await store.put(path, validated.data, validated.mime_type)

    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            insert into public.uploads
                (tenant_id, uploaded_by, storage_path, original_name, mime_type,
                 byte_size, width, height, display_type, campaign_objective,
                 hero_product, quality_flags, low_confidence)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            returning id
            """,
            (
                user.tenant_id, user.user_id, path, file.filename,
                validated.mime_type, len(validated.data), validated.width,
                validated.height, display_type, campaign_objective, hero_product,
                json.dumps(validated.quality_flags), validated.low_confidence,
            ),
        )
        upload_id = str((await cursor.fetchone())["id"])

    await _audit(connection, user, "upload.created", "upload", upload_id)

    return UploadResponse(
        upload_id=upload_id,
        width=validated.width,
        height=validated.height,
        quality_flags=validated.quality_flags,
        low_confidence=validated.low_confidence,
    )


@router.post("/analyses", status_code=status.HTTP_202_ACCEPTED)
async def create_analysis(
    body: AnalysisRequest,
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> dict:
    """Start an analysis and return immediately with its id.

    A real analysis takes minutes, which is far too long to hold an HTTP
    request open: proxies and browsers time out well before it finishes, and a
    dropped connection would abandon work already paid for. So the row is
    created here, the graph runs detached, and the client polls
    GET /analyses/{id} -- which is what lets the UI show named progress steps
    instead of a spinner.

    No job queue: Phase 1 does not need the infrastructure, and each analysis is
    self-contained. The cost is that a process restart mid-run strands a row in
    a running state, which the stale-analysis sweep below resolves on read.
    """
    async with connection.cursor(row_factory=dict_row) as cursor:
        # RLS makes another tenant's upload invisible, so this doubles as the
        # authorisation check.
        await cursor.execute(
            """
            select id, storage_path, mime_type, display_type, campaign_objective,
                   hero_product, quality_flags
            from public.uploads
            where id = %s
            """,
            (body.upload_id,),
        )
        upload = await cursor.fetchone()
        if upload is None:
            raise not_found()

        brand = await _brand_context(connection, user)

    # Written on its own connection so it is committed before this handler
    # returns. On the request's connection it would stay uncommitted until the
    # dependency closes the transaction -- and the detached task below would
    # race that commit, updating a row it cannot yet see.
    async with service_session() as own:
        async with own.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                insert into public.analyses (tenant_id, upload_id, requested_by, status)
                values (%s, %s, %s, 'validating')
                returning id
                """,
                (user.tenant_id, upload["id"], user.user_id),
            )
            analysis_id = str((await cursor.fetchone())["id"])

    await _audit(connection, user, "analysis.started", "analysis", analysis_id)

    quality_flags = upload["quality_flags"] or []
    if isinstance(quality_flags, str):
        quality_flags = json.loads(quality_flags)

    # Detached from the request. Held in a module-level set because asyncio only
    # keeps a weak reference to a bare task, and a garbage-collected task
    # cancels the analysis silently mid-run.
    task = asyncio.create_task(
        _execute_analysis(
            analysis_id=analysis_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            storage_path=upload["storage_path"],
            mime_type=upload["mime_type"],
            display_type=upload["display_type"],
            campaign_objective=upload["campaign_objective"],
            hero_product=upload["hero_product"],
            quality_flags=quality_flags,
            brand=brand,
        )
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)

    return {"id": analysis_id, "status": "validating"}


# Strong references to in-flight analyses; see the note in create_analysis.
_RUNNING: set[asyncio.Task] = set()


async def _execute_analysis(
    *,
    analysis_id: str,
    tenant_id: str,
    user_id: str,
    storage_path: str,
    mime_type: str,
    display_type: str | None,
    campaign_objective: str | None,
    hero_product: str | None,
    quality_flags: list[str],
    brand: dict | None,
) -> None:
    """Run the graph outside the request and record the outcome either way.

    Opens its own sessions: the request's connection is long gone by the time
    this runs, and each write takes a fresh one so nothing sits idle across the
    minutes of model calls in between.
    """
    settings = get_settings()
    user = CurrentUser(user_id=user_id, email=None, tenant_id=tenant_id)

    async def report(stage: str) -> None:
        async with service_session() as connection:
            await connection.execute(
                "update public.analyses set status = %s where id = %s",
                (stage, analysis_id),
            )

    try:
        store = storage_module.for_settings(settings)
        image_bytes = await store.get(storage_path)

        async with OpenRouterClient() as client:
            result, telemetry = await run_analysis(
                client=client,
                retriever=PerQueryRetriever(tenant_id),
                image_bytes=image_bytes,
                mime_type=mime_type,
                tenant_id=tenant_id,
                quality_flags=quality_flags,
                display_type=display_type,
                campaign_objective=campaign_objective,
                hero_product=hero_product,
                brand_context=brand,
                on_stage=report,
            )
    except Exception as exc:  # noqa: BLE001 - the row must record the failure
        logger.exception("analysis %s failed", analysis_id)
        try:
            async with service_session() as connection:
                await connection.execute(
                    """
                    update public.analyses
                    set status = 'failed', error_detail = %s, completed_at = now()
                    where id = %s
                    """,
                    (_failure_detail(exc), analysis_id),
                )
        except Exception:  # noqa: BLE001
            logger.exception("could not mark analysis %s failed", analysis_id)
        return

    async with service_session() as connection:
        await _persist_result(connection, user, analysis_id, result, telemetry)
        await _audit(connection, user, "analysis.completed", "analysis", analysis_id)


def _failure_detail(exc: Exception) -> str:
    """A message worth showing, even when the exception carries none.

    asyncio.TimeoutError stringifies to the empty string, so the obvious
    str(exc) wrote a blank reason into the row and the UI showed a failure with
    no explanation. The exception type is the information in that case.
    """
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return (
            "The analysis exceeded the time limit before it could finish. "
            "Please try again."
        )
    return (str(exc) or exc.__class__.__name__)[:500]


async def _brand_context(connection: AsyncConnection, user: CurrentUser) -> dict | None:
    """The tenant's brand identity, so specialists judge against its own voice."""
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            select brand_name, tone_of_voice, colours, fonts,
                   guidelines, categories
            from public.brand_identity
            where tenant_id = %s
            """,
            (user.tenant_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {key: value for key, value in row.items() if value}


async def _persist_result(
    connection: AsyncConnection,
    user: CurrentUser,
    analysis_id: str,
    result: AnalysisResult,
    telemetry: dict,
) -> None:
    """Write the analysis, its sections and its ranked actions in one go."""
    chunk_ids = sorted({ref for refs in telemetry.get("evidence_refs", {}).values() for ref in refs})

    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            update public.analyses
            set status = 'complete',
                visual_evidence = %s,
                overall_summary = %s,
                uncertainty_note = %s,
                retrieved_chunk_ids = %s,
                model_versions = %s,
                stage_timings_ms = %s,
                total_tokens = %s,
                cost_usd = %s,
                completed_at = now()
            where id = %s
            """,
            (
                result.evidence.model_dump_json(),
                result.synthesis.overall_summary,
                result.synthesis.uncertainty_note,
                chunk_ids,
                json.dumps(telemetry.get("model_versions", {})),
                json.dumps(telemetry.get("stage_timings_ms", {})),
                telemetry.get("prompt_tokens", 0) + telemetry.get("completion_tokens", 0),
                telemetry.get("cost_usd", 0.0),
                analysis_id,
            ),
        )

        # A perspective that failed writes no section at all, rather than an
        # empty one that would read as "we looked and found nothing".
        for finding in result.findings:
            await cursor.execute(
                """
                insert into public.analysis_sections
                    (tenant_id, analysis_id, perspective, items, evidence_refs)
                values (%s, %s, %s, %s, %s)
                on conflict (analysis_id, perspective) do update
                set items = excluded.items, evidence_refs = excluded.evidence_refs
                """,
                (
                    user.tenant_id, analysis_id, finding.perspective.value,
                    json.dumps([item.model_dump() for item in finding.items]),
                    json.dumps(
                        (telemetry.get("evidence_refs") or {}).get(finding.perspective.value, [])
                    ),
                ),
            )

        for action in result.synthesis.actions:
            await cursor.execute(
                """
                insert into public.analysis_actions
                    (tenant_id, analysis_id, rank, action, rationale, effort,
                     confidence, priority_score, priority_band, trade_off, evidence_refs)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (analysis_id, rank) do update
                set action = excluded.action, rationale = excluded.rationale
                """,
                (
                    user.tenant_id, analysis_id, action.rank, action.action,
                    action.rationale, action.effort.value, action.confidence,
                    action.priority_score, _priority_band(action.priority_score),
                    action.trade_off,
                    json.dumps([p.value for p in action.contributing_perspectives]),
                ),
            )


def _priority_band(score: int) -> str:
    """Document 26's P1-P4 banding over the 0-100 priority score."""
    if score >= 80:
        return "P1"
    if score >= 60:
        return "P2"
    if score >= 40:
        return "P3"
    return "P4"


@router.get("/analyses")
async def list_analyses(
    limit: int = 20,
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> list[dict]:
    """Recent analyses for this workspace (FR-13)."""
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            select a.id, a.status::text as status, a.overall_summary,
                   a.created_at, a.completed_at, u.storage_path, u.low_confidence
            from public.analyses a
            join public.uploads u on u.id = a.upload_id
            order by a.created_at desc
            limit %s
            """,
            (min(limit, 100),),
        )
        rows = await cursor.fetchall()
    return [{**row, "id": str(row["id"])} for row in rows]


@router.get("/analyses/{analysis_id}")
async def get_analysis(
    analysis_id: uuid.UUID,
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> dict:
    """One analysis with its sections and prioritised actions."""
    async with connection.cursor(row_factory=dict_row) as cursor:
        # A process restart kills detached analyses without marking them, and a
        # row left mid-flight would poll forever. Anything past the ceiling with
        # no result is dead, so say so instead of spinning.
        await cursor.execute(
            """
            update public.analyses
            set status = 'failed',
                error_detail = 'The analysis stopped before completing.',
                completed_at = now()
            where id = %s
              and status not in ('complete', 'failed')
              and created_at < now() - make_interval(secs => %s)
            """,
            (analysis_id, get_settings().analysis_timeout_seconds + 120),
        )

        await cursor.execute(
            """
            select id, status::text as status, visual_evidence, overall_summary,
                   uncertainty_note, stage_timings_ms, model_versions, cost_usd,
                   error_detail, created_at, completed_at
            from public.analyses
            where id = %s
            """,
            (analysis_id,),
        )
        analysis = await cursor.fetchone()
        # RLS already filtered another tenant's row out, so "not found" and
        # "not yours" are genuinely the same case here.
        if analysis is None:
            raise not_found()

        await cursor.execute(
            """
            select perspective::text as perspective, items, evidence_refs
            from public.analysis_sections
            where analysis_id = %s
            order by perspective
            """,
            (analysis_id,),
        )
        sections = await cursor.fetchall()

        await cursor.execute(
            """
            select rank, action, rationale, effort::text as effort, confidence,
                   priority_score, priority_band, trade_off, evidence_refs
            from public.analysis_actions
            where analysis_id = %s
            order by rank
            """,
            (analysis_id,),
        )
        actions = await cursor.fetchall()

    return {
        **analysis,
        "id": str(analysis["id"]),
        "sections": sections,
        "actions": actions,
    }


@router.post("/analyses/{analysis_id}/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    analysis_id: uuid.UUID,
    body: FeedbackRequest,
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> dict:
    """Rate a recommendation (FR-12)."""
    async with connection.cursor(row_factory=dict_row) as cursor:
        # Confirm the analysis is visible to this session before writing. RLS
        # would reject the insert anyway, but a foreign key violation is a poor
        # way to tell a user their analysis does not exist.
        await cursor.execute("select 1 from public.analyses where id = %s", (analysis_id,))
        if await cursor.fetchone() is None:
            raise not_found()

        await cursor.execute(
            """
            insert into public.feedback
                (tenant_id, analysis_id, perspective, item_index, action_rank,
                 verdict, correction, submitted_by)
            values (%s,%s,%s,%s,%s,%s,%s,%s)
            returning id
            """,
            (
                user.tenant_id, analysis_id, body.perspective, body.item_index,
                body.action_rank, body.verdict, body.correction, user.user_id,
            ),
        )
        feedback_id = str((await cursor.fetchone())["id"])

    await _audit(connection, user, "feedback.submitted", "analysis", str(analysis_id))
    return {"feedback_id": feedback_id}


@router.put("/brand")
async def update_brand(
    body: BrandRequest,
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> dict:
    """Update this workspace's brand profile (FR-17, administrators only).

    Authorisation is the database's, not this handler's: the
    brand_identity_admin_write policy requires is_tenant_admin(), so a standard
    user's update matches zero rows. That returning nothing is how we detect the
    refusal -- there is no role check here that could drift out of step with the
    policy.
    """
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            update public.brand_identity
            set brand_name = %s, tone_of_voice = %s, guidelines = %s,
                colours = %s, fonts = %s, categories = %s, updated_at = now()
            where tenant_id = %s
            returning brand_name
            """,
            (
                body.brand_name, body.tone_of_voice, body.guidelines,
                json.dumps(body.colours), json.dumps(body.fonts),
                json.dumps(body.categories), user.tenant_id,
            ),
        )
        updated = await cursor.fetchone()

    if updated is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a workspace administrator can change the brand profile.",
        )

    await _audit(connection, user, "brand.updated", "brand_identity", user.tenant_id)
    return {"brand_name": updated["brand_name"]}


@router.get("/brand")
async def get_brand(
    user: CurrentUser = Depends(current_user),
    connection: AsyncConnection = Depends(db),
) -> dict:
    """This workspace's brand profile (FR-17)."""
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            select brand_name, logo_path, colours, fonts, tone_of_voice,
                   guidelines, categories
            from public.brand_identity
            """
        )
        row = await cursor.fetchone()
    return row or {}
