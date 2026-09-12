-- Migration: create dcm.dbo.search_history
-- Responsibility: backs GET /api/search/recent. A row is inserted as a
-- side effect of every GET /api/customer/{phone} call.

CREATE TABLE dcm.dbo.search_history (
  id INT IDENTITY(1,1) PRIMARY KEY,
  agent_id INT NOT NULL,
  searched_phone NVARCHAR(50) NOT NULL,
  searched_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE NONCLUSTERED INDEX IX_search_history_agent
  ON dcm.dbo.search_history (agent_id, searched_at DESC);
