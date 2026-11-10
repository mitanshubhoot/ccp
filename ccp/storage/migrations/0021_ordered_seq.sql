-- MT2: ordered topics are read by a per-(tenant, topic) sequence (spec.md A-104).
--   event_log.seq              an ordered-topic record's place in its tenant's topic, taken
--                              from the `topic:<topic>` counter in tenant_sequences, whose row
--                              the producing transaction locks until it commits. A second
--                              producer on the same tenant and topic waits, so numbers become
--                              visible in order and a cursor that passed n never meets a lower
--                              number later. The global offset is numbered at insert but
--                              visible at commit, which let a second process skip a row.
--   consumer_offsets.last_seq  the cursor, in seq; the offset cursor is dropped.
-- Parallel topics keep seq NULL: their records are claimed per row and need no order.

ALTER TABLE event_log ADD COLUMN IF NOT EXISTS seq BIGINT;

WITH numbered AS (
  SELECT "offset",
         row_number() OVER (PARTITION BY tenant_id, topic ORDER BY "offset") AS n
  FROM event_log
  WHERE topic IN ('raw.github', 'raw.hris', 'raw.manual', 'entities', 'verdicts')
)
UPDATE event_log e SET seq = numbered.n FROM numbered WHERE e."offset" = numbered."offset";

INSERT INTO tenant_sequences (tenant_id, name, value)
  SELECT tenant_id, 'topic:' || topic, max(seq) FROM event_log
  WHERE seq IS NOT NULL GROUP BY tenant_id, topic
ON CONFLICT (tenant_id, name)
  DO UPDATE SET value = greatest(tenant_sequences.value, EXCLUDED.value);

CREATE UNIQUE INDEX IF NOT EXISTS ux_event_log_seq
  ON event_log (tenant_id, topic, seq) WHERE seq IS NOT NULL;

-- The cursor moves from offsets to seq. A cursor row is named <consumer>:<tenant>, and
-- tenant ids hold no colon, so the tenant is what follows the last colon. Rows at or below
-- the old offset cursor were read, and seq numbers them in offset order, so the new cursor
-- is the highest seq among them.
ALTER TABLE consumer_offsets ADD COLUMN IF NOT EXISTS last_seq BIGINT NOT NULL DEFAULT 0;
UPDATE consumer_offsets c SET last_seq = coalesce((
  SELECT max(e.seq) FROM event_log e
  WHERE e.topic = c.topic
    AND e.tenant_id = regexp_replace(c.consumer, '^.*:', '')
    AND e.seq IS NOT NULL
    AND e."offset" <= c.last_offset
), 0);
ALTER TABLE consumer_offsets DROP COLUMN IF EXISTS last_offset;
