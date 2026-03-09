# K8s CRD Design for the Job State Machine

## Overview

Two CRDs model the job orchestration domain: `MinionJob` (parent) and `MinionTask` (child). The state machine transition rules are enforced by the controller, not the CRD schema.

### Notes

Any time change
call reconciliation 

when deploy CRD, k8s will start watching
if state changes, the k8s recon engine will do things

nuances:
- ensure that all objects exist, else k8s won't know what to do with it



---

## MinionJob CRD

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: minionjobs.minions.dev
spec:
  group: minions.dev
  names:
    kind: MinionJob
    listKind: MinionJobList
    singular: minionjob
    plural: minionjobs
    shortNames: [mj]
    categories: [minion-suite]
  scope: Namespaced
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Type
          type: string
          jsonPath: .spec.jobType
        - name: Phase
          type: string
          jsonPath: .status.phase
        - name: Tasks
          type: string
          jsonPath: .status.taskSummary
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [jobType]
              properties:
                jobType:
                  type: string
                  enum: [development, review]

                # Development jobs
                featureSpec:
                  type: string
                  description: Feature specification text (development jobs)

                # Review jobs
                mrUrl:
                  type: string
                  description: MR/PR URL (review jobs)

                # Common
                project:
                  type: string
                  description: Project key from the registry
                externalRef:
                  type: string
                  description: External tracker ID (Trello card, GitLab issue, etc.)
                model:
                  type: string
                  default: ""
                  description: LiteLLM model override (empty = use default)
                dryRun:
                  type: boolean
                  default: false
                timeouts:
                  type: object
                  description: Per-role timeout overrides
                  additionalProperties:
                    type: object
                    properties:
                      taskTimeoutSeconds:
                        type: integer
                      subtaskTimeoutSeconds:
                        type: integer
                      maxSubtaskRetries:
                        type: integer

            status:
              type: object
              properties:
                phase:
                  type: string
                  enum:
                    - SpecReceived
                    - SpecReady
                    - TasksCreated
                    - DevInProgress
                    - PrOpen
                    - ReviewInProgress
                    - Merged
                    - Deploying
                    - Deployed
                    - Done
                    - NoWorkNeeded
                    - Failed
                observedGeneration:
                  type: integer
                  format: int64
                error:
                  type: string
                taskSummary:
                  type: string
                  description: "Human-readable, e.g. '2/3 done, 1 in_progress'"
                cost:
                  type: object
                  properties:
                    totalUsd:
                      type: string
                    inputTokens:
                      type: integer
                      format: int64
                    outputTokens:
                      type: integer
                      format: int64
                artifacts:
                  type: object
                  properties:
                    s3Prefix:
                      type: string
                    uploadedAt:
                      type: string
                      format: date-time
                conditions:
                  type: array
                  items:
                    type: object
                    required: [type, status]
                    properties:
                      type:
                        type: string
                        enum:
                          - SpecAnalysed
                          - TasksPlanned
                          - EngineersComplete
                          - PrsOpen
                          - ReviewComplete
                          - Merged
                          - Deployed
                          - ArtifactsUploaded
                          - Failed
                      status:
                        type: string
                        enum: ["True", "False", "Unknown"]
                      lastTransitionTime:
                        type: string
                        format: date-time
                      reason:
                        type: string
                      message:
                        type: string
                events:
                  type: array
                  description: Recent state machine events (ring buffer, last 50)
                  items:
                    type: object
                    properties:
                      timestamp:
                        type: string
                        format: date-time
                      type:
                        type: string
                      source:
                        type: string
                      detail:
                        type: string
```

---

## MinionTask CRD

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: miniontasks.minions.dev
spec:
  group: minions.dev
  names:
    kind: MinionTask
    listKind: MinionTaskList
    singular: miniontask
    plural: miniontasks
    shortNames: [mt]
    categories: [minion-suite]
  scope: Namespaced
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}
      additionalPrinterColumns:
        - name: Job
          type: string
          jsonPath: .spec.jobRef
        - name: Role
          type: string
          jsonPath: .spec.agentRole
        - name: Service
          type: string
          jsonPath: .spec.service
        - name: Phase
          type: string
          jsonPath: .status.phase
        - name: Attempt
          type: string
          jsonPath: .status.attempt
        - name: Age
          type: date
          jsonPath: .metadata.creationTimestamp
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [jobRef, title, service, agentRole]
              properties:
                jobRef:
                  type: string
                  description: Name of the parent MinionJob
                title:
                  type: string
                description:
                  type: string
                service:
                  type: string
                  description: Target service name from project registry
                agentRole:
                  type: string
                  enum:
                    - spec_analyst
                    - arbiter
                    - backend_engineer
                    - frontend_engineer
                    - database_engineer
                    - code_reviewer
                    - deploy_monitor
                maxAttempts:
                  type: integer
                  default: 3

                # Review-specific
                mrUrl:
                  type: string
                mrId:
                  type: string

            status:
              type: object
              properties:
                phase:
                  type: string
                  enum:
                    - Pending
                    - InProgress
                    - PrOpen
                    - InReview
                    - Merged
                    - Deploying
                    - Done
                    - Failed
                attempt:
                  type: integer
                revisionCount:
                  type: integer
                error:
                  type: string

                # PR lifecycle
                branchName:
                  type: string
                prNumber:
                  type: integer
                prUrl:
                  type: string
                reviewStatus:
                  type: string
                  enum:
                    - pending_review
                    - approved
                    - changes_requested
                    - revision_in_progress
                    - revision_complete
                deployStatus:
                  type: string

                # Review task results
                verdict:
                  type: string
                  enum: [approve, request_changes]
                commentsPosted:
                  type: integer

                # Agent tracking (current agent for this task)
                agent:
                  type: object
                  properties:
                    id:
                      type: string
                    k8sJobName:
                      type: string
                    status:
                      type: string
                      enum: [starting, running, completed, failed]
                    startedAt:
                      type: string
                      format: date-time
                    finishedAt:
                      type: string
                      format: date-time
                    cost:
                      type: object
                      properties:
                        inputTokens:
                          type: integer
                          format: int64
                        outputTokens:
                          type: integer
                          format: int64
                        costUsd:
                          type: string
                        numTurns:
                          type: integer

                # Subtask progress
                subtasks:
                  type: array
                  items:
                    type: object
                    properties:
                      sequenceNum:
                        type: integer
                      description:
                        type: string
                      status:
                        type: string
                        enum: [Pending, Running, Completed, Failed]
                      error:
                        type: string

                conditions:
                  type: array
                  items:
                    type: object
                    required: [type, status]
                    properties:
                      type:
                        type: string
                      status:
                        type: string
                        enum: ["True", "False", "Unknown"]
                      lastTransitionTime:
                        type: string
                        format: date-time
                      reason:
                        type: string
                      message:
                        type: string
```

