-- MT10 step 8: stratified judge samples and a monthly expert relabel (spec.md A-157).
--   judged_items     insert-only: each AI verdict the nightly sample judged, with the stratum
--                    it was drawn from (status, confidence band, evidence type) and whether
--                    the judge passed it; a judge that fails a verdict disagrees with it
--   relabel_batches  insert-only: the 20 judged items an expert relabels in a month
--   expert_labels    insert-only: an expert's label on a judged item, with a note
-- and quality_checks gains the kind `relabel`: the month's agreement against the judge's
-- calibrated interval, which fails when it falls outside and calls for re-calibration.

CREATE TABLE IF NOT EXISTS judged_items (
  tenant_id     TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  check_id      TEXT NOT NULL,
  verdict_id    TEXT NOT NULL,
  test_id       TEXT NOT NULL,
  stratum       TEXT NOT NULL,
  status        TEXT NOT NULL,
  confidence    DOUBLE PRECISION,
  judge_pass    BOOLEAN NOT NULL,
  judge_model   TEXT NOT NULL,
  judged_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, item_id)
);
CREATE INDEX IF NOT EXISTS ix_judged_items_time ON judged_items (tenant_id, judged_at);

CREATE TABLE IF NOT EXISTS relabel_batches (
  tenant_id   TEXT NOT NULL,
  batch_id    TEXT NOT NULL,
  month       TEXT NOT NULL,
  item_ids    JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, batch_id),
  UNIQUE (tenant_id, month)
);

CREATE TABLE IF NOT EXISTS expert_labels (
  tenant_id    TEXT NOT NULL,
  label_id     TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  label_pass   BOOLEAN NOT NULL,
  annotator    TEXT NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  labelled_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, label_id)
);
CREATE INDEX IF NOT EXISTS ix_expert_labels_item ON expert_labels (tenant_id, item_id, labelled_at);

GRANT SELECT, INSERT ON judged_items, relabel_batches, expert_labels TO {{app_role}};
REVOKE UPDATE, DELETE ON judged_items, relabel_batches, expert_labels FROM {{app_role}};
ALTER TABLE judged_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE judged_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON judged_items
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE relabel_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE relabel_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON relabel_batches
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
ALTER TABLE expert_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE expert_labels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON expert_labels
  USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

ALTER TABLE quality_checks DROP CONSTRAINT IF EXISTS quality_checks_kind_check;
ALTER TABLE quality_checks ADD CONSTRAINT quality_checks_kind_check
  CHECK (kind IN ('judged_sample', 'canary', 'shadow', 'relabel'));
