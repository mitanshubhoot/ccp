-- MT2: a parallel claim is a lease (spec.md A-105).
--   lease_token       minted per claim; every status change after the claim presents it, so
--                     a worker that lost its lease cannot overwrite the record
--   lease_owner       host:pid of the worker holding it, for operators
--   lease_expires_at  the holder's heartbeat pushes it forward while it works; another
--                     worker re-claims the record only after it has passed
-- A claim made before this file has no lease and keeps its old visibility timeout.

ALTER TABLE event_log ADD COLUMN IF NOT EXISTS lease_token TEXT;
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
