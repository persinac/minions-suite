# Backend Engineering Review

## API Contracts
- HTTP status codes match semantics: 201 for create, 204 for delete, 409 for conflict, 422 for validation
- Response shapes are consistent across endpoints (same error format, same pagination structure)
- Breaking changes to existing endpoints are flagged — check for removed fields, changed types, renamed paths
- Content-Type headers are set correctly (application/json, multipart/form-data)

## Auth & Authorization
- Every mutating endpoint has auth middleware applied
- Role-based access is checked (not just "is authenticated" but "has permission for this resource")
- Resource ownership is validated — users cannot access/modify other users' resources
- Admin-only endpoints are protected with admin role checks

## Input Validation
- All user input is validated at the boundary (request body, query params, path params)
- Numeric ranges, string lengths, and enum values are constrained
- Optional fields have sensible defaults
- Reject unexpected fields rather than silently ignoring them

## Error Handling
- Internal errors do not leak stack traces or internal state to the client
- Database errors are caught and translated to appropriate HTTP responses
- External service failures are handled with timeouts and fallbacks
- Error responses include enough context for the caller to understand what went wrong

## Database
- Queries use parameterized values (no string interpolation into SQL)
- N+1 query patterns are avoided (use joins or batch fetches)
- Transactions are used for multi-step mutations
- New columns are nullable or have defaults (to avoid breaking existing rows)
- Indexes are added for columns used in WHERE, JOIN, and ORDER BY clauses

## Idempotency
- PUT and DELETE operations are idempotent
- POST operations that create resources handle duplicate submissions gracefully
- Retryable operations use idempotency keys where appropriate
