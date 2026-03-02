#Requires -Version 5.1
$ErrorActionPreference = "Stop"

if ($args.Count -lt 2) {
    Write-Host "Usage: .\dbmate.ps1 <database> <dbmate-command> [args...]"
    Write-Host "Example: .\dbmate.ps1 pgsql up"
    exit 1
}

$Database   = $args[0]
$DbmateArgs = $args[1..($args.Count - 1)]

# Get the directory where this script lives
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Build migrations dir correctly
$migrationsDir = Join-Path -Path $scriptDir -ChildPath "$Database\migrations"

if (-not (Test-Path $migrationsDir)) {
    Write-Error "Migrations directory not found: $migrationsDir"
    exit 1
}

# Construct DATABASE_URL from Doppler secrets
$dbUser     = doppler secrets get DB_ADMIN    --plain --project db-migrations --config prd
$dbPassword = doppler secrets get DB_PASSWORD --plain --project db-migrations --config prd
$dbHost     = doppler secrets get DB_HOST     --plain --project db-migrations --config prd
$dbPort     = doppler secrets get DB_PORT     --plain --project db-migrations --config prd
$dbName     = doppler secrets get DB_NAME     --plain --project db-migrations --config prd

# URL-encode user and password in case they contain special characters
$dbUserEncoded     = [System.Uri]::EscapeDataString($dbUser)
$dbPasswordEncoded = [System.Uri]::EscapeDataString($dbPassword)

$env:DATABASE_URL = "postgres://${dbUserEncoded}:${dbPasswordEncoded}@${dbHost}:${dbPort}/${dbName}?sslmode=require"

# Run dbmate with migrations directory
dbmate --migrations-dir "$migrationsDir" --migrations-table "minions.schema_migrations" @DbmateArgs
