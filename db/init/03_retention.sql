-- Retention enforcement (deletion concept): purge expired feedback rows.
--
-- SECURITY DEFINER, owned by rag_owner: retention is a SYSTEM maintenance duty
-- that must cover every tenant and department, while rag_app's RLS view is
-- deliberately bound to one request context. A narrowly-scoped, content-free
-- function is the least-privilege bridge across that boundary — the app role
-- gets EXECUTE on exactly this operation, never a blanket RLS bypass.
--
-- NOTE: db/init scripts run on FIRST container init only. Existing volumes
-- apply this once manually (the file is mounted in the postgres container):
--   docker compose exec -T postgres psql -U rag_owner -d rag -f /docker-entrypoint-initdb.d/03_retention.sql

CREATE OR REPLACE FUNCTION purge_expired_feedback(older_than interval)
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    WITH deleted AS (
        DELETE FROM feedback
        WHERE created_at < now() - older_than
        RETURNING id
    )
    SELECT count(*)::integer FROM deleted;
$$;

REVOKE ALL ON FUNCTION purge_expired_feedback(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_feedback(interval) TO rag_app;
