# SQL Review Rules

## Schema Changes
- New columns on existing tables are nullable or have DEFAULT values
- DROP COLUMN / DROP TABLE is preceded by data migration verification
- CHECK constraints enforce enum values and ranges at the database level
- Foreign keys specify ON DELETE behavior explicitly (CASCADE, SET NULL, RESTRICT)
- Column types are appropriate — do not over-size (BIGINT when INT suffices, TEXT when VARCHAR works)

## Indexes
- WHERE clause columns have supporting indexes
- JOIN columns are indexed on both sides
- Composite indexes match query patterns — column order matters (most selective first)
- Partial indexes are used when queries filter on constant predicates
- UNIQUE indexes enforce business uniqueness rules

## Migrations
- Each migration file has both UP and DOWN sections
- DDL and DML are not mixed in the same migration (schema changes separate from data backfills)
- Large table alterations use online DDL or are scheduled during maintenance windows
- Index creation on large tables uses CONCURRENTLY (Postgres) or equivalent
- Migration files do NOT contain explicit BEGIN/COMMIT — the migration tool handles transactions

## Query Patterns
- No string interpolation into queries — use parameterized statements or ORM
- SELECT specifies columns explicitly — no `SELECT *` in application code
- LIMIT is used on unbounded queries (especially in user-facing endpoints)
- COUNT(*) on large tables is avoided where approximate counts suffice
- Pagination uses keyset (cursor) pagination over OFFSET for large result sets
- Subqueries are evaluated for performance — prefer JOINs or CTEs when clearer

## Data Integrity
- Timestamps use a consistent timezone strategy (UTC preferred)
- Soft deletes (is_deleted, deleted_at) are preferred over hard deletes for audit-critical data
- Unique constraints prevent duplicate data where business rules require uniqueness
- NOT NULL is applied to columns that must always have values
