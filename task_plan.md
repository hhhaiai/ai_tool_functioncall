# Task Plan: Analyze the AI Tool Function-Call Gateway

## Goal
Fully correct the audited Gateway issues, harden production behavior, verify the result with local/full/integration tests and a controlled real-upstream smoke, preserve the user's existing worktree, and make no Git commits.

## Current Phase
Phase 28

## Phases

### Phase 1: Repository Discovery
- [x] Read repository guidance and top-level documentation
- [x] Inventory code, tests, configuration, and deployment surfaces
- [x] Establish project purpose and supported operating modes
- **Status:** complete

### Phase 2: Architecture & Runtime Analysis
- [x] Trace application startup and HTTP request flow
- [x] Trace tool conversion, execution, streaming, and persistence paths
- [x] Map module responsibilities and dependencies
- **Status:** complete

### Phase 3: Quality & Risk Review
- [x] Inspect configuration, security, concurrency, and error handling
- [x] Inspect tests and identify coverage or maintainability gaps
- [x] Distinguish current evidence from potentially stale documentation
- **Status:** complete

### Phase 4: Testing & Verification
- [x] Run the automated test suite or representative subsets
- [x] Perform static syntax/consistency checks
- [x] Record failures and determine whether they are code or environment issues
- **Status:** complete

### Phase 5: Delivery
- [x] Cross-check conclusions against source and tests
- [x] Prepare a concise Chinese report with prioritized recommendations
- **Status:** complete

### Phase 6: Current-State Revalidation
- [x] Inspect changes since the 2026-07-16 audit
- [x] Recheck whether confirmed defects still exist
- [x] Re-run proportionate verification for changed/current code
- **Status:** complete

### Phase 7: Enhancement Roadmap
- [x] Separate unresolved defects from optional enhancements
- [x] Prioritize by production risk, effort, and expected value
- [x] Prepare an actionable staged roadmap in Chinese
- **Status:** complete

### Phase 8: Upstream Transport Reliability
- [x] Correct curl transport failure detection and HTTP 000 handling
- [x] Implement bounded retry/deadline/Retry-After behavior with correct error types
- [x] Add regression tests for transport codes, timeouts, malformed responses, and retry limits
- **Status:** complete

### Phase 9: Configuration, Identity & Secret Safety
- [x] Make config writes locked, atomic, restrictive, and conflict-aware where applicable
- [x] Make encryption failure behavior explicit/fail-closed in production
- [x] Unify downstream client identity with permission policy and secure defaults
- [x] Eliminate runtime/template/Compose default drift
- **Status:** complete

### Phase 10: Response Correctness & Canonical Execution
- [x] Fix fan-out ordering/truncation/rewrite semantics
- [x] Replace under-keyed semantic caching with conservative canonical request fingerprints
- [x] Route legacy Claude execution through the canonical tool runtime
- [x] Normalize bad-request and package import behavior
- **Status:** complete

### Phase 11: Production Runtime Hardening
- [x] Establish an enforceable TLS/external-ingress contract
- [x] Add live/readiness probes and graceful SIGTERM cleanup
- [x] Harden container privileges and bound long-lived subprocess/output resources
- [x] Add bounded retention/cleanup behavior
- **Status:** complete

### Phase 12: Capability & Operations Integration
- [x] Enforce configured per-client/tenant rate limits and budgets on the live path
- [x] Expose truthful capability discovery for minimal or inactive surfaces
- [x] Integrate or explicitly demote dormant concurrency/stats/Web2API surfaces
- [x] Add practical observability hooks without destabilizing the runtime
- **Status:** complete

### Phase 13: Comprehensive Verification
- [x] Run syntax/static checks and focused regression suites after each batch
- [x] Run full pytest and deployment contract verification
- [x] Run local service smoke and controlled real-upstream smoke without persisting secrets
- **Status:** complete

### Phase 14: Completion Audit
- [x] Re-audit every identified issue against current source and runtime evidence
- [x] Confirm no Git commit was created and no secret entered tracked files/log artifacts
- [x] Document remaining optional enhancements separately from required fixes
- **Status:** complete

