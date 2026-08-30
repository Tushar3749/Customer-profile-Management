"""
Authentication / session verification.

Responsibility:
- Reuse the existing dcm.users + current_session_token pattern already
  used by other GhorerBazar internal tools.
- Provide a dependency (e.g. get_current_agent) that routers can use to
  reject unauthenticated requests and identify the acting employee_id
  for write actions.

CONFIRMED 2026-08-12 — dcm.users schema:
  id (int), employee_id (nvarchar), password_hash (nvarchar), email
  (nvarchar), name (nvarchar), user_role (nvarchar), is_active (bit),
  reset_otp (nvarchar), reset_otp_expiration (datetime2), created_at
  (datetime2), updated_at (datetime2), profilePicture (nvarchar),
  current_session_token (nvarchar), last_login_at (datetime2)

Token transport: "Authorization: Bearer <token>" header. Verification
is a straight lookup of current_session_token — no login endpoint is
built here; the existing GhorerBazar system issues/rotates the token
elsewhere. is_active=0 is rejected. There is no expiry column
(reset_otp_expiration is OTP-only), so no expiry check is performed —
see the comment at the query below.

id (int) — not employee_id (nvarchar) — is what gets stored as
performed_by_id / target_agent_id in agent_assignment_history and
matches the int assigned_agent_id/previous_agent_id columns in
dcm.customer_calls.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import fetch_one

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    # No token expiry column in schema — relies on token being
    # cleared/rotated elsewhere on logout.
    agent = fetch_one(
        """
        SELECT id, employee_id, name, user_role, is_active
        FROM dcm.dbo.users
        WHERE current_session_token = ?
        """,
        (credentials.credentials,),
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    if not agent['is_active']:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is deactivated")
    return agent
