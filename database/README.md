# Minions Schema 

This repo houses all database related changes, migrations, etc.

# Running

  | Command                       | Description                                             |
  |-------------------------------|---------------------------------------------------------|
  | .\dbmate.ps1 pgsql up         | Apply all pending migrations                            |
  | .\dbmate.ps1 pgsql down       | Rollback the most recent migration                      |
  | .\dbmate.ps1 pgsql status     | Show migration status (applied vs pending)              |
  | .\dbmate.ps1 pgsql new <name> | Create a new migration file (e.g., new add_users_table) |
  | .\dbmate.ps1 pgsql migrate    | Alias for up                                            |
  | .\dbmate.ps1 pgsql rollback   | Alias for down                                          |
  | .\dbmate.ps1 pgsql create     | Create the database (if it doesn't exist)               |
  | .\dbmate.ps1 pgsql drop       | Drop the database                                       |
  | .\dbmate.ps1 pgsql dump       | Dump the schema to db/schema.sql                        |
  | .\dbmate.ps1 pgsql load       | Load schema from db/schema.sql                          |
  | .\dbmate.ps1 pgsql wait       | Wait for database to be available (useful in CI/Docker) |

  Most common workflow:
  # Check what needs to run
  .\dbmate.ps1 pgsql status

  # Apply pending migrations
  .\dbmate.ps1 pgsql up

  # Create a new migration
  .\dbmate.ps1 pgsql new add_some_feature

# Taskfile

### Install
```bash
scoop install task
```
