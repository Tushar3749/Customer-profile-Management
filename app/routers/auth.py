"""
POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me.

Responsibility:
- Real login: verify employee_id + password (bcrypt) against
  dcm.dbo.users.password_hash, issue a new current_session_token (UUID),
  return it plus the user's resolved role/permission list.
- Logout: clear current_session_token for the authenticated caller.
- /me: re-fetch role/permissions for an already-issued token — used on
  dashboard load (including a page refresh), since the token travels via
  a URL query param into the frontend's in-memory AUTH_TOKEN variable
  rather than persistent browser storage (see the frontend's login flow).

Does NOT touch app.auth.get_current_agent(): /login is the one endpoint
that deliberately has no Depends() on it (that's the whole point of
logging in — no token exists yet), while /logout and /me use it exactly
like every pre-existing endpoint in this app.
"""

import uuid

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import get_current_agent
from app.db import execute, fetch_one
from app.services.roles import get_permissions_for_role, resolve_role

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    employee_id: str
    password: str


class UserSummary(BaseModel):
    id: int
    employee_id: str
    name: str
    role: str
    permissions: list[str]


class LoginResponse(BaseModel):
    token: str
    user: UserSummary


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest):
    row = fetch_one(
        """
        SELECT id, employee_id, password_hash, name, role_id, is_active
        FROM dcm.dbo.users
        WHERE employee_id = ?
        """,
        (body.employee_id,),
    )
    # Same generic error for "no such employee_id" and "wrong password" —
    # doesn't tell a caller which one was wrong.
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid employee ID or password")
    if row is None:
        raise invalid
    if not bcrypt.checkpw(body.password.encode('utf-8'), row['password_hash'].encode('utf-8')):
        raise invalid
    if not row['is_active']:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is deactivated")

    token = str(uuid.uuid4())
    execute(
        "UPDATE dcm.dbo.users SET current_session_token = ?, last_login_at = SYSUTCDATETIME() WHERE id = ?",
        (token, row['id']),
    )
    role = resolve_role(row['role_id'])
    permissions = get_permissions_for_role(row['role_id'])
    return LoginResponse(
        token=token,
        user=UserSummary(
            id=row['id'], employee_id=row['employee_id'] or '', name=row['name'],
            role=role['role_name'], permissions=permissions,
        ),
    )


@router.post("/logout")
def logout(agent: dict = Depends(get_current_agent)):
    execute("UPDATE dcm.dbo.users SET current_session_token = NULL WHERE id = ?", (agent['id'],))
    return {"ok": True}


@router.get("/me", response_model=UserSummary)
def me(agent: dict = Depends(get_current_agent)):
    row = fetch_one("SELECT role_id FROM dcm.dbo.users WHERE id = ?", (agent['id'],))
    role_id = row['role_id'] if row else None
    role = resolve_role(role_id)
    permissions = get_permissions_for_role(role_id)
    return UserSummary(
        id=agent['id'], employee_id=agent['employee_id'] or '', name=agent['name'],
        role=role['role_name'], permissions=permissions,
    )
