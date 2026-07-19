# Migrations

Apply these SQL files to Supabase/Postgres in order:

1. `001_create_services_schema.sql`
2. `002_seed_demo_devices.sql`

The application still uses the local SQLite fallback in this sandbox, but these files match the production `services` schema in the spec.

