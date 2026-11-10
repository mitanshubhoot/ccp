-- NT10: reviewer decisions kept as labels (spec.md A-100). Insert-only. Every approved or
-- rejected approval item records what the model or the system proposed (the item's payload),
-- what the reviewer decided, and the consent scope in force when it was decided:
--   none      kept for audit only; no eval or training use
--   eval      may become a labelled eval case
--   training  may also be used to improve a model
-- The scope is the tenant's setting at the time (TenantSettings.label_consent, default none);
-- a tenant admin opts in. A later change of setting never rewrites an earlier label.

CREATE TABLE IF NOT EXISTS review_labels (
  label_id        TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  approval_id     TEXT NOT NULL,
  kind            TEXT NOT NULL,                   -- the approval item's kind
  ref_id          TEXT NOT NULL,                   -- the thread, proposal, or draft it was about
  proposed        JSONB NOT NULL,                  -- the item's payload as the reviewer saw it
  decision        JSONB NOT NULL,                  -- the reviewer's decision
  reviewer        TEXT NOT NULL CHECK (reviewer <> ''),
  consent_scope   TEXT NOT NULL CHECK (consent_scope IN ('none', 'eval', 'training')),
  recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, approval_id)
);
CREATE INDEX IF NOT EXISTS ix_review_labels_scope ON review_labels (tenant_id, consent_scope);

GRANT SELECT, INSERT ON review_labels TO {{app_role}};
REVOKE UPDATE, DELETE ON review_labels FROM {{app_role}};
ALTER TABLE review_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_labels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON review_labels
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
