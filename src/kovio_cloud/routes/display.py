"""Public, unauthenticated screen-facing endpoint for OEM custom displays.

``GET /display/v1/{code}`` returns the active playlist for a custom display so a
robot screen (or the web ``/display/<code>`` player) can render it. Read-only, no
auth — the code is an unguessable slug. Paused or unknown displays return 404 so
a screen pointed at one simply goes blank.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import CustomDisplay, CustomDisplayItem

router = APIRouter(prefix="/display/v1", tags=["display"])


@router.get("/{code}")
async def get_display(code: str, session: AsyncSession = Depends(get_session)):
    d = (
        await session.execute(select(CustomDisplay).where(CustomDisplay.code == code))
    ).scalar_one_or_none()
    if d is None or d.status != "active":
        return JSONResponse(
            status_code=404, content={"code": "not_found", "detail": "display not found"}
        )
    items = (
        await session.execute(
            select(CustomDisplayItem)
            .where(CustomDisplayItem.display_id == d.id)
            .order_by(CustomDisplayItem.position)
        )
    ).scalars().all()
    return {
        "code": d.code,
        "name": d.name,
        "default_image_seconds": d.default_image_seconds,
        "items": [
            {
                "media_url": it.media_url,
                "media_type": it.media_type,
                "duration_seconds": it.duration_seconds,
            }
            for it in items
        ],
    }
