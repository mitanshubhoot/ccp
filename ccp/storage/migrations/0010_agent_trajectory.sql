-- Phase 8: the node sequence a graph run took, recorded on its thread row for the trajectory
-- tests and the CLI's inspect view.
ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS trajectory JSONB NOT NULL DEFAULT '[]';
