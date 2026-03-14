## ADDED Requirements

### Requirement: Knowledge context builder
The context module SHALL provide `build_knowledge_context(store, project, task_description, embedding, max_tokens) -> str` that retrieves relevant L3 memories and formats them as Obsidian-style markdown. SHALL return empty string when no relevant knowledge is found.

#### Scenario: Build context with relevant memories
- **WHEN** L3 contains memories about "auth patterns" in project "payments-api"
- **WHEN** calling `build_knowledge_context(store, "payments-api", "implement JWT refresh", embedding, max_tokens=2000)`
- **THEN** the output is Obsidian-style markdown containing relevant memories with tags, links, source attribution, and a local graph view section

#### Scenario: Empty context when no memories
- **WHEN** L3 has no memories for project "new-project"
- **WHEN** calling `build_knowledge_context(store, "new-project", "any task", embedding, max_tokens=2000)`
- **THEN** the result is an empty string

#### Scenario: Respects token budget
- **WHEN** many relevant memories exist
- **WHEN** calling with `max_tokens=500`
- **THEN** the output fits within 500 tokens, prioritizing highest-scored memories

### Requirement: File context builder
The context module SHALL provide `build_file_context(store, project, file_paths, max_tokens) -> str` that retrieves memories linked to the given file paths (via backlinks) and formats them as markdown. SHALL return empty string when no file-linked memories exist.

#### Scenario: Build file context for code review
- **WHEN** memories are linked to entities "src/auth/handler.py" and "src/auth/middleware.py"
- **WHEN** calling `build_file_context(store, "payments-api", ["src/auth/handler.py", "src/auth/middleware.py"])`
- **THEN** the output contains file-grouped markdown sections with relevant memories for each file

#### Scenario: Empty file context
- **WHEN** no memories are linked to the given file paths
- **THEN** the result is an empty string

### Requirement: Prompt integration for engineers
The `build_agent_prompt()` function SHALL accept an optional `knowledge_context: str | None` parameter. When provided and non-empty, a "Prior Knowledge" section SHALL be inserted into the prompt between the task context and additional context sections.

#### Scenario: Engineer gets prior knowledge
- **WHEN** `knowledge_context` contains memory markdown
- **WHEN** calling `build_agent_prompt(role="BACKEND_ENGINEER", knowledge_context=knowledge_ctx)`
- **THEN** the prompt includes a "## Prior Knowledge" section with the memory content

#### Scenario: No section when context is None
- **WHEN** `knowledge_context` is None or empty string
- **WHEN** calling `build_agent_prompt(role="BACKEND_ENGINEER", knowledge_context=None)`
- **THEN** the prompt does NOT contain a "Prior Knowledge" section

### Requirement: Prompt integration for code reviewers
The review engine SHALL build file context from changed files and inject it into the reviewer's prompt when `memory_enabled` is True.

#### Scenario: Reviewer gets file knowledge
- **WHEN** `memory_enabled` is True and changed files have linked memories
- **WHEN** running a review job
- **THEN** the reviewer's prompt includes a "## File Knowledge" section with file-grouped memories

#### Scenario: No file knowledge when disabled
- **WHEN** `memory_enabled` is False
- **THEN** the reviewer's prompt does NOT contain a "File Knowledge" section

### Requirement: Dev engine knowledge injection
The dev engine SHALL build knowledge context from the task description before launching engineer agents when `memory_enabled` is True.

#### Scenario: Engineer launched with knowledge
- **WHEN** `memory_enabled` is True and the memory store has relevant knowledge
- **WHEN** `run_engineer()` is called
- **THEN** `build_knowledge_context` is called with the task description and the result is passed to the agent prompt
