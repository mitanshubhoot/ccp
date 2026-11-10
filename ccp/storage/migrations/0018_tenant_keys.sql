-- NT9: every reference stays inside one tenant (spec.md A-99).
-- Foreign-key and unique checks bypass row-level security, so a single-column reference let
-- a row point at another tenant's row, and a global cache key let one tenant's insert collide
-- with another's. Every tenant reference now carries the tenant, and both caches are keyed
-- by (tenant, key). Identifiers stay globally unique; (tenant_id, id) is the key referenced.

-- the composite keys that tenant references point at
ALTER TABLE event_log ADD CONSTRAINT ux_event_log_tenant UNIQUE (tenant_id, "offset");
ALTER TABLE verdicts ADD CONSTRAINT ux_verdicts_tenant UNIQUE (tenant_id, verdict_id);
ALTER TABLE compliance_state ADD CONSTRAINT ux_compliance_state_tenant UNIQUE (tenant_id, row_id);
ALTER TABLE resolution_decisions ADD CONSTRAINT ux_resolution_decisions_tenant
  UNIQUE (tenant_id, decision_id);
ALTER TABLE review_queue ADD CONSTRAINT ux_review_queue_tenant UNIQUE (tenant_id, decision_id);
ALTER TABLE approvals ADD CONSTRAINT ux_approvals_tenant UNIQUE (tenant_id, approval_id);
ALTER TABLE gap_reports ADD CONSTRAINT ux_gap_reports_tenant UNIQUE (tenant_id, report_id);

-- every single-column tenant reference becomes a (tenant_id, id) reference
ALTER TABLE dead_letter DROP CONSTRAINT dead_letter_offset_fkey,
  ADD CONSTRAINT fk_dead_letter_event
  FOREIGN KEY (tenant_id, "offset") REFERENCES event_log (tenant_id, "offset");
ALTER TABLE ordered_failures DROP CONSTRAINT ordered_failures_offset_fkey,
  ADD CONSTRAINT fk_ordered_failures_event
  FOREIGN KEY (tenant_id, "offset") REFERENCES event_log (tenant_id, "offset");
ALTER TABLE dead_letter_replays DROP CONSTRAINT dead_letter_replays_offset_fkey,
  ADD CONSTRAINT fk_dead_letter_replays_event
  FOREIGN KEY (tenant_id, "offset") REFERENCES event_log (tenant_id, "offset");
ALTER TABLE person_links DROP CONSTRAINT person_links_decision_id_fkey,
  ADD CONSTRAINT fk_person_links_decision
  FOREIGN KEY (tenant_id, decision_id) REFERENCES resolution_decisions (tenant_id, decision_id);
ALTER TABLE review_queue DROP CONSTRAINT review_queue_decision_id_fkey,
  ADD CONSTRAINT fk_review_queue_decision
  FOREIGN KEY (tenant_id, decision_id) REFERENCES resolution_decisions (tenant_id, decision_id);
ALTER TABLE review_resolutions DROP CONSTRAINT review_resolutions_decision_id_fkey,
  ADD CONSTRAINT fk_review_resolutions_queue
  FOREIGN KEY (tenant_id, decision_id) REFERENCES review_queue (tenant_id, decision_id);
ALTER TABLE compliance_state DROP CONSTRAINT compliance_state_verdict_id_fkey,
  ADD CONSTRAINT fk_compliance_state_verdict
  FOREIGN KEY (tenant_id, verdict_id) REFERENCES verdicts (tenant_id, verdict_id);
ALTER TABLE compliance_state DROP CONSTRAINT compliance_state_supersedes_row_id_fkey,
  ADD CONSTRAINT fk_compliance_state_supersedes
  FOREIGN KEY (tenant_id, supersedes_row_id) REFERENCES compliance_state (tenant_id, row_id);
ALTER TABLE remediation_proposals DROP CONSTRAINT remediation_proposals_verdict_id_fkey,
  ADD CONSTRAINT fk_remediation_proposals_verdict
  FOREIGN KEY (tenant_id, verdict_id) REFERENCES verdicts (tenant_id, verdict_id);
ALTER TABLE approval_events DROP CONSTRAINT approval_events_approval_id_fkey,
  ADD CONSTRAINT fk_approval_events_approval
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, approval_id);
ALTER TABLE investigations DROP CONSTRAINT investigations_verdict_id_fkey,
  ADD CONSTRAINT fk_investigations_verdict
  FOREIGN KEY (tenant_id, verdict_id) REFERENCES verdicts (tenant_id, verdict_id);
ALTER TABLE policy_drafts DROP CONSTRAINT fk_policy_drafts_approval,
  ADD CONSTRAINT fk_policy_drafts_approval
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, approval_id);
ALTER TABLE policy_drafts DROP CONSTRAINT policy_drafts_based_on_report_id_fkey,
  ADD CONSTRAINT fk_policy_drafts_report
  FOREIGN KEY (tenant_id, based_on_report_id) REFERENCES gap_reports (tenant_id, report_id);

-- the caches: one tenant's identical text no longer collides with another's
ALTER TABLE embedding_cache DROP CONSTRAINT embedding_cache_pkey,
  ADD PRIMARY KEY (tenant_id, cache_key);
ALTER TABLE enrichment_cache DROP CONSTRAINT enrichment_cache_pkey,
  ADD PRIMARY KEY (tenant_id, cache_key);