### Phase 15: Incremental Tool-Runtime Stability Audit
- [x] Audit canonical Bash/code-interpreter outcome classification for non-zero exits
- [x] Audit read/tool-result cache invalidation after workspace mutations, including persistence scope
- [x] Reproduce confirmed defects and prepare targeted regression/fix designs without changing product code beyond the user's analysis request
- [x] Run focused and full-suite verification without alarm/reminder testing
- [x] Document remaining risks and prioritized enhancements
- **Status:** complete

### Phase 16: Tool Outcome and Cache-Coherence Corrections
- [x] Implement canonical non-zero process failure semantics and non-destructive retry classification
- [x] Implement workspace/runtime-scoped memory and persistent tool-cache invalidation
- [x] Invalidate cache after all mutation-capable tools, including partial-failure/timeout paths
- [x] Add direct/protocol/orchestration/cache-isolation/persistence regressions
- [x] Run focused tests, local real-tool acceptance, full pytest, and static/deployment gates without alarm/reminder validation
- **Status:** complete

### Phase 17: Process Output and Exec-Session Reliability
- [x] Replace post-hoc output truncation with genuinely memory-bounded Bash/code capture
- [x] Terminate process groups on timeout so shell grandchildren do not survive
- [x] Normalize immediate and waited exec-session non-zero exit semantics
- [x] Add output-flood, timeout-child, exec-session protocol, and cleanup regressions
- [x] Run focused/full/local acceptance/static/deployment gates without alarm/reminder validation
- **Status:** complete

### Phase 18: Atomic and Concurrent Workspace Writes
- [x] Inventory all direct local file replacement paths and their metadata semantics
- [x] Add striped in-process locks plus optional cross-process advisory locks
- [x] Add fsync-backed atomic replace preserving existing mode/ownership where possible
- [x] Migrate canonical Write/Edit/MultiEdit/RegexEdit/CopyPath/NotebookEdit paths
- [x] Add concurrency/failure/permission/temp-cleanup regressions and run full gates without alarm/reminder validation
- **Status:** complete

### Phase 19: Durable Path Mutations and Transactional Patch Safety
- [x] Audit CreateDirectory/DeletePath/MovePath/apply_patch races, durability, and confinement
- [x] Add ordered multi-path locking and durable directory mutation helpers
- [x] Pre-validate all Codex patch targets against the current workspace
- [x] Snapshot targets and rollback partial patch failure/timeout safely
- [x] Add adversarial/concurrency regressions and run full gates without alarm/reminder validation
- **Status:** complete

### Phase 20: Remaining-Risk and Enhancement Audit
- [x] Revalidate current worktree/HEAD and preserve all existing dirty changes
- [x] Inspect shared rate limiting, upstream response buffering, MCP subprocess lifecycle, streaming semantics, capability scope, observability, and module hotspots
- [x] Separate confirmed runtime risks from architectural/product enhancements
- [x] Prioritize the next implementation batches without changing product code or running alarm/reminder validation
- **Status:** complete

### Phase 21: Upstream and MCP Resource Boundaries
- [x] Add synchronized configuration for upstream response and MCP frame/stderr limits
- [x] Replace unbounded upstream subprocess capture with bounded concurrent pipe draining
- [x] Bound MCP framing, continuously drain stderr, and terminate complete process groups
- [x] Add adversarial output/frame/process-tree regressions
- [x] Run focused/full/local acceptance/static/deployment gates without alarm/reminder validation
- **Status:** complete

### Phase 22: Shared Multi-Process Rate Limiting
- [x] Define privacy-preserving memory/SQLite backend and failure semantics
- [x] Implement atomic cross-process token consumption, restart persistence, and expiry cleanup
- [x] Wire live HTTP enforcement, capabilities, and metrics to the active backend
- [x] Synchronize Python/JSON/YAML/Compose/environment defaults
- [x] Add multi-process/restart/expiry/privacy/fallback regressions and run full gates
- **Status:** complete

### Phase 23: True End-to-End Streaming Orchestration
- [x] Define a bounded canonical upstream streaming event/state contract for Chat, Responses, and Anthropic
- [x] Add cancellable upstream streaming transport with response/event limits and disconnect cleanup
- [x] Stream safe text deltas immediately while buffering tool-decision boundaries
- [x] Preserve multi-round tool execution, downstream delegation, final response/memory/audit parity, and protocol-specific completion events
- [x] Add timing/backpressure/disconnect/tool-delta/cross-protocol regressions and run full gates
- **Status:** complete

