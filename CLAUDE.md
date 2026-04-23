# CLAUDE.md — project context for Claude Code

Read this first in any new session. It captures facts that don't live in the
code or git history but are needed to be useful here.

## What this repo is

A playground for **Databricks Lakebase Autoscaling** integration:

- `LakebaseAutoscalingClient` — direct-Postgres client (psycopg2 + pool).
- `LakebaseDataApiClient` / `AsyncLakebaseDataApiClient` — REST clients for
  the Lakebase Data API (PostgREST-compatible).
- Provisioning SQL + usage docs + unit/integration tests.

Everything is Python 3.11, runs locally in `.venv`, and also on DBR 16.4 LTS /
Serverless notebooks.

## Repo layout

```
.
├── CLAUDE.md                        ← this file
├── .gitignore                       ← ignores .venv, .idea, tests/test_config.yaml
├── pyproject.toml                   ← pytest config only
├── src/
│   ├── lakebase_utils/
│   │   ├── _common.py               resolve_base_url / resolve_auth / _make_ws
│   │   ├── lakebase_api.py          LakebaseDataApiClient (sync, requests)
│   │   ├── lakebase_api_async.py    AsyncLakebaseDataApiClient (aiohttp) + _TokenBucket + LakebaseDataApiError
│   │   └── lakebase_connect.py      LakebaseAutoscalingClient (psycopg2)
│   ├── create_widgets.sql           sample table DDL + grants
│   ├── provision_data_api_role.sql  template; MUST use SQL not UI (see below)
│   ├── test_*.py                    runnable demo scripts (not pytest)
│   └── test_user_authentication_flow.ipynb
├── tests/
│   ├── conftest.py                  mock_workspace_client, clean_env
│   ├── test_config.yaml             (gitignored, has real endpoint)
│   ├── test_config.yaml.template    (committed template)
│   ├── unit/                        62 pytest tests, no network
│   └── integration/                 4 tests, gated on LAKEBASE_API_URL
└── docs/
    ├── quick_reference.md           tables + FAQ ← send people here first
    ├── data_access_patterns.md      narrative: two-layer auth, provisioning, network patterns
    ├── lakebase_api.md              Data API client reference
    ├── lakebase_connect.md          Postgres client reference
    └── fix_data_api_auth.md         PGRST301 / 42501 playbook
```

## Critical mental model — the two-layer auth

Lakebase has two orthogonal permission surfaces. Internalise this or every
bug report reads as wrong:

1. **Workspace / project layer** — `CAN_CREATE` / `CAN_USE` / `CAN_MANAGE`
   ACLs on the Lakebase project. Granted via the Lakebase UI or
   Databricks ACL API. Controls who can create branches, resize computes,
   read connection strings.
2. **Database layer** — Postgres roles + `GRANT` statements. Controls who
   can `SELECT` / `INSERT` / etc. on tables.

A project `CAN_MANAGE` user will still get zero rows from `SELECT *` until
a Postgres role exists for their identity and has the right grants. The
two systems do not auto-sync.

Mermaid diagrams and the full explanation live in
`docs/data_access_patterns.md#authentication-model-at-a-glance`.

## Non-obvious gotchas — keep these top-of-mind

1. **Never use the project owner as a serving identity.** Owners inherit
   `databricks_superuser`; `authenticator` can't assume elevated roles.
   Any OAuth call as the owner fails with `password authentication failed`.
   Use a non-owner user or service principal for app traffic.

2. **Provision Data API roles via SQL, not the UI.** The Lakebase UI's
   *Roles & Databases → Add Role → OAuth* flow creates roles without
   `ADMIN OPTION` for the project owner, so `GRANT "<role>" TO authenticator`
   fails with `42501 permission denied to grant role`. Always use
   `databricks_create_role('<identity>', 'USER' | 'SERVICE_PRINCIPAL')`.
   See `src/provision_data_api_role.sql` and `docs/fix_data_api_auth.md`.

3. **Data API schemas need both GRANTs AND exposure.** After granting
   access, the schema must be added in the UI under **Data API →
   Settings → Exposed schemas** and the **Refresh schema cache** button
   clicked. Otherwise PostgREST returns `PGRST205 Could not find the table`.

4. **`information_schema.tables` is privilege-filtered.** It hides rows
   the current Postgres role has no privilege on. Use
   `pg_catalog.pg_tables` to see the unfiltered list.

5. **Prefer typed SDK methods.** Use
   `w.postgres.generate_database_credential(endpoint=...)` over raw
   `api_client.do()` for Lakebase credentials.

6. **Don't force a `host` on `WorkspaceClient(...)` unless needed.** In a
   notebook, `WorkspaceClient()` with no kwargs uses ambient auth. Forcing
   `host=...` locally is fine, but the client wrappers in this repo
   already pass only truthy kwargs via `_make_ws`.

## Dev / test commands

```bash
# Install dev deps (already in .venv after initial setup)
.venv/bin/pip install aiohttp pytest pytest-asyncio aioresponses pyyaml

# Unit tests (no network, 62 tests, ~2s)
.venv/bin/pytest tests/unit/ -v

# Integration tests (needs LAKEBASE_API_URL + SDK auth)
LAKEBASE_API_URL=... .venv/bin/pytest tests/integration/ -v
# ...or fill tests/test_config.yaml (gitignored) and just:
.venv/bin/pytest tests/integration/ -v

# Run one demo script
.venv/bin/python src/test_oauth_user.py
.venv/bin/python src/test_lakebase_api.py
```

## Working endpoint (for reference, not secrets)

This repo has been exercised against a live project called
`ts42-demo` in the workspace `adb-984752964297111.11.azuredatabricks.net`
(Azure East US 2). Endpoint host:
`ep-crimson-leaf-e1dz6s0k.database.eastus2.azuredatabricks.net`.

Those values are real but non-sensitive (URLs/UUIDs, no secrets). The live
`tests/test_config.yaml` has them pre-populated. A template without them
ships as `tests/test_config.yaml.template`.

## When the user asks…

- **"Why does the Data API return 42501 / PGRST301?"** → check the
  gotchas above, then point to `docs/fix_data_api_auth.md`.
- **"Which auth mode should I use?"** → `docs/quick_reference.md` FAQ and
  `docs/lakebase_api.md` / `docs/lakebase_connect.md` constructor tables.
- **"How do I test this from a notebook / outside Databricks?"** →
  `docs/quick_reference.md` has dedicated sections.
- **"How do I provision a new user / SP?"** →
  `src/provision_data_api_role.sql` + the SQL-vs-UI warning above.
- **"What's the network story for external apps?"** →
  `docs/data_access_patterns.md` §5.2 (on-prem) / §5.3 (direct Postgres).

## What NOT to do

- Don't commit secrets. `tests/test_config.yaml` is gitignored for this
  reason; put real values there, not in the template.
- Don't edit Lakebase roles via the UI when SQL will do — see gotcha #2.
- Don't redesign the auth layering without re-reading
  `docs/data_access_patterns.md` first — the two-layer model is
  intentional, not an artefact.
