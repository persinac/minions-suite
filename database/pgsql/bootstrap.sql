-- Bootstrap script: Run this ONCE manually to set up the minions schema.
-- Connect to your target database as a superuser/admin, then run this script.
--
-- Option A: Dedicated database (greenfield)
--   CREATE DATABASE minions;
--   \c minions
--   -- then run the statements below
--
-- Option B: Existing database (e.g. cackalacky shared DB)
--   \c your_existing_db
--   -- then run the statements below

-- 1. Create the app user (skip if reusing an existing role)
CREATE USER minions_admin WITH PASSWORD 'your_password_here';

/**
If get: ERROR:  must be member of role "minions_admin"

SQL state: 42501

Run:
select current_user, current_role;
GRANT minions_admin TO <current_role>;
**/

-- 2. Create the schema and grant ownership
CREATE SCHEMA IF NOT EXISTS minions AUTHORIZATION minions_admin;

-- 3. Grant the user connect + usage on the database and schema
-- Grant connect on the database you're connected to:
--   GRANT CONNECT ON DATABASE your_db_name TO minions_admin;
GRANT USAGE, CREATE ON SCHEMA minions TO minions_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA minions
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO minions_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA minions
    GRANT USAGE, SELECT ON SEQUENCES TO minions_admin;

/* Brute force perms :cries-in-security: */
GRANT CONNECT ON DATABASE cackalacky TO minions_admin;
GRANT ALL ON DATABASE cackalacky TO minions_admin;