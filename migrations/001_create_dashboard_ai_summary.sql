-- Migration: create dcm.dbo.dashboard_ai_summary
-- Responsibility: one-time setup script for the only new table this
-- project introduces (base GhorerBazar tables stay untouched).
-- Run manually against the dcm database before starting the background job.

CREATE TABLE dcm.dbo.dashboard_ai_summary (
  id                  INT IDENTITY(1,1) PRIMARY KEY,
  customer_phone       NVARCHAR(50) NOT NULL,
  notes_count          INT NOT NULL DEFAULT 0,
  notes_source_hash    VARBINARY(32) NOT NULL,
  summary_short_bn      NVARCHAR(MAX),
  summary_short_en      NVARCHAR(MAX),
  summary_full_bn       NVARCHAR(MAX),   -- JSON array, stored as text
  summary_full_en       NVARCHAR(MAX),
  call_pattern_bn       NVARCHAR(MAX),
  call_pattern_en       NVARCHAR(MAX),
  consent_signal        NVARCHAR(20),     -- given | do_not_call | unclear
  interested_in_bn      NVARCHAR(MAX),    -- JSON array, stored as text
  interested_in_en      NVARCHAR(MAX),
  generated_at          DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
  CONSTRAINT UQ_dashboard_ai_summary_phone UNIQUE (customer_phone)
);

CREATE NONCLUSTERED INDEX IX_dashboard_ai_summary_phone
  ON dcm.dbo.dashboard_ai_summary (customer_phone);
