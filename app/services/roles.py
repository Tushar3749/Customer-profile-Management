"""
Role/permission lookups for dcm.dbo.roles / permissions / role_permissions.

Responsibility:
- Resolve a user's role_id (which may be NULL — see below) to a role row
  and its permission_key list.

CONFIRMED 2026-09-01 — dcm.dbo.users.role_id is a new NULLable FK added by
migrations/004_add_roles_permissions.sql; every pre-existing user row has
role_id=NULL (the migration doesn't touch any existing column). Rather than
treat that as an error, resolve_role()/get_permissions_for_role() fall back
to the 'agent' role whenever role_id is NULL or doesn't match a real row —
so a legacy user keeps authenticating and gets the default (CRM-only)
permission set instead of being locked out or crashing the login/me
endpoints.
"""

from app.db import fetch_all, fetch_one

DEFAULT_ROLE_NAME = 'agent'


def resolve_role(role_id: int | None) -> dict:
    """Returns {'id': int|None, 'role_name': str}. Falls back to the
    'agent' role when role_id is NULL or stale (deleted role row)."""
    if role_id is not None:
        row = fetch_one("SELECT id, role_name FROM dcm.dbo.roles WHERE id = ?", (role_id,))
        if row:
            return row
    fallback = fetch_one("SELECT id, role_name FROM dcm.dbo.roles WHERE role_name = ?", (DEFAULT_ROLE_NAME,))
    return fallback or {'id': None, 'role_name': DEFAULT_ROLE_NAME}


def get_permissions_for_role(role_id: int | None) -> list[str]:
    role = resolve_role(role_id)
    if role['id'] is None:
        # 'agent' role row itself is missing (shouldn't happen post-seed) —
        # no permissions rather than a crash.
        return []
    rows = fetch_all(
        """
        SELECT p.permission_key
        FROM dcm.dbo.role_permissions rp
        JOIN dcm.dbo.permissions p ON p.id = rp.permission_id
        WHERE rp.role_id = ?
        """,
        (role['id'],),
    )
    return [r['permission_key'] for r in rows]


def get_all_roles() -> list[dict]:
    return fetch_all("SELECT id, role_name, description FROM dcm.dbo.roles ORDER BY id")
