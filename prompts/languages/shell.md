# Shell Script Review Rules

## Safety
- Scripts start with `set -euo pipefail` (fail on errors, undefined variables, pipe failures)
- All variable expansions are quoted: `"$VAR"` not `$VAR`
- Temporary files use `mktemp` and are cleaned up via `trap 'rm -f "$tmpfile"' EXIT`
- No `eval` with user-controlled input
- No `curl | sh` patterns — download first, verify, then execute

## Portability
- Shebang line is `#!/usr/bin/env bash` for bash scripts, `#!/bin/sh` for POSIX-only
- Bash-specific features (arrays, `[[ ]]`, process substitution) are only used in bash scripts
- GNU vs BSD tool differences are handled (e.g. `sed -i` behaves differently on macOS)
- No reliance on specific PATH ordering — use full paths for critical binaries

## Structure
- Functions are used for reusable logic — not everything in the global scope
- Exit codes are meaningful: 0 for success, non-zero for specific failure modes
- Usage/help text is shown when invoked with `--help` or incorrect arguments
- Long commands use backslash continuation for readability

## Input Handling
- Script arguments are validated before use
- Default values are set with `${VAR:-default}` syntax
- File existence is checked before reading (`[[ -f "$file" ]]`)
- User-provided paths are never passed unsanitized to `rm -rf`

## Logging
- Output goes to stderr for diagnostic messages, stdout for data output
- Verbose/debug output is gated behind a flag (`-v` or `--verbose`)
- Colors are only used when stdout is a terminal (`[[ -t 1 ]]`)
