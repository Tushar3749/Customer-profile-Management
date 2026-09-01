"""
GET /api/users, GET /api/roles, PATCH /api/users/{user_id}/role.

Responsibility:
- Basic User Management for the Super Admin MIS section — a user list
  with each row's resolved role, and a role-change action. Structure
  only (per spec: "pura feature na, structure-i thik ache") — no
  create/deactivate/password-reset here, just list + reassign role.

Every route is gated by require_permission('manage_users'), an ADDITIONAL
layer on top of app.auth.get_current_agent() (see app/permissions.py) —
not a replacement: the same bearer-token/is_active check every other
endpoint already runs still runs first.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.db import execute, fetch_all, fetch_one
from app.permissions import require_permission
from app.services.roles import get_all_roles, resolve_role

router = APIRouter(tags=["users"])


class UserListItem(BaseModel):
    id: int
    employee_id: str
    name: str
    role: str
    role_id: int | None
    is_active: bool


class RoleOption(BaseModel):
    id: int
    role_name: str


class RoleChangeRequest(BaseModel):
    role_id: int


@router.get("/users", response_model=list[UserListItem])
def list_users(agent: dict = Depends(require_permission('manage_users'))):
    rows = fetch_all("SELECT id, employee_id, name, role_id, is_active FROM dcm.dbo.users ORDER BY id")
    return [
        UserListItem(
            id=r['id'], employee_id=r['employee_id'] or '', name=r['name'],
            role=resolve_role(r['role_id'])['role_name'], role_id=r['role_id'], is_active=r['is_active'],
        )
        for r in rows
    ]


@router.get("/roles", response_model=list[RoleOption])
def list_roles(agent: dict = Depends(require_permission('manage_users'))):
    return [RoleOption(id=r['id'], role_name=r['role_name']) for r in get_all_roles()]


@router.patch("/users/{user_id}/role")
def change_user_role(user_id: int, body: RoleChangeRequest, agent: dict = Depends(require_permission('manage_users'))):
    role = fetch_one("SELECT id FROM dcm.dbo.roles WHERE id = ?", (body.role_id,))
    if role is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown role_id")
    execute("UPDATE dcm.dbo.users SET role_id = ? WHERE id = ?", (body.role_id, user_id))
    return {"ok": True}
