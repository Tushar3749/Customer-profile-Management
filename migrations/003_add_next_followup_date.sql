-- Migration: add next_followup_date to dcm.dbo.customer_calls
-- Responsibility: gives the Action Panel's "next follow-up date" field
-- somewhere to persist (frontend prototype had this input with no
-- backing column until now).

ALTER TABLE dcm.dbo.customer_calls ADD next_followup_date DATE NULL;
