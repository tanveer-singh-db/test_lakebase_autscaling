-- Template: provision a Databricks identity as a Postgres role so it can
-- use the Lakebase Data API.
--
-- Run from the Lakebase SQL Editor authenticated as the project owner
-- (owners have DDL rights, even though they themselves can't use the Data
-- API — `authenticator` can't assume an elevated role).
--
-- Replace <IDENTITY> with either:
--   * a user's email (e.g. alice@example.com), when using 'USER' below, or
--   * a service principal's application id (UUID), when using 'SERVICE_PRINCIPAL'.
-- Replace <IDENTITY_TYPE> with 'USER' or 'SERVICE_PRINCIPAL'.

CREATE EXTENSION IF NOT EXISTS databricks_auth;

SELECT databricks_create_role(
    '<IDENTITY>',
    '<IDENTITY_TYPE>'
);

-- Let authenticator switch into this identity when serving API requests.
GRANT "<IDENTITY>" TO authenticator;

-- Table-level perms on the public schema.
GRANT USAGE ON SCHEMA public
    TO "<IDENTITY>";

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO "<IDENTITY>";

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public
    TO "<IDENTITY>";

-- Ensure future tables created in public are also accessible.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES
    TO "<IDENTITY>";
