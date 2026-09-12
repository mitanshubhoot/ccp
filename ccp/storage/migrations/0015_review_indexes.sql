-- Review of phases 13 to 17: the indexes and constraints the new tables were missing.
-- policy_drafts is read by control (ccp policy drafts) and joined to its report and its
-- approval; copilot_answers documents "one row per turn", which is now enforced.

CREATE INDEX IF NOT EXISTS ix_policy_drafts_control
  ON policy_drafts (tenant_id, control_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_policy_drafts_report ON policy_drafts (based_on_report_id);
ALTER TABLE policy_drafts
  ADD CONSTRAINT fk_policy_drafts_approval FOREIGN KEY (approval_id) REFERENCES approvals (approval_id);

DROP INDEX IF EXISTS ix_copilot_answers_thread;
CREATE UNIQUE INDEX ix_copilot_answers_thread ON copilot_answers (tenant_id, thread_id, turn);
