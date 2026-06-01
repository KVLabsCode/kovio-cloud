"""``/oem/v1/*`` — fleet-operator endpoints. SKELETAL for now.

Reserves the URL namespace. The OEM web app (a separate Next.js project) is
built after the advertiser app.

TODO (next milestone):
  - POST /oem/v1/fleets                 — fleet onboarding
  - POST /oem/v1/fleets/{id}/robot-keys — mint robot/SDK API keys
  - PUT  /oem/v1/fleets/{id}/brand-safety — blocked categories / advertisers
  - GET  /oem/v1/revenue/summary        — pending + lifetime payouts
  - GET  /oem/v1/payouts                — payout history
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/oem/v1", tags=["oem"])


@router.get("/me")
async def oem_me():
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OEM API is wired up in the next milestone.",
    )
