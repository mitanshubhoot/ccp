-- LT11 step 2: a playbook records when a problem it raised is no longer seen (spec.md
-- A-167). `cleared` is still not a pass: the resource is a gap again until a deterministic
-- test covers the intent.

ALTER TABLE playbook_results DROP CONSTRAINT IF EXISTS playbook_results_status_check;
ALTER TABLE playbook_results ADD CONSTRAINT playbook_results_status_check
  CHECK (status IN ('fail', 'needs_review', 'cleared'));
