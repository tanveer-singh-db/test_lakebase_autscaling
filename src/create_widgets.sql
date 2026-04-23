-- Create a sample `widgets` table in Lakebase with seed data.
--
-- Run this as the project OWNER (the Databricks user who created the project),
-- not the `authenticator` role — only the owner has CREATE on `public`.
--
--   psql "<owner-connection-url>" -f src/create_widgets.sql

CREATE TABLE IF NOT EXISTS public.widgets (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    stock       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.widgets (name, price_cents, stock) VALUES
    ('Sprocket', 1299, 100),
    ('Flange',    899,  50),
    ('Gizmo',    2499,  25)
ON CONFLICT DO NOTHING;

-- Let the Data API role read/write this table.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.widgets TO api_user;

-- Optional: let `authenticator` / `api_user` create future tables on its own.
-- GRANT CREATE ON SCHEMA public TO api_user;