---

## State Machine — Controller-Enforced Transitions

### Job Phase Transitions

```
SpecReceived  --> SpecReady --> TasksCreated --> DevInProgress --> PrOpen
     |                |               |                              |
     |                |               +--> ReviewInProgress <--------+
     |                |               |         |
     |                |               |         +--> Merged --> Deploying --> Deployed --> Done
     |                |               |         |       ^
     |                |               |         +--> Done
     |                |               |         +--> TasksCreated (revision)
     |                |               +--> NoWorkNeeded
     +--> Done        |
     |                +--> Failed <-- (any non-terminal)
     +--> Failed            |
                            +--> TasksCreated (arbiter retry)
```

Transition map (from `core/state_transitions.py`):

| From               | Allowed Next States                                           |
|--------------------|---------------------------------------------------------------|
| SpecReceived       | SpecReady, Done, Failed                                       |
| SpecReady          | TasksCreated, Failed                                          |
| TasksCreated       | DevInProgress, ReviewInProgress, NoWorkNeeded, Failed         |
| DevInProgress      | PrOpen, Merged, Failed                                        |
| PrOpen             | ReviewInProgress, InProgress, Failed                          |
| ReviewInProgress   | TasksCreated, Merged, Done, Failed                            |
| Merged             | Deploying, Deployed, Failed                                   |
| Deploying          | Deployed, Failed                                              |
| Deployed           | Done, Failed                                                  |
| Done               | (terminal)                                                    |
| NoWorkNeeded       | (terminal)                                                    |
| Failed             | TasksCreated (arbiter retry)                                  |

### Task Phase Transitions

**Default (most roles):**

| From        | Allowed Next States                           |
|-------------|-----------------------------------------------|
| Pending     | InProgress, Failed                             |
| InProgress  | PrOpen, Merged, Done, Failed                   |
| PrOpen      | InReview, InProgress, Merged, Done, Failed     |
| InReview    | Merged, InProgress, PrOpen, Failed             |
| Merged      | Deploying, Done, Failed                        |
| Deploying   | Done, Failed                                   |
| Done        | (terminal)                                     |
| Failed      | Pending (arbiter retry)                        |

**Database Engineer (no PR/review cycle):**

| From        | Allowed Next States      |
|-------------|--------------------------|
| Pending     | InProgress, Failed       |
| InProgress  | Merged, Done, Failed     |
| Merged      | Done, Failed             |
| Done        | (terminal)               |
| Failed      | Pending (arbiter retry)  |

### Role Restrictions on Task Transitions

| Transition             | Allowed Roles                                  |
|------------------------|------------------------------------------------|
| InProgress -> Merged   | database_engineer only                         |
| InProgress -> Done     | code_reviewer, deploy_monitor, database_engineer |
| PrOpen -> Done         | code_reviewer, deploy_monitor                  |
| PrOpen -> Merged       | code_reviewer only                             |
| InReview -> Merged     | code_reviewer only                             |
| Failed -> Pending      | arbiter only                                   |

### Task Preconditions (validating webhook)

| Target Phase | Required Fields                              |
|--------------|----------------------------------------------|
| PrOpen       | prUrl, prNumber, branchName                  |
| Merged       | prUrl (except database_engineer)             |
| Deploying    | branchName                                   |

---

## Ownership and Garbage Collection

```
MinionJob (parent)
  +-- ownerReferences --> MinionTask[] (children)
                            +-- ownerReferences --> K8s Job (agent pod)
                                                      +-- ConfigMap (work-item.json)
```

Deleting a `MinionJob` cascades to its `MinionTask`s, which cascade to their K8s Jobs and ConfigMaps.

---

## Design Decisions

1. **Two CRDs, not one.** Tasks are separate resources so the controller can watch/reconcile them independently, and `kubectl get mt` gives per-task visibility. Embedding tasks in `MinionJob.status` breaks down with concurrent engineer agents writing to the same status object.

2. **Subtasks stay embedded in `MinionTask.status`.** They're short-lived, tightly coupled to a single agent run, and don't need independent lifecycle management.

3. **Agent info embedded in `MinionTask.status.agent`.** One active agent per task at a time. Historical agent runs (retries) go into conditions/events, not a growing array.

4. **Conditions for milestone tracking.** Standard K8s pattern — each major gate (SpecAnalysed, ReviewComplete, Merged) gets a condition so tools like ArgoCD or Flux can gate on them.

5. **Transition rules live in the controller, not the schema.** OpenAPI can enforce valid enum values but can't express "from X only to Y." A validating webhook or the reconcile loop itself enforces the transition map.
