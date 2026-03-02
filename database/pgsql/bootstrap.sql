-- Bootstrap script: Run this ONCE manually to create the database and user
-- This cannot be run via dbmate as it creates the database dbmate connects to

-- Connect to postgres as superuser first, then run:
CREATE USER minions_admin WITH PASSWORD 'your_password_here';
CREATE DATABASE minions;
ALTER DATABASE minions OWNER TO minions_admin;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE minions TO minions_admin;
