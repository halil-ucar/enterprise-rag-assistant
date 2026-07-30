-- Audit trail (A4): access METADATA only, append-only by grants (E3).
-- NOTE: db/init scripts run on FIRST container init only. Existing volumes
-- apply this once manually:
--   docker compose exec -T postgres psql -U rag_owner -d rag \
--     -f /docker-entrypoint-initdb.d/04_audit.sql
--
-- Design (mirrored in docs/ARCHITECTURE.md):
-- * No RLS on audit_events: rag_app has no SELECT — reading is structurally
--   impossible, not just filtered; auth_failure events have no valid scope
--   (tenant/user_id NULL). The protection model here is GRANTS. Read access
--   only via rag_audit_reader (NOLOGIN; the operator attaches a login role)
--   or rag_owner (make psql).
-- * "No field can carry content text" is DB-enforced: event_type/decision/
--   data_class are closed CHECK sets; all id fields are length-capped; count
--   columns are int; deliberately NO jsonb (a text smuggling path). Question/
--   answer/chunk text has no possible place here.
-- * Surrogate (E3): documents.audit_ref (random UUID; existing rows are
--   filled by the DEFAULT during ALTER). The audit stores ONLY audit_ref
--   values; the mapping lives in the document row and dies with the deletion
--   cascade — the log stays evidential, the reference becomes meaningless.
-- * 30-day floor in the purge function (GREATEST): defense in depth — even a
--   compromised app call with interval '0' cannot empty the log immediately.

ALTER TABLE documents ADD COLUMN IF NOT EXISTS audit_ref uuid
    NOT NULL DEFAULT gen_random_uuid();
CREATE UNIQUE INDEX IF NOT EXISTS documents_audit_ref_key ON documents(audit_ref);

CREATE TABLE IF NOT EXISTS audit_events (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    tenant      text CHECK (char_length(tenant) <= 200),
    user_id     text CHECK (char_length(user_id) <= 200),
    department  text CHECK (char_length(department) <= 200),
    event_type  text NOT NULL CHECK (event_type IN
        ('query','ingest','delete','policy_denial','rate_limited','auth_failure')),
    collection  text CHECK (char_length(collection) <= 200),
    data_class  text CHECK (data_class IN ('public','internal','confidential')),
    decision    text NOT NULL CHECK (decision IN
        ('context_served','no_context','accepted','executed','denied','limited','failed')),
    doc_refs    uuid[] NOT NULL DEFAULT '{}',
    request_id  text CHECK (char_length(request_id) <= 64),
    chunks_deleted int, cache_purged int, sessions_redacted int, feedback_deleted int
);
CREATE INDEX IF NOT EXISTS audit_events_ts ON audit_events (ts);

REVOKE ALL ON audit_events FROM PUBLIC;
GRANT INSERT ON audit_events TO rag_app;
-- "GRANT ... ON ALL SEQUENCES" in 02_roles_rls.sql does NOT cover sequences
-- created later — without this explicit grant the very first app INSERT
-- fails with permission denied.
GRANT USAGE ON SEQUENCE audit_events_id_seq TO rag_app;

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_audit_reader') THEN
        CREATE ROLE rag_audit_reader NOLOGIN;
    END IF;
END $$;
GRANT SELECT ON audit_events TO rag_audit_reader;

CREATE OR REPLACE FUNCTION purge_expired_audit(older_than interval)
RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
    WITH deleted AS (
        DELETE FROM audit_events
        WHERE ts < now() - GREATEST(older_than, interval '30 days')
        RETURNING id
    ) SELECT count(*)::integer FROM deleted;
$$;
REVOKE ALL ON FUNCTION purge_expired_audit(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_audit(interval) TO rag_app;
