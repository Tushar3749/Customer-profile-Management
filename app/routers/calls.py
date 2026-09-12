"""
POST /api/customer-calls/{id}/update.

Responsibility:
- Update the given row in dcm.customer_calls (status, remark, and
  optionally assigned_agent_id for reassignment).
- Insert a corresponding audit row into dcm.agent_assignment_history,
  following the existing action/previous_status/new_status/remark/
  performed_by_id/target_agent_id pattern.

CONFIRMED 2026-08-12 — dcm.customer_calls schema:
  id (int), customer_name (nvarchar), customer_number (nvarchar),
  last_order_date (date), last_order_number (nvarchar), status
  (nvarchar), assigned_agent_id (int), remark (nvarchar), tag
  (nvarchar), created_at (datetime2), updated_at (datetime2),
  assigned_team_id (int), dataSourceId (int), fileName (nvarchar),
  isSubscriber (bit), last_data_status (nvarchar), assign_date
  (nvarchar), delivery_address (nvarchar), order_qty (int),
  previous_agent_id (int), lastDistributedAt (datetime2),
  lastAssignToAgent (datetime2)

Only status/remark/assigned_agent_id/next_followup_date are editable
here. previous_agent_id exists specifically to track reassignment, so a
body.assigned_agent_id that differs from the current value is treated
as a real reassignment (action='reassigned' in the audit row) rather
than a plain status update (action='status_changed').

CONFIRMED 2026-08-12 — dcm.agent_assignment_history schema:
  id (int), customer_call_id (int), agent_id (int), action (nvarchar),
  previous_status (nvarchar), new_status (nvarchar), remark (nvarchar),
  created_at (datetime2), performed_by_id (int), target_agent_id (int),
  previous_agent_id (int), source_file (nvarchar), metadata (nvarchar)

agent_id and performed_by_id are both set to the current logged-in
agent's id (both required by schema, same value in this flow).
source_file/metadata are left NULL — they belong to other ingestion
contexts, not this endpoint.

next_followup_date: dcm.customer_calls gained this column via
migrations/003_add_next_followup_date.sql — no longer a gap.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_agent
from app.db import execute, execute_returning_identity, fetch_one
from app.models.schemas import CallUpdateRequest, CallUpdateResponse

router = APIRouter(tags=["calls"])


@router.post("/customer-calls/{call_id}/update", response_model=CallUpdateResponse)
def update_customer_call(
    call_id: int,
    body: CallUpdateRequest,
    agent: dict = Depends(get_current_agent),
):
    existing = fetch_one(
        "SELECT id, status, assigned_agent_id FROM dcm.dbo.customer_calls WHERE id = ?",
        (call_id,),
    )
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="customer_calls row not found")

    previous_status = existing['status']
    previous_agent_id = existing['assigned_agent_id']
    is_reassignment = (
        body.assigned_agent_id is not None
        and body.assigned_agent_id != previous_agent_id
    )
    new_agent_id = body.assigned_agent_id if is_reassignment else previous_agent_id

    if is_reassignment:
        execute(
            """
            UPDATE dcm.dbo.customer_calls
            SET status = ?, remark = ?, assigned_agent_id = ?,
                previous_agent_id = ?, next_followup_date = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (
                body.status, body.remark, new_agent_id, previous_agent_id,
                body.next_followup_date, call_id,
            ),
        )
    else:
        execute(
            """
            UPDATE dcm.dbo.customer_calls
            SET status = ?, remark = ?, next_followup_date = ?,
                updated_at = SYSUTCDATETIME()
            WHERE id = ?
            """,
            (body.status, body.remark, body.next_followup_date, call_id),
        )

    action = 'reassigned' if is_reassignment else 'status_changed'

    execute_returning_identity(
        """
        INSERT INTO dcm.dbo.agent_assignment_history
            (customer_call_id, agent_id, action, previous_status, new_status,
             remark, performed_by_id, target_agent_id, previous_agent_id,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
        """,
        (
            call_id, agent['id'], action, previous_status, body.status,
            body.remark, agent['id'], new_agent_id,
            previous_agent_id if is_reassignment else None,
        ),
    )

    return CallUpdateResponse(
        customer_call_id=call_id,
        status=body.status,
        remark=body.remark,
        previous_status=previous_status,
        assigned_agent_id=new_agent_id,
        previous_agent_id=previous_agent_id if is_reassignment else None,
        next_followup_date=body.next_followup_date,
        performed_by_id=agent['id'],
        updated_at=datetime.now(timezone.utc),
    )
