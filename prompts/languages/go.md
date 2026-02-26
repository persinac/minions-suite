# Go Review Rules

## Error Handling
- Errors are checked immediately after every function call that returns an error
- No `_` for error values unless explicitly justified with a comment
- Errors are wrapped with context using `fmt.Errorf("doing X: %w", err)` — not returned bare
- Sentinel errors use `errors.Is()` / `errors.As()` for comparison, not string matching
- Custom error types implement the `error` interface

## Concurrency
- Goroutines have clear ownership and lifecycle — every goroutine has a shutdown path
- Channels are closed by the sender, never the receiver
- `sync.WaitGroup` or `errgroup.Group` is used to wait for goroutine completion
- Shared state is protected by `sync.Mutex` or accessed via channels — not both
- Context propagation: `context.Context` is the first parameter, passed through the call chain

## Interfaces
- Interfaces are defined by the consumer, not the implementer
- Interfaces are small (1-3 methods) — prefer composition of small interfaces
- Accept interfaces, return structs
- No empty interface (`interface{}` / `any`) without a compelling reason

## Naming
- Package names are short, lowercase, single-word — no underscores or camelCase
- Exported names do not stutter (`http.Server` not `http.HTTPServer`)
- Receiver names are short (1-2 characters), consistent within a type
- Acronyms are all-caps (`ID`, `HTTP`, `URL`) in exported names

## Testing
- Table-driven tests for functions with multiple input/output scenarios
- Test helpers use `t.Helper()` for clean error reporting
- No `init()` in test files — use `TestMain` if setup is needed
- Benchmarks exist for performance-critical paths

## Dependencies
- `go.sum` is committed and not manually edited
- Vendoring or module proxy is configured for reproducible builds
- No unused dependencies in `go.mod`
