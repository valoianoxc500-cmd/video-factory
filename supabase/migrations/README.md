# Database migrations

## This folder is not yet the full history

Migrations for this project have historically been applied directly to the
hosted Supabase database and were never committed. At the time this folder was
created the live database had **14 applied migrations and this folder held
one**. The schema is therefore *not* reproducible from a fresh checkout, and
this folder should not be read as a complete record.

The thirteen earlier migrations — the `jobs`, `profiles`, `channels`,
`videos`, `usage_events`, `worker_credentials` and `vrf_*` objects — remain
unversioned. Backfilling them is deliberate future work and was explicitly out
of scope for the change that created this folder.

## What is here

| File | Records |
| --- | --- |
| `20260909200429_create_quote_studio_tables.sql` | Quote Studio persistence: `quote_studio_profiles`, `quote_studio_projects` |

That file **documents a migration that has already been applied to the live
database**. It was written after the fact, from the statements stored in
`supabase_migrations.schema_migrations`, so it reflects what actually ran
rather than what was intended to run.

## Do not re-run blindly against production

The Quote Studio migration is already applied. Running this folder against the
production database is unnecessary and, in general, unsafe:

- Every statement in that one file is `create ... if not exists` or
  `create policy`. The `create policy` statements are **not** idempotent and
  will fail with `42710 duplicate_object` on a database that already has them.
- Because this folder is an incomplete history, a migration tool that assumes
  it is complete may conclude the live database is ahead of, or diverged from,
  the repository.

Before applying anything here, check what the target database already has:

```sql
select version, name
from supabase_migrations.schema_migrations
order by version;
```

## Adding a migration from here on

Apply it and commit the file in the same change, named
`<version>_<snake_case_name>.sql` with the same `YYYYMMDDHHMMSS` version the
database records, so the two do not drift again.
