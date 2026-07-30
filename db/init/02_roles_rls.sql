-- Row-Level Security: enforcement lives in the DATABASE, not in app-code discipline.
--
-- The two classic production traps, both handled here:
--   1) The table OWNER bypasses RLS by default  → FORCE ROW LEVEL SECURITY,
--      and the app connects as a separate role (rag_app), never as rag_owner.
--   2) Connection pooling leaks session state    → the app sets context via
--      set_config(..., is_local => true) INSIDE a transaction (SET LOCAL semantics).
--
-- current_setting(..., true) returns NULL when unset → policies match nothing.
-- Fail-closed: a request that forgot to set its context sees zero rows.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_app') THEN
        CREATE ROLE rag_app LOGIN PASSWORD 'rag_app_pw';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks, collection_state, feedback TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;

-- Visibility: same tenant AND (own department OR 'all').
-- WITH CHECK mirrors the write rule: a scope may only create/update rows for
-- its OWN department or 'all' — otherwise any user could plant documents into
-- a foreign department's visibility (write-side poisoning). The seeder is
-- compatible: it sets its scope per document (seed/ingest_corpus.py).
-- NOTE: policies apply on FIRST database init. An existing volume keeps the
-- old policies — reset with `docker compose down -v` + re-seed (see docs).
CREATE POLICY documents_visibility ON documents
    USING (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    )
    WITH CHECK (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    );

CREATE POLICY chunks_visibility ON chunks
    USING (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    )
    WITH CHECK (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    );

CREATE POLICY feedback_visibility ON feedback
    USING (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    )
    WITH CHECK (
        tenant = current_setting('app.tenant', true)
        AND department IN (current_setting('app.department', true), 'all')
    );

-- collection_state is not sensitive (a counter), but scope it to the tenant anyway.
ALTER TABLE collection_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection_state FORCE ROW LEVEL SECURITY;
CREATE POLICY collection_state_tenant ON collection_state
    USING (tenant = current_setting('app.tenant', true))
    WITH CHECK (tenant = current_setting('app.tenant', true));
