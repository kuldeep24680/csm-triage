-- Account page: look up source rows by the account's linked external ids,
-- instead of scanning every ticket/email/billing row in the tenant.
CREATE INDEX IF NOT EXISTS identity_links_account_idx ON identity_links (tenant_id, account_id, source);
CREATE INDEX IF NOT EXISTS tickets_org_created_idx ON tickets (tenant_id, org_id, created_at);
CREATE INDEX IF NOT EXISTS tickets_requester_created_idx ON tickets (tenant_id, requester_email, created_at);
CREATE INDEX IF NOT EXISTS emails_counterpart_sent_idx ON emails (tenant_id, counterpart, sent_at);
CREATE INDEX IF NOT EXISTS billing_customer_occurred_idx ON billing_events (tenant_id, customer_id, occurred_at);
CREATE INDEX IF NOT EXISTS crm_contacts_account_idx ON crm_contacts (tenant_id, account_id);
CREATE INDEX IF NOT EXISTS crm_account_versions_account_idx ON crm_account_versions (tenant_id, account_id, modified_at DESC);
CREATE INDEX IF NOT EXISTS briefs_account_idx ON briefs (tenant_id, account_id);

-- Brief reuse: a fingerprint of the facts a brief was written from. When the
-- next run produces the same fingerprint for an account, the brief is reused
-- (after re-validating it against the new facts) instead of calling the LLM again.
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS facts_hash text;
ALTER TABLE briefs ADD COLUMN IF NOT EXISTS email jsonb;   -- the drafted email, so a reused brief keeps it
