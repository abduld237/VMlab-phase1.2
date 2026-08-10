"""Upload, analysis and feedback endpoints.

Every handler reaches the database through the RLS-scoped session, so tenant
scoping is not re-implemented here. A row that belongs to another tenant is not
forbidden -- it is invisible, and the handler returns 404 because it found
nothing. That is deliberate: a 403 would confirm the id exists.
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["analysis"])


class UploadResponse(BaseModel):
    upload_id: str
    width: int
    height: int
    quality_flags: list[str]
    low_confidence: bool


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
