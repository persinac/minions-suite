## ADDED Requirements

### Requirement: Fast path archival
The MemoryArchiver SHALL provide `async archive_job(tuplespace, store, job_id, project) -> int` that reads all L2 facts for the given job, creates L3 MemoryNodes with temporal edges (ordered by timestamp) and entity edges (extracted from fact content), and returns the count of archived facts.

#### Scenario: Archive job facts to L3
- **WHEN** a job has published 5 facts to L2 during execution
- **WHEN** calling `archiver.archive_job(tuplespace, store, job_id, "payments-api")`
- **THEN** 5 MemoryNodes are created in L3 with appropriate tags and links
- **THEN** temporal edges connect the nodes in timestamp order
- **THEN** the return value is 5

#### Scenario: No facts to archive
- **WHEN** a job published no facts
- **WHEN** calling `archiver.archive_job(tuplespace, store, job_id, "payments-api")`
- **THEN** the return value is 0 and no nodes are created

### Requirement: L2 cleanup after archival
After successful archival, the archived facts SHALL be removed from L2 to prevent duplicate archival on subsequent runs.

#### Scenario: Facts cleaned up after archive
- **WHEN** `archive_job` completes successfully
- **THEN** the archived facts are no longer present in L2

### Requirement: Temporal edge creation
Archived nodes SHALL be linked with temporal edges (`FOLLOWS` link_type) based on their original L2 timestamps, preserving the chronological order of facts within a job.

#### Scenario: Temporal ordering preserved
- **WHEN** 3 facts are archived with timestamps t1 < t2 < t3
- **THEN** node1 --FOLLOWS--> node2 --FOLLOWS--> node3 edges are created

### Requirement: Entity edge extraction
The archiver SHALL extract entity references from fact content and create links from archived nodes to the corresponding entities (via `store.ensure_entity` and `store.create_link`).

#### Scenario: Entity extraction from facts
- **WHEN** a fact has value "Updated auth-module to use JWT tokens"
- **THEN** the archived node is linked to entity "auth-module" with link_type="mentions"

### Requirement: Async causal inference
The MemoryArchiver SHALL provide `schedule_causal_inference(store, node_ids, project) -> str | None` that submits an Anthropic Message Batches request to infer causal relationships between archived nodes. Returns a batch ID or None if batching is unavailable.

#### Scenario: Submit causal batch
- **WHEN** calling `schedule_causal_inference(store, node_ids, "payments-api")`
- **THEN** a Message Batches request is submitted with the node contents
- **THEN** a batch ID string is returned

#### Scenario: Batch API unavailable
- **WHEN** the Anthropic API key is not configured or batch API fails
- **THEN** the function returns None and logs a warning (non-fatal)

### Requirement: Causal batch processing
The MemoryArchiver SHALL provide `process_causal_batch(store, batch_id) -> int` that parses completed batch results and creates causal edges (`CAUSED_BY` link_type) between nodes. Returns the count of causal edges created.

#### Scenario: Process completed batch
- **WHEN** a batch has completed with causal inferences
- **WHEN** calling `process_causal_batch(store, batch_id)`
- **THEN** causal edges are created between the identified node pairs
- **THEN** the return value is the number of edges created

### Requirement: Archival triggered on job completion
The JobEngine SHALL trigger `archiver.archive_job()` when a job reaches a terminal state (DONE or FAILED) and `memory_enabled` is True.

#### Scenario: Job completion triggers archival
- **WHEN** a job transitions to DONE and `memory_enabled` is True
- **THEN** `archiver.archive_job()` is called with the job's tuplespace, store, job_id, and project

#### Scenario: No archival when disabled
- **WHEN** a job completes and `memory_enabled` is False
- **THEN** `archiver.archive_job()` is NOT called

### Requirement: Periodic causal batch polling
The JobEngine main loop SHALL periodically check for completed causal inference batches and process them when `memory_enabled` is True.

#### Scenario: Batch polling in main loop
- **WHEN** the job engine is running with `memory_enabled=True`
- **THEN** it periodically polls for completed causal batches and calls `process_causal_batch` for each
