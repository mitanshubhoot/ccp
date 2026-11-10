-- NT6: every verdict names the rule that decided it (spec.md A-101): "<rule version>:<parameters
-- hash>", the same stamp the evaluation key includes, so a corrected rule over unchanged data
-- is a new evaluation and an auditor can ask "by what method?" of any stored verdict.
-- Rows written before this migration carry NULL: their method is the code of their commit.

ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS rule_version TEXT;
CREATE INDEX IF NOT EXISTS ix_verdicts_rule_version ON verdicts (tenant_id, test_id, rule_version);