### Phase 24: Post-Streaming Remaining-Risk Audit
- [x] Restore Phase 21–23 evidence and revalidate the dirty worktree/HEAD
- [x] Inspect executable-tool/MCP isolation, environment inheritance, and container boundaries
- [x] Inspect external exposure/CORS, observability, CI/static gates, protocol scope, and maintainability hotspots
- [x] Rank confirmed issues and enhancements by production risk, effort, and dependency order
- [x] Deliver an evidence-backed Chinese roadmap without changing product code or testing alarm/reminder behavior
- **Status:** complete

### Phase 25: External Binding and CORS Safety
- [x] Define a backward-compatible local/external binding security contract
- [x] Make ordinary Compose loopback-only by default and fail closed for unsafe external binds
- [x] Enforce default-credential handling and configurable CORS origin policy
- [x] Add startup, HTTP, configuration-sync, and deployment regressions
- [x] Run focused/full/static/Compose gates without alarm/reminder validation
- **Status:** complete

### Phase 26: Executable Environment and Sandbox Isolation
- [x] Define a versioned sandbox job/result/diff contract and capability reporting
- [x] Stop Shell/code/apply-patch/exec/MCP children from inheriting Gateway secrets by default
- [x] Add a cancellable worker boundary with per-job resource policies and validated workspace/write-scope declarations
- [x] Validate apply_patch overlay diffs before atomic workspace commit and preserve rollback/cache/audit semantics
- [x] Enforce OS/container filesystem and network isolation for Shell/code/exec/MCP
- [x] Add secret-read, undeclared-write, network-deny, resource-flood, crash, cancellation, and multi-tenant regressions
- [x] Run focused/full/static/deployment/local acceptance gates without alarm/reminder validation
- **Status:** complete

### Phase 27: Persistent Data Security and Capacity Governance
- [x] Centralize restrictive SQLite/runtime directory creation and harden DB/WAL/SHM modes
- [x] Add bounded retention for primary request logs, tool failures, memories, planner sessions, and stale runtime directories
- [x] Add incremental cleanup/checkpoint/space metrics with observable failure semantics
- [x] Add permission, retention, capacity, concurrency, and failure regressions
- [x] Run focused/full/static/deployment/local acceptance gates without alarm/reminder validation
- **Status:** complete

### Phase 28: Remaining Scalability and Operations Roadmap
- [x] Design an explicit operator-controlled one-time compaction path for legacy SQLite files that predate incremental auto-vacuum
- [x] Replace process-local global request concurrency with a truthful shared/multi-process admission boundary
- [x] Add request/tool/upstream tracing and per-component latency/error metrics with bounded-cardinality labels
- [x] Establish enforced CI lint/type/security/dependency/deployment gates
- [ ] Split the largest runtime/test modules without changing protocol behavior
  - [x] Extract authenticated Admin operations GET routes and add direct contracts
  - [x] Extract request-admission compatibility boundary and preserve facade/runtime exports
  - [x] Extract typed HTTP request/response I/O primitives while preserving Handler/facade exports and request-limit monkeypatch behavior
  - [x] Extract typed Admin/downstream authentication with password-upgrade, stable client identity, route ACL, and fail-closed malformed configuration contracts
  - [x] Extract Admin same-origin write policy and bounded JSON/urlencoded form decoding, including forwarded-host spoof resistance
  - [x] Extract transactional client config, Admin password, and downstream-key mutations with copy-before-validate and single revision-aware save
  - [x] Extract transactional MCP, HTTP Action, and upstream-profile mutations with save-before-reload and active-profile consistency
  - [x] Extract transactional General Admin Config with upstream/path/limit/CORS/context invariants and complete profile uniqueness checks
  - [ ] Finish the Admin Skill/Marketplace/MCP catalog boundary: align install visibility with runtime discovery, close symlink races, enforce restrictive permissions, and add direct/static-gate coverage
  - [ ] Continue decomposing the remaining 5k-line Tool Runtime, 4.4k-line Planner, and 13.6k-line Gateway test module
