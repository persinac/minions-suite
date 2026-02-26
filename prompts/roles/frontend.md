# Frontend Engineering Review

## Accessibility
- Interactive elements are keyboard-navigable (buttons, links, form fields)
- Images have meaningful alt text (or empty alt="" for decorative images)
- ARIA labels are used on elements without visible text labels
- Color is not the only indicator of state (pair with icons or text)
- Focus management is correct after modals, navigation, and dynamic content changes

## State Management
- Server state uses the project's data-fetching pattern (React Query, SWR, etc.) — not local useState for remote data
- Loading, error, and empty states are all handled in the UI
- Optimistic updates revert gracefully on failure
- Derived state is computed, not stored redundantly

## Component Patterns
- Components have clear, minimal props interfaces
- No prop drilling more than 2 levels deep — use context or composition
- Side effects are in useEffect with correct dependency arrays (no missing deps, no over-firing)
- Event handlers are stable references (useCallback where needed to prevent rerenders)

## Security
- No `dangerouslySetInnerHTML` without sanitization
- User input is escaped before rendering
- Sensitive tokens are not stored in localStorage (use httpOnly cookies or in-memory)
- API calls use CSRF tokens where required

## Rendering & Performance
- Lists use stable, unique keys (not array index unless static)
- Large lists use virtualization when appropriate
- Images are optimized (proper sizing, lazy loading, next/image or equivalent)
- No unnecessary rerenders from unstable object/array references in props

## Responsive Design
- Layout works on mobile, tablet, and desktop viewports
- Touch targets are at least 44x44px
- No horizontal scrolling on mobile
- Text is readable without zooming
