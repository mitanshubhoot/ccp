-- MT1 step 3 (spec.md A-120): why a failed sync run failed, in the error table's terms.
-- auth: the credential was refused; not_visible: the source is hidden from it; fail: anything
-- else. A complete or partial run has none.
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS error_kind TEXT
  CHECK (error_kind IS NULL OR error_kind IN ('auth', 'not_visible', 'fail'));
