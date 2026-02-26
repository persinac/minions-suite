# Python Review Rules

## Style
- No ternary/inline conditionals — use explicit if/else blocks
- Atomic functions: one job per function, prefer functional programming
- Use `logging` module for output, never `print()` in application code
- String constants and status values should use Enums, not bare strings
- No module-level mutable state — state flows through arguments or config objects

## Async
- All I/O operations use async/await (database, HTTP, file I/O)
- No blocking calls inside async functions (no `time.sleep()`, use `asyncio.sleep()`)
- No `asyncio.run()` inside an already-running event loop
- Background tasks are tracked and cleaned up on shutdown

## Typing
- Type annotations on function signatures (parameters and return types)
- Use `Optional[T]` or `T | None` for nullable types — do not use bare `None` defaults without annotation
- Pydantic models for API boundaries (request/response schemas)
- Internal utility functions can be less strictly typed

## Error Handling
- Catch specific exceptions, not bare `except:` or `except Exception:`
- Do not swallow exceptions silently — at minimum log them
- Use custom exception classes for domain errors
- Context managers (`with`, `async with`) for resource cleanup

## Imports
- Standard library, then third-party, then local — separated by blank lines
- No wildcard imports (`from module import *`)
- Prefer absolute imports over relative imports

## Dependencies
- Use `requirements.txt`, `pyproject.toml`, or `uv` — dependencies are pinned
- Virtual environments are used (not system Python)
- No unnecessary dependencies for simple operations