- [x] Re-evaluate intentionally minimal Assistants/Threads and inactive Web2API/concurrency surfaces against actual product scope
- **Status:** in_progress

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Treat source code and executable tests as authoritative | The repository contains many historical/status documents that may lag implementation. |
| Make no product-code edits | The user requested analysis, not implementation. |
| Preserve the existing dirty worktree | Existing modifications belong to the user and are outside the analysis scope. |
| Revalidate before repeating prior findings | The current date/worktree may have changed since the previous audit. |
| Do not commit | The user explicitly prohibited Git commits. |
| Preserve current dirty changes and edit overlaps carefully | Existing worktree modifications are user-owned. |
| Use the real upstream only through controlled smoke paths | The provided credential must not enter source, tests, planning files, or committed artifacts. |
| Treat Phase 16 as an implementation iteration | The persistent user goal explicitly asks to optimize and continue functional stability work, so the confirmed Phase 15 defects are now authorized for correction. |
| Treat Phases 21–22 as implementation iterations | The persistent goal requires continued stabilization; resource-boundary and shared-quota gaps were confirmed in current source and corrected with direct regressions. |
| Keep full legacy SQLite compaction offline and explicit | The current online request/readiness boundary cannot prove all Admin/background/global SQLite writers are quiescent; read-only Admin preflight plus a stopped-Gateway CLI is the safe mutation contract. |
| Default shared admission failure to fail-closed | Falling back silently to process-local concurrency can overload a multi-worker deployment; memory fallback remains an explicit operator choice and is reported as degraded. |
| Keep traces bounded and memory-only by default | A 1000-span ring provides request/tool/upstream correlation without creating another unbounded or sensitive persistence surface; durable export can be added later behind an explicit OpenTelemetry endpoint. |
| Enforce correctness-first lint/type scope now | Ruff correctness rules run across the whole tree; Mypy initially covers the newly added security/operations modules so CI is immediately green and meaningful while legacy module splitting expands typed coverage incrementally. |
| Keep Assistants/Threads minimal until a real lifecycle requirement exists | The current create-only compatibility response is truthfully disclosed; adding messages/runs/persistence would create a separate multi-tenant state machine without evidence it is required. |
| Keep Web2API and legacy multi-upstream concurrency library-only for now | Their modules/tests remain available, but wiring them would expand SSRF/routing/failure semantics; current docs and capabilities must say they are not on the HTTP request path. |

