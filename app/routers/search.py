"""
GET /api/search/recent.

Responsibility:
- Return the current authenticated agent's recently searched phone
  numbers/customers.

CONFIRMED 2026-08-12 — backed by dcm.dbo.search_history (see
migrations/002_create_search_history.sql). A row is inserted as a
side effect of every GET /api/customer/{phone} call
(routers/customer.py). Returns the agent's last 10 distinct phone
numbers, most recently searched first.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_agent
from app.db import fetch_all
from app.models.schemas import RecentSearchItem

router = APIRouter(tags=["search"])


@router.get("/search/recent", response_model=list[RecentSearchItem])
def get_recent_searches(agent: dict = Depends(get_current_agent)):
    return fetch_all(
        """
        SELECT TOP 10 searched_phone AS phone, MAX(searched_at) AS searched_at
        FROM dcm.dbo.search_history
        WHERE agent_id = ?
        GROUP BY searched_phone
        ORDER BY MAX(searched_at) DESC
        """,
        (agent['id'],),
    )
