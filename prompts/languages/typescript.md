# TypeScript Review Rules

## Type Safety
- Strict mode enabled — no implicit `any`
- Avoid `as` type assertions when a type guard or conditional check suffices
- Use discriminated unions over type assertions for narrowing
- Interface/type definitions for all component props and API responses
- `unknown` over `any` for values of uncertain type — force explicit narrowing

## Null Safety
- Use optional chaining (`?.`) for property access on potentially null objects
- Use nullish coalescing (`??`) over logical OR (`||`) for default values (preserves `0`, `""`, `false`)
- Avoid non-null assertions (`!`) unless the invariant is truly guaranteed

## React Patterns (when applicable)
- Components use function declarations, not class components
- Hooks follow the rules of hooks (no conditional hooks, no hooks in loops)
- `useEffect` dependency arrays are complete — no missing or extra dependencies
- `useCallback` and `useMemo` are used where reference stability matters (not everywhere)
- State that derives from other state is computed, not stored separately

## Module Organization
- Named exports over default exports (better refactoring support)
- Barrel files (`index.ts`) re-export only — no logic
- Types and interfaces are co-located with their usage or in a shared types file
- No circular imports

## Error Handling
- Async operations have try/catch or .catch() handlers
- Error boundaries exist around major UI sections
- API errors surface user-friendly messages, not raw error objects
- Network failures show retry options or fallback UI

## Build & Config
- No `// @ts-ignore` or `// @ts-expect-error` without an explanatory comment
- Unused imports and variables are removed
- `tsconfig.json` strict settings are not relaxed in the diff