## Errors Encountered
| Error | Resolution |
|-------|------------|
| First General Admin Config focused run showed duplicate profile IDs unrelated to the edited profile were not rejected | Validate uniqueness across the complete existing profile collection before replacing/appending the submitted profile, not only among entries matching the submitted ID. |
| First Mypy pass on `gateway_admin_security.py` did not narrow two separate `config.get("gateway")` calls | Bind the value once, then narrow the local with `isinstance(..., dict)` so runtime behavior stays identical and the typed boundary is explicit. |
| First Phase 28 local tool acceptance launch was rejected before execution because the wrapper command contained explicit recursive cleanup | Replace shell cleanup with a Python `TemporaryDirectory` and explicit child-process termination so isolation remains automatic without prohibited destructive command text. |
| First isolated Phase 28 tool acceptance returned HTTP 400 on `Tree` | The production-safe default correctly classifies filesystem tools as downstream-owned; rerun only the trusted-local acceptance harness with `GATEWAY_EXECUTE_USER_SIDE_TOOLS=1`, matching the documented opt-in instead of weakening the default. |
| Second isolated Phase 28 tool acceptance reached WebFetch but rejected its loopback fixture | Preserve the production SSRF default and enable `GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS=1` only inside the isolated acceptance process, because its HTTP fixture intentionally binds to localhost. |
| First F821 cleanup patch assumed `gateway_mcp.py` imported only `Any`; the live import also includes `Callable`, so the atomic multi-file patch failed | Re-read each exact import/call block and apply separate narrow patches. |
| First enforced Ruff gate found four pre-existing F821 names hidden by runtime paths/postponed annotations | Remove the dead legacy renderer's undefined call and add explicit `TYPE_CHECKING`/test type declarations rather than suppressing the correctness rule. |
| Request-ID correlation patch used a multi-file context that did not exactly match the live one-line `_write_request_log` signature | Re-read the exact logging/proxy/test locations and apply independent narrow patches. |
| Full Phase 28 suite exposed a low-probability `database is locked` when eight threads concurrently switched a first-open database to WAL | Centralize journal-mode configuration behind bounded lock retry/backoff and migrate every Gateway SQLite initializer instead of weakening the concurrency regression. |
| Shared-admission hardening made the rate-limit unavailable-path test receive `SQLiteSecurityError` instead of its established `OSError` contract | Make the specific security exception inherit `OSError`, preserving fail-closed detail and backward-compatible backend failure classification. |
| First Phase 28 timeout regression completed the small VACUUM before SQLite reached the 10,000-opcode progress callback | Add a pre-execution deadline check and tighten the progress-handler interval so already-expired and in-flight deadlines are both enforced. |
| Phase 27 dry-run semantics patch combined two non-adjacent contexts and failed atomically | Re-read the maintenance module and split runtime-result semantics, failure-state helper, app wiring, and tests into separate patches. |
| First Phase 27 SQLite-hardening patch failed atomically because the `gateway_agent_planner.py` import context expected a stale `typing.Iterable` import | Re-read the exact live imports and connection sites, then split the helper creation and each module migration into small independently verifiable patches. |
| Targeted pytest `-k` filtered every supplied file | Run core files unfiltered and run the gateway subset separately. |
| Initial transport patch context was stale because shared-worktree edits landed concurrently | Re-read the live diff and continue from the newer transport implementation without overwriting it. |
| First fan-out ordering regression fake matched synthesis text because it searched for a non-anchored chunk label | Match only prompts that start with the partial chunk label; synthesis embeds source-index labels intentionally. |
| Phase 10 focused tests passed but full pytest exposed nine legacy adapter tests running under the newly aligned strict-planner default | Keep the production strict default and make tests that specifically exercise non-strict legacy adapter/protocol behavior opt out explicitly. |
| First readiness diagnostic shell command was rejected because its cleanup used a prohibited `rm -rf` pattern | Use a directly managed gateway process/session and separate curl probe without destructive shell cleanup. |
| Local real-tool acceptance initially saw only an anonymous empty workspace | The production runtime correctly refuses to fall back to the Gateway checkout; update the trusted local acceptance client to send its workspace explicitly on every direct tool request. |
| Second local smoke reached the correct workspace but the bounded root Tree was exhausted by a large `.gateway_runtime` before reaching `src/` | Scope the acceptance Tree probe to `src/`; keep the runtime output bound and test the intended product-code discovery directly. |
| Post-smoke artifact scan found `gateway.client_snippet_api_key` in plaintext in script-generated config | Route script config generation through canonical encrypted atomic `save_config`, propagate the encryption runtime directory, and remove plaintext API keys from launchd plist environment entries. |
| Post-encryption smoke's second unittest reused stale Python bytecode after equal-length same-second source and test edits | Make both acceptance edits change file size as well as semantics so Python's timestamp/size pyc cache cannot mask either update. |
| Phase 15 temporary reproduction imported `gateway` from the `src` package, but `src.__init__` does not export that facade | Reuse the test suite's explicit module import pattern after inspecting its imports; do not repeat the failing import. |
| Second Phase 15 reproduction reached the public endpoint but cloud-mode policy rejected direct user-side execution | Enable `execute_user_side_tools_in_gateway` only in the temporary test config, matching existing trusted local acceptance tests. |
| Phase 15 findings patch used a stale section header and failed context verification | Re-read the live planning files and apply a narrower patch against current text. |
| Phase 16 focused test found `NameError: time is not defined` in the new unified failure helper | Add the missing module import, then update retry-contract tests before re-running; the failure occurred before product verification. |
| Phase 16 background-exec cache patch used an incorrect live global-block context | Re-read the exact exec-session globals and apply smaller patches to current source. |
| Phase 16 local tool acceptance connected to the pre-existing `127.0.0.1:8885` service and received readiness 503 before tool checks | Inspect the acceptance launcher and run an isolated managed Gateway child on a separate port rather than relying on external process state. |
| Production Compose validation refused to render without required upstream/downstream variables | Re-run with process-local dummy contract values; do not persist credentials or weaken required-variable guards. |
| Second Compose render used `ADMIN_PASSWORD`, but the production contract requires `GATEWAY_ADMIN_PASSWORD` | Use the exact Compose variable name on the final attempt and retain the strict guard. |
| Phase 17 write_stdin terminal test assumed process exit was observable in the same call that drained its final output | Respect asynchronous poll semantics: if the first interaction still reports running, poll again after a short delay and assert the eventual non-zero terminal result. |
| Phase 18 encryption tests import `gateway_encryption` as a top-level legacy module, so the new relative file-ops import failed collection | Add the repository's package/script dual-import fallback and re-run the same test selection. |
| First live two-process rate-limit test used `/v1/models`, so accepted requests continued to the intentionally unreachable fake upstream and returned 502 | Keep the shared-quota assertion but use the Gateway-owned calculator endpoint so accepted requests deterministically return 200 before the third request returns 429. |
| Initial Phase 23 regression lost complete-JSON Chat `message.content` and treated adapter-encoded tools as unauthorized | Teach the accumulator to preserve complete Chat messages and carry tool authority from the original caller request, not only the converted upstream body. |
| First true-stream timing test found native mode attached all builtins to plain chat; the tool-round test declared `calculator`, correctly making it downstream-owned | Keep plain native chat tool-free unless the client authorizes implicit tools and the user has tool intent; test Gateway-owned execution through that implicit capability rather than overriding private-function ownership. |
| Disconnect regression showed `BrokenPipeError` closed the upstream iterator but was then mapped as an upstream SSE error | Re-raise client disconnect/reset exceptions through the scoped loop so the outer HTTP boundary performs silent cleanup without a futile second write. |
| Phase 23 full suite found two empty-message schema tests conflicting with explicit implicit-tool authorization, plus a first-start SQLite WAL lock race | Make schema tests carry client capability/tool intent and add bounded cross-process retry around SQLite initialization pragmas/schema creation. |
| First Phase 25 release-gate shell line ended with cleanup `|| true`, which could mask an earlier failure | Re-ran the complete gate without the masking suffix; every command returned exit 0. |
| A truncated long-output view appeared to show a duplicated `args.key` in `smoke_gateway_tools.py` | Re-read the exact numbered source before editing; the live call signature was already correct, so no speculative patch was applied. |
| First Phase 26 worker migration patch expected the Git subprocess block without its live `check=False` argument | The patch failed atomically; re-read the exact numbered source and split the migration into smaller current-context patches. |
| Worker setup-mapping test assumed an extremely large `RLIMIT_NOFILE` would exceed the host hard limit | Darwin reports the hard limit as `RLIM_INFINITY`, so the setting legitimately succeeded. Keep real NOFILE enforcement coverage and inject an invalid worker contract for deterministic setup-error mapping. |
| MCP worker-crash regression received a raw `EOFError` instead of the canonical tool error | Normalize MCP initialization/request transport failures to `ToolExecutionError`, preserve sandbox setup classification, and keep the failed session closed and non-reusable. |
| First isolated local tool acceptance kept the production private-network deny default, so its intentional localhost WebFetch fixture was rejected | Keep the secure product default and rerun only the isolated acceptance process with `GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS=1`; the first process and temporary directory were cleaned. |
| First apply-patch overlay regressions retained a print-only environment probe and a pre-overlay rollback expectation | Make the apply-patch probe produce its declared file, and move rollback-failure coverage to the commit phase; a failed overlay command now correctly leaves the original workspace untouched without rollback. |
| Official Linux Landlock documentation search returned HTTP 404 from the configured web search endpoint | Use the distribution's official Linux UAPI header plus runtime ABI probing as the implementation authority; do not guess constants or weaken the existing container security model. |
| Bubblewrap failed as the non-root production user because default Docker disallows unprivileged namespace creation | Do not add `SYS_ADMIN`/privileged mode; investigate unprivileged Landlock, which is allowed by the current container kernel and reports ABI 8. |
| macOS restricted-read Git probe aborted when sandbox-exec had to resolve a relative PATH executable after entering the profile | Resolve argv[0] to an absolute executable before applying the OS sandbox and include that concrete dependency in the read allowlist. |
| macOS sandbox-exec SIGABRTs when a restricted-read profile directly launches a shebang script | Parse the shebang before entering the sandbox and explicitly execute the interpreter with the script path and original arguments; retain the script in the read allowlist. |
| The configured Codex apply_patch wrapper tried to write `~/.codex/state_5.sqlite` and was denied by the new write scope | Give each patch job an isolated temporary `CODEX_HOME`; do not grant the executable access to the real Codex state or credentials. |
