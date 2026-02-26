# Data Engineering Review

## Schema Design
- New tables have appropriate primary keys (prefer surrogate keys for application tables)
- Column types are correct and minimal (don't use TEXT when VARCHAR(50) suffices, don't use BIGINT when INT suffices)
- NOT NULL constraints are applied to columns that must always have values
- DEFAULT values are set for columns with common initial values
- Foreign keys have ON DELETE behavior defined (CASCADE, SET NULL, or RESTRICT)

## Migrations
- Migrations are reversible — both UP and DOWN sections are present and correct
- New columns on existing tables are nullable or have defaults (to avoid locking large tables)
- Destructive changes (DROP COLUMN, DROP TABLE) are preceded by verification that data is migrated
- Index creation on large tables uses CONCURRENTLY where supported
- Migrations do not contain application logic or data transformations mixed with DDL

## Query Performance
- Queries used in hot paths have supporting indexes
- JOINs are on indexed columns
- EXPLAIN ANALYZE has been considered for new complex queries
- COUNT(*) on large tables is avoided in favor of approximate counts where acceptable
- Pagination uses cursor-based (keyset) approach over OFFSET for large result sets

## Data Integrity
- CHECK constraints enforce business rules at the database level (enum values, ranges, formats)
- Unique constraints prevent duplicate data where business rules require uniqueness
- Composite indexes match the actual query patterns (column order matters)
- Partial indexes are used when queries filter on a constant value

## ETL & Pipelines
- Idempotent operations — rerunning a pipeline does not create duplicates
- Error handling includes dead-letter queues or retry mechanisms
- Large data operations are batched to avoid memory issues
- Timestamps use UTC consistently (no mixing of timezones)

## Observability
- Slow queries are logged or flagged
- Data quality checks exist for critical tables (row counts, null rates, freshness)
- Pipeline failures trigger alerts
