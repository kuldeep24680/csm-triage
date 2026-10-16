-- Tenant isolation enforced by the database, not just the API.
--
-- Request handlers and per-tenant agent steps run inside a transaction that does
--     SET LOCAL ROLE triage_app;  SET LOCAL app.tenant_id = '<tenant>';
-- triage_app has no BYPASSRLS, so every query is filtered by the policies
-- below. If app.tenant_id is unset the policy compares against NULL and
-- returns nothing: isolation fails closed.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'triage_app') THEN
        CREATE ROLE triage_app NOLOGIN;
    END IF;
END $$;

-- The connecting (owner) user must be able to switch into the restricted role.
GRANT triage_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO triage_app;
GRANT SELECT ON models TO triage_app;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenants', 'users', 'landing_files', 'raw_records', 'rejected_records',
        'crm_users', 'crm_account_versions', 'accounts', 'crm_contacts',
        'crm_opportunities', 'identity_links', 'zendesk_orgs', 'tickets', 'emails',
        'product_workspaces', 'usage_weekly', 'stripe_customers', 'billing_events',
        'feature_snapshots', 'agent_runs', 'account_scores', 'briefs',
        'outreach_drafts', 'audit_log'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I TO triage_app
               USING (tenant_id = current_setting(''app.tenant_id'', true))
               WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO triage_app', t);
    END LOOP;
END $$;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO triage_app;

-- FORCE RLS also applies to the table owner, so the owner (used for
-- migrations, login lookup and cross-tenant orchestration) needs an explicit
-- policy. Superusers bypass RLS regardless.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenants', 'users', 'landing_files', 'raw_records', 'rejected_records',
        'crm_users', 'crm_account_versions', 'accounts', 'crm_contacts',
        'crm_opportunities', 'identity_links', 'zendesk_orgs', 'tickets', 'emails',
        'product_workspaces', 'usage_weekly', 'stripe_customers', 'billing_events',
        'feature_snapshots', 'agent_runs', 'account_scores', 'briefs',
        'outreach_drafts', 'audit_log'
    ]
    LOOP
        EXECUTE format('CREATE POLICY owner_all ON %I TO CURRENT_USER USING (true) WITH CHECK (true)', t);
    END LOOP;
END $$;
