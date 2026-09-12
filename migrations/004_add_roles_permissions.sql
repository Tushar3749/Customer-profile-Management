-- Migration: role-based permission system
-- Responsibility: adds dcm.dbo.roles / dcm.dbo.permissions /
-- dcm.dbo.role_permissions, and a nullable role_id FK on dcm.dbo.users.
-- EXISTING dcm.dbo.users columns are untouched — role_id is additive only,
-- so every existing row (all currently NULL here) keeps authenticating
-- exactly as before; the backend treats a NULL role_id as the 'agent'
-- role's permission set (see app/services/roles.py resolve_role()).
-- Run manually against the dcm database.

CREATE TABLE dcm.dbo.roles (
  id            INT IDENTITY(1,1) PRIMARY KEY,
  role_name     NVARCHAR(50) NOT NULL,
  description   NVARCHAR(255) NULL,
  created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  CONSTRAINT UQ_roles_role_name UNIQUE (role_name)
);

CREATE TABLE dcm.dbo.permissions (
  id              INT IDENTITY(1,1) PRIMARY KEY,
  permission_key  NVARCHAR(50) NOT NULL,
  description     NVARCHAR(255) NULL,
  CONSTRAINT UQ_permissions_permission_key UNIQUE (permission_key)
);

CREATE TABLE dcm.dbo.role_permissions (
  role_id       INT NOT NULL,
  permission_id INT NOT NULL,
  CONSTRAINT PK_role_permissions PRIMARY KEY (role_id, permission_id),
  CONSTRAINT FK_role_permissions_role FOREIGN KEY (role_id) REFERENCES dcm.dbo.roles(id),
  CONSTRAINT FK_role_permissions_permission FOREIGN KEY (permission_id) REFERENCES dcm.dbo.permissions(id)
);

ALTER TABLE dcm.dbo.users ADD role_id INT NULL;
ALTER TABLE dcm.dbo.users ADD CONSTRAINT FK_users_role FOREIGN KEY (role_id) REFERENCES dcm.dbo.roles(id);

-- Seed roles. 'supervisor' is named in the spec's example role list but
-- given no explicit permission set of its own — defaulted here to
-- view_crm only (same as agent), the safest minimal grant; adjust via the
-- Super Admin role-change UI (structure only, not full CRUD) if wrong.
INSERT INTO dcm.dbo.roles (role_name, description) VALUES
  ('super_admin', N'Full access to every MIS category plus user management'),
  ('manager', N'Sales, Inventory, and CRM access'),
  ('supervisor', N'CRM access — default grant, adjust as needed'),
  ('agent', N'CRM access only — the default role for every call-center agent');

INSERT INTO dcm.dbo.permissions (permission_key, description) VALUES
  ('view_crm', N'View the CRM category (Customer 360 report)'),
  ('view_sales', N'View the Sales category'),
  ('view_inventory', N'View the Inventory category'),
  ('view_finance', N'View the Finance category'),
  ('manage_users', N'View/edit users and their assigned role');

-- super_admin: every permission
INSERT INTO dcm.dbo.role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM dcm.dbo.roles r CROSS JOIN dcm.dbo.permissions p
WHERE r.role_name = 'super_admin';

-- agent, supervisor: view_crm only
INSERT INTO dcm.dbo.role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM dcm.dbo.roles r
JOIN dcm.dbo.permissions p ON p.permission_key = 'view_crm'
WHERE r.role_name IN ('agent', 'supervisor');

-- manager: view_crm + view_sales + view_inventory
INSERT INTO dcm.dbo.role_permissions (role_id, permission_id)
SELECT r.id, p.id FROM dcm.dbo.roles r
JOIN dcm.dbo.permissions p ON p.permission_key IN ('view_crm', 'view_sales', 'view_inventory')
WHERE r.role_name = 'manager';
