-- MT2: one evaluation in flight per subject, and late verdicts filed into history (A-107).
--   ix_event_log_subject      finds a live claim, or a waiting record, on one partition key
--                             (an evaluation's partition key is its tenant and subject)
--   compliance_state.history  a verdict about an earlier moment than the subject's head,
--                             recorded after it: kept, bounded at the next newer fact, and
--                             never the head, so it cannot displace a newer belief

CREATE INDEX IF NOT EXISTS ix_event_log_subject ON event_log (topic, partition_key)
  WHERE status IN ('pending', 'claimed');

ALTER TABLE compliance_state ADD COLUMN IF NOT EXISTS history BOOLEAN NOT NULL DEFAULT false;
