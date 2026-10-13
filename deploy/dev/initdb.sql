-- Local development database: same layout as production (schema "bridge",
-- owned by the app role). Alembic creates the tables on first start.
CREATE SCHEMA IF NOT EXISTS bridge;
REVOKE ALL ON SCHEMA bridge FROM PUBLIC;
