"""
require_permission(key) — permission-gate dependency.

Responsibility:
- Add a permission check ON TOP OF app.auth.get_current_agent(), never
  instead of it. Every route using require_permission() still runs the
  exact same bearer-token/is_active check get_current_agent() already
  performs for every other endpoint in this app — this module only adds
  an additional role-permission check after that succeeds, via FastAPI's
  own Depends() chaining (Depends(get_current_agent) is a sub-dependency
  of the callable this factory returns).
"""

from fastapi import Depends, HTTPException, status

from app.auth import get_current_agent
from app.db import fetch_one
from app.services.roles import get_permissions_for_role


def require_permission(permission_key: str):
    def _dependency(agent: dict = Depends(get_current_agent)) -> dict:
        row = fetch_one("SELECT role_id FROM dcm.dbo.users WHERE id = ?", (agent['id'],))
        permissions = get_permissions_for_role(row['role_id'] if row else None)
        if permission_key not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission_key}",
            )
        return agent
    return _dependency
