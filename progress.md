# Progress Log

## 2026-07-17 — Phase 19 durable path mutations and transactional patch safety
- Opened the next stability iteration for CreateDirectory/DeletePath/MovePath/apply_patch.
- Current focus: remove path TOCTOU races, fsync directory mutations, reject patch escapes before execution, and rollback partial patch failures/timeouts.
- Alarm/reminder and real-upstream validation remain excluded; no commit will be created.
- Isolated real-binary reproduction proved `apply_patch` accepts `../outside.txt` and writes outside the workspace with exit 0. Treating patch target prevalidation as a P0 confinement fix.
- Added ordered multi-path locks and durable CreateDirectory/DeletePath/MovePath helpers.
- Added patch target parsing/confinement, symlink rejection, snapshots, rollback, mode restoration, success directory fsync, and explicit rollback-failure reporting.
- Added adversarial/concurrency regressions; focused patch/path gate passed 13 tests.
- Routed apply_patch through bounded-output process-group execution with threaded stdin; combined focused gate passed 14 tests.
- Added and passed real `Move to` patch coverage; compileall and diff-check remained clean.
- Full suite passed **1131 passed, 2 skipped in 67.43s**.
- Managed local real-tool acceptance passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with patch confinement/rollback active.
- Both Compose renders, 19 deployment/config/script tests, compileall, and diff-check pass. No alarm/reminder or real-upstream validation was performed; no commit was created; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`.

## 2026-07-17 — Phase 18 atomic and concurrent workspace writes
- Opened the next stability iteration for canonical local file mutations.
- Current focus: prevent partial files and lost concurrent edits through shared locking and fsync-backed atomic replacement while preserving existing metadata where possible.
- Alarm/reminder and real-upstream validation remain excluded; no commit will be created.
- Inventory confirmed six canonical in-place writers plus Admin skill, log export, legacy encryption, and computer-use image outputs. A shared reusable atomic-write layer is preferable to duplicating safety logic inside one module.
- Added `src/gateway_file_ops.py` with in-process stripes, cross-process advisory locks, metadata-preserving fsync-backed atomic replacement, text updates, and atomic copy.
- Migrated canonical Write/Edit/MultiEdit/RegexEdit/CopyPath/NotebookEdit; 14 existing file/workspace/notebook compatibility tests pass.
- Added `tests/test_gateway_file_ops.py` and canonical concurrent Edit coverage for mode, privacy, replace failure, temp cleanup, threaded increments, and cross-process locking.
- Atomic/file/workspace focused selection passes **19 tests**.
- Migrated Admin skill files, JSON fallback stats/JSONL logs, encryption migration, and image-generation outputs to the shared atomic layer.
- First expanded run stopped during collection because legacy top-level `gateway_encryption` could not resolve a relative import; added package/script fallback rather than weakening tests.
- Package/script encryption compatibility restored; 43 expanded tests passed.
- Added concurrent JSON fallback stats and JSONL append regressions; 49 tests, compileall, and diff-check pass.
- Final source review identified a pre-existing Admin `SKILL.md` symlink redirection risk that must be closed before completing the atomic-write phase.
- Added final-component and lock-time allowed-root checks for Admin skill writes; symlink escape regressions pass.
- Direct-write rescan confirms all ordinary replacement paths now use canonical/config/shared atomic writers; 51 focused tests and diff-check pass.
- Replaced racy encryption-key first creation with shared-lock atomic create-if-absent; added 50-way concurrency, 0600, single-winner, and invalid-key fail-closed tests.
- Atomic/encryption/runtime focused gate passes **39 tests**.
- Full suite passed **1126 passed, 2 skipped in 64.61s**.
- Managed local real-tool acceptance passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with atomic workspace writes active.
- Both Compose renders, 19 deployment/config/script tests, compileall, and diff-check pass. No alarm/reminder or real-upstream validation was performed; no commit was created; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`.

## 2026-07-17 — Phase 17 process output and exec-session reliability
- Opened the next stability iteration after Phase 16 completion.
- Current audit targets true in-memory output bounds, timeout process-group cleanup, and non-zero terminal semantics for long-lived exec sessions.
- Alarm/reminder and real-upstream validation remain excluded; no commit will be created.
- Live source confirms post-hoc truncation still permits unbounded capture memory, long-lived exec terminal non-zero exits are reported as success, and shell descendants are not reliably terminated by timeout/reaper cleanup.
- Implemented bounded concurrent stdout/stderr drain, dedicated process groups, full-group termination, partial timeout output, and canonical non-zero exec-session terminal results.
- Existing process/session/cache focused selection compiles and passes 7 tests before new adversarial regressions.
- First adversarial run passed output-flood, timeout-child, immediate-exit, and exec_wait cases but one write_stdin assertion raced process completion; updated the test to poll the asynchronous terminal state rather than require same-call completion.
- Hardened process-group cleanup against already-exited shell leaders and added a successful-command background-child escape regression.
- Final focused Phase 17 adversarial gate passes **28 tests**.
- Full suite passed **1113 passed, 2 skipped in 66.28s**.
- Managed local real-tool acceptance again passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with the bounded process runtime active.
- Both Compose renders, 19 deployment/config/script tests, compileall, and diff-check pass. No alarm/reminder or real-upstream validation was performed; no commit was created; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`.

## 2026-07-17 — Phase 16 tool outcome and cache-coherence corrections
- Resumed the persistent optimization goal and opened an implementation phase for the Phase 15 confirmed defects.
- Planned canonical process-outcome semantics, retry safety, scope-aware cache invalidation, regression coverage, and full verification.
- Alarm/reminder validation remains explicitly excluded; no calendar/role modules exist in this Gateway repository.
- Finalized the implementation contract: retryable errors are explicit and default false; non-zero process output is preserved as failure; write/code tools invalidate exact workspace/runtime scope on every terminal path; persistent schema gains scope columns and deletes legacy unscoped cache rows during migration.
- First focused run compiled successfully but exposed four failure-path test errors caused by a missing module-level `time` import in the new telemetry helper; cache/persistence tests otherwise reached execution. The next patch fixes the import and updates old implicit-retry expectations to the explicit retryable contract.
- Added the missing time import and updated retry tests so only explicitly retryable transient errors repeat; permanent failures now assert one attempt and retry_count=0.
- Added `tests/test_tool_runtime_stability.py` plus persistence/telemetry regressions for all reproduced failure and cache-coherence paths.
- Focused compile and 99-test runtime/cache/persistence/compatibility gate passed.
- Added safe HTTP Action retry classification and regression coverage: idempotent GET recovered from one 503, while POST with a retry budget executed only once.
- Expanded impacted-path selection passed **380 tests, 272 deselected in 20.60s**.
- Identified and scheduled a cache bypass while scoped background exec sessions remain active, closing the mutation window between start/interact/wait calls.
- Implemented scoped active-exec detection and bypassed read-tool caching while a background process can still mutate files; the new phase-one/phase-two regression and 22-test core gate pass.
- Added a legacy v4-to-v5 persistence migration regression requiring scope columns and removal of opaque legacy rows.
- Full pytest passed **1107 tests, 2 skipped in 57.22s**.
- Initial local acceptance depended on an external 8885 service and hit readiness 503; a managed isolated Gateway child then passed both direct real-tool and native two-round orchestration acceptance checks.
- First production Compose render correctly refused missing required variables; final deployment validation will use process-local dummy values without writing secrets.
- Second Compose attempt used the wrong admin variable alias (`ADMIN_PASSWORD`); final attempt will use required `GATEWAY_ADMIN_PASSWORD`.
- Production/development Compose rendering, 19 deployment/config/script tests, compileall, and diff-check passed with process-local contract values.
- Tightened the tool-cache confidentiality default: local workspace results are memory-only unless `GATEWAY_TOOL_CACHE_PERSIST_LOCAL_RESULTS=1`; added synchronized Python/JSON/YAML/Compose/.env defaults and persistence behavior coverage.
- Post-hardening focused gate passed **85 tests**; final full suite passed **1108 passed, 2 skipped in 55.63s**.
- Repeated managed local real-tool acceptance passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with the secure default active.
- Final Compose render, compileall, and diff-check pass. No alarm/reminder or real-upstream validation was performed; no commit was created; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`.
- First background-exec patch was rejected before changing files because the live exec-session global block differed from the assumed context; re-reading current source before retrying.

## 2026-07-17 — Phase 15 incremental tool-runtime stability audit
- Restored the persistent planning context and started a new post-completion audit phase.
- Current focus: canonical non-zero process outcome semantics and workspace-scoped cache invalidation after mutating tools.
- Preserving the dirty worktree, creating no commits, and excluding alarm/reminder validation.
- Live-source review confirmed both suspected correctness gaps: non-zero Bash/code-interpreter returns are marked successful, and mutating tools do not invalidate memory or persistent read-tool cache entries for their workspace/runtime scope.
- Enumerated the complete mutation-capable built-in surface, including file tools, patches/notebooks, one-shot shell/code execution, and long-lived exec sessions; path-only invalidation is insufficient for directory/query caches.
- First temporary reproduction attempt stopped before execution because `from src import gateway` is not a supported import; next attempt will use the repository's test import pattern.
- Second attempt used the correct facade and was safely rejected by the cloud-mode ownership guard before any tool ran; the final temporary config must explicitly opt into Gateway-owned execution.
- Final temporary reproduction enabled trusted Gateway-owned execution in an isolated workspace/database and confirmed: non-zero Bash/code results are exposed as protocol success; Read and Glob remain stale after Edit/Write; persisted stale Read survives cache-object recreation.
- Test/source audit found that current invalidation coverage is memory-only, persistent lookup precedes memory, all ToolExecutionError values are retried without transient/idempotency classification, and unexpected exceptions bypass failure telemetry.
- Identified a confidentiality concern: default persistent caching stores Read/Grep result bodies in plaintext SQLite/WAL despite short logical TTLs.
- Focused cache/persistence/tool-trace/compatibility verification passed 93 tests; compileall and diff-check passed, confirming existing tests do not cover the reproduced failures.
- Reviewed enhancement boundaries: orchestrated SSE buffers non-streaming upstream responses, rate limits are process-local, multi-upstream/Web2API remain explicitly unintegrated, and automated CI/type/lint gates are absent.
- Full pytest baseline passed: **1098 passed, 2 skipped in 61.95s**. No alarm/reminder validation or real-upstream call was performed.
- Completed Phase 15 analysis with prioritized defect and enhancement designs; product source was not modified in this analysis-only turn, no commit was created, and HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`.

## 2026-07-17 — Script-secret hardening and repeatable local acceptance
- Local acceptance exposed that `mimo_gateway.sh` bypassed canonical config persistence and stored `gateway.client_snippet_api_key` in plaintext; launchd plist generation also embedded upstream/downstream API keys.
- Script config generation now uses encrypted atomic `save_config`, exports/persists the correct encryption runtime directory, and omits API keys from launchd plist environment entries. `claude_m1.sh` reads config through canonical decryption.
- Added script regression coverage for encrypted downstream/upstream values, mode 0600, decryptability, and plist secret omission; 27 focused script/config/encryption tests passed.
- Updated the trusted local acceptance client to pass workspace identity explicitly and removed Python same-second `.pyc` flakiness from its edit/test loop.
- Final repeated local tool acceptance passed, and binary/text scan of its config/log/SQLite/runtime artifacts found no credential markers.
- Post-change full suite: **1098 passed, 2 skipped in 59.61s**; Compose rendering, compileall, diff-check, and workspace secret-pattern scan pass; no smoke artifacts remain.

## 2026-07-17 — Phases 13–14 completed
- Final full suite: **1098 passed, 2 skipped in 59.07s**, with marker/asyncio warnings eliminated by explicit pytest configuration.
- Deployment verification: both Compose files pass `docker compose config -q`; Nginx passes `nginx -t` with a temporary certificate and Gateway DNS mapping.
- Runtime verification: real child Gateway returned ready/live and exited cleanly on SIGTERM; controlled real-upstream request returned a valid response with non-empty text without printing or persisting the credential.
- Secret audit found a short credential previously stored verbatim as a downstream display prefix plus historical log/audit remnants. Prefixes now use an irreversible SHA-256 fingerprint; runtime config was re-saved encrypted; SQLite rows/WAL and text artifacts were redacted. Final workspace exact-secret scan returned zero matches.
- Final source audit confirmed every required transport/config/identity/fan-out/cache/error/package/deployment/lifecycle/rate/capability control is present.
- Git HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`; no commit was created; `git diff --check` passes.

## 2026-07-17 — Full regression gate after Phase 10-12 convergence
- Synchronized legacy adapter tests with the deliberate strict-planner production default without weakening that default.
- Revalidated child-process readiness/SIGTERM and exec-session reaper behavior after concurrent hardening edits.
- Current full suite is green: **1097 passed, 2 skipped in 55.68s**, with pytest configuration warnings eliminated.
- `compileall`, focused 210-test security/correctness suite, deployment contracts, and `git diff --check` pass.
- Phase 13 comprehensive verification remains active for Compose rendering, local service smoke, controlled real-upstream smoke, and final secret/artifact audit.

## Verification issue: Compose PID limit syntax
- `docker compose config -q` rejected simultaneous/top-level PID normalization as distinct from deploy resource limits in the installed Compose version.
- Resolution: keep a single `deploy.resources.limits.pids: 256` contract and update the deployment assertion.

## 2026-07-17 — Phase 12 completed
- Wired configured per-client rate limiting into authenticated Python GET/POST API paths using stable client identities and HTTP 429 responses.
- Added `/capabilities` with explicit support levels and truthful disclosure of minimal Assistants/Threads compatibility and non-integrated concurrency/Web2API modules.
- Added Admin-authenticated Prometheus-text counters for requests, rate-limit state/rejections, and readiness.
- Synchronized rate-limit defaults across runtime, templates, environment example, and both Compose variants.
- Verification: rate/capability tests and config/deployment contract tests passed; compileall and diff-check passed.

## 2026-07-17 — Phase 11 completed
- Added `/livez` and readiness-aware `/readyz`; `/healthz` remains a readiness-compatible alias. Readiness is withdrawn before shutdown.
- SIGTERM/SIGINT now drive graceful HTTP shutdown and cleanup of MCP sessions, exec/agent/team runtime sessions, persistence, and maintenance activity.
- Added recurring expired semantic/tool-cache and statistics-retention cleanup.
- Bounded shell/code output and added lazy exec-session TTL reaping plus shutdown termination.
- Production image runs as a non-root user. Production Compose uses a read-only root filesystem, drops all capabilities, enables no-new-privileges, limits PIDs, and supplies a bounded tmpfs.
- Bundled Nginx now requires TLS certificate/key files, redirects HTTP to HTTPS, enables TLS 1.2/1.3 and HSTS, and exposes liveness/readiness proxy routes.
- Verification: 8 deployment/process tests passed, including a real child Gateway readiness check and SIGTERM exit; 19 focused hardening tests and 4 strict-default compatibility regressions passed.

## 2026-07-17 — Phase 10 completed
- Fan-out partials are synthesized in source order even when workers finish out of order. Metadata exposes successful/failed chunks and source truncation/omitted characters.
- Quality review explicitly requests a corrected user-ready final answer and preserves supported response protocol shapes.
- Semantic response caching now exact-matches a complete canonical request fingerprint rather than keying only the last user message.
- Malformed JSON, invalid UTF-8, and non-object top-level bodies now raise HTTP 400 errors.
- Marketplace imports are package-relative; package-mode startup succeeds.
- The Claude compatibility executor delegates to the canonical built-in registry and its workspace/write/shell/network controls.
- Verification: 197 compatibility/edge/stability tests passed; 53 cache/fan-out/marketplace tests passed; package smoke, compileall, and diff-check passed.

## 2026-07-17 — Phase 10 fan-out correctness batch
- Fan-out partial results are now restored to source-chunk order after concurrent completion and retain original chunk labels in the synthesis prompt.
- Capped fan-out now reports truncation, processed/omitted source characters, successful chunks, and failed chunk indexes in `gateway_context`.
- Quality review now explicitly requests a user-ready rewritten final answer instead of critique, and reviewed text is replaced consistently for Chat/Anthropic/simple Responses shapes.
- Added focused regressions for out-of-order workers, capped-source disclosure, and Anthropic review replacement.
- Verification: 13 focused fan-out/context tests passed; py_compile and diff-check passed.

## 2026-07-17 — Phase 10 request/cache contract batch
- Malformed UTF-8/JSON, top-level array/scalar JSON, invalid Content-Length, and incomplete bodies now use BadRequestError/HTTP 400 semantics.
- Added end-to-end HTTP regressions for malformed/non-object JSON and invalid Content-Length.
- Verified all live Marketplace imports are package-relative and added a package-mode import contract.
- Canonical semantic-cache fingerprints now bind path, model, system/instructions, complete history, and generation fields while ignoring only non-streaming-path `stream`; fingerprint entries require exact cache matches and skip embeddings.
- Verification: 5 focused HTTP/package/cache tests passed; py_compile and diff-check passed.

## 2026-07-17 — Phase 10 canonical Claude execution batch
- Removed the duplicate Claude compatibility implementations for Read/Write/Edit/Bash/Glob/Grep/WebFetch/WebSearch and their separate path/shell/SSRF policies.
- `execute_claude_code_tool` now constructs a canonical ToolCall and delegates through the main workspace scope and tool runtime, including client permission identity, cache scope, retries, audit, and statistics.
- Retained only protocol adaptation for zero-based Read offsets and millisecond Bash timeouts.
- Added an explicit delegation contract test.
- Verification: 35 Claude compatibility tests passed; py_compile and diff-check passed; no duplicate compatibility executors remain.

## 2026-07-17 — Phase 9 completed
- Config persistence now uses a process lock, revision conflicts, mode-0600 atomic replacement, file and directory fsync, and fail-closed encryption by default.
- Admin passwords now use PBKDF2-SHA256 (600,000 iterations) with legacy SHA-256 verification and successful-login migration; verification iteration counts are bounded.
- Cached only the environment/default Admin verifier so normal config merges do not repeatedly pay the PBKDF2 cost. Focused identity/config tests dropped from minutes to seconds.
- Downstream keys now receive stable IDs; HTTP auth returns that ID, and permission maps migrate aliases keyed by legacy display name or key hash. Enabled permissions default to deny.
- Runtime defaults now match JSON/YAML/environment/Compose contracts: 1,048,576 input tokens, 131,072 output tokens, 10 tool rounds, strict planner enabled, and 120,000-token fan-out chunks.
- Added regression coverage for slow/legacy password verification, stable ID rename behavior, legacy permission aliases, 0600 writes, stale revision conflicts, encryption failure preservation, missing crypto, wrong keys, and template/Compose drift.
- Verification: 58 focused config/encryption/permission tests passed; 5 focused Gateway compatibility tests passed; compileall and diff-check passed.

## Session: 2026-07-17 Follow-up Current-State Audit

### Current Status
- **Phase:** 9 - Configuration, Identity & Secret Safety

### Actions Taken
- Restored the persistent plan and re-read the live dirty worktree rather than relying on the earlier snapshot.
- Verified current config atomicity, encryption, stable-client identity, permission defaults, response fan-out/cache, request parsing, runtime lifecycle, and deployment hardening surfaces.
- Preserved concurrent edits and re-ran initially failing cases after the live source changed during the audit.
- Ran syntax compilation, focused security/config/permission/transport tests, and the full pytest suite.

### Test Results
| Test | Actual | Status |
|------|--------|--------|
| `py_compile` on current critical modules | Exit 0 | PASS |
| Focused encryption/permission/config/proxy/deployment suite | 67 passed, 1 permission-contract failure | PARTIAL |
| Re-run of six initially failing focused cases after concurrent updates | 4 passed, 2 failed | PARTIAL; identity test was subsequently updated |
| Current full `pytest -q` | 1069 passed, 2 skipped, 1 failed, 21 warnings in 60.83s | PARTIAL |

### Errors
| Error | Resolution |
|-------|------------|
| First full run loaded source before concurrent PBKDF2 caching/test updates and took 423.68s before interruption with 5 failures | Re-read the live source and re-ran focused/full tests; current run is 60.83s with one remaining permission expectation mismatch. |

## Session: 2026-07-17 Full Correction

### Current Status
- **Phase:** 9 - Configuration, Identity & Secret Safety
- **Started:** 2026-07-17

### Actions Taken
- Restored the audit/roadmap context and confirmed the persistent goal is active.
- Expanded the plan from analysis into seven implementation and completion-audit phases.
- Confirmed the user prohibits Git commits and authorized controlled real-upstream testing.
- Implemented robust curl transport failure/status/header handling and correct 502/504 mappings.
- Replaced the ineffective urllib-only retry loop with bounded retries for active Gateway exceptions, exponential jittered delays, Retry-After support, and attempt deadlines.
- Added nine focused proxy regression tests (11 total in the file).
- Verified the real curl subprocess path against a local HTTP server: two 503 responses followed by 200 produced exactly three attempts and a valid result.
- Completed Phase 8 and moved to configuration, identity, and secret-safety corrections.
- Re-read the current proxy/error implementation and all direct proxy tests, then defined a backward-compatible bounded retry/error contract.
- Detected and preserved concurrent shared-worktree transport edits; reviewed their diff and identified missing regression coverage plus curl argv credential exposure.
- Removed upstream credentials from curl argv by passing bounded header configuration on stdin; added malformed-status, argv-secret, and retry-deadline tests.
- Added retry controls to JSON/YAML templates, `.env.example`, and both Compose environments, then verified the focused 24-test suite.

### Test Results
| Test | Expected | Actual | Status |
|------|----------|--------|--------|
| `tests/test_gateway_proxy_errors.py` | New transport contract passes | 11 passed | PASS |
| Proxy/upstream/retry/config focused suite | No regressions | 112 passed, 262 deselected | PASS |
| `compileall` + `git diff --check` | Clean | Exit 0 | PASS |
| Local real-curl retry integration | Two 503s then 200 | 3 calls, final `ok` | PASS |
| Dedicated proxy suite after credential hardening | All transport/retry/security cases pass | 14 passed | PASS |
| Proxy/config/deployment/script suite | Related contracts pass | 24 passed | PASS |
| py_compile + JSON validation + diff check | Clean | Exit 0 | PASS |

### Errors
| Error | Resolution |
|-------|------------|

## Session: 2026-07-17

### Current Status
- **Phase:** Complete
- **Started:** 2026-07-17

### Actions Taken
- Restored the completed 2026-07-16 audit context and confirmed no unsynced planning context.
- Added revalidation and enhancement-roadmap phases for the follow-up request.
- Compared HEAD, worktree status, diff statistics, and relevant file diffs with the previous audit; no new source changes were found.
- Re-read the active proxy, deployment, config, fan-out, permission, and compatibility-executor paths; all previously prioritized defects remain in current source.
- Audited production reachability of concurrency, statistics, Web2API, compatibility, persistence, intelligence, headroom, and assistants modules; identified multiple tested-but-not-integrated capability islands.
- Reviewed rate limiting, health/readiness, shutdown lifecycle, config-write concurrency, and database retention; identified additional production-hardening gaps.
- Re-ran syntax compilation successfully and reconfirmed the proxy/default defects with an isolated script.
- Initial targeted pytest selection passed 5 tests but unintentionally deselected 396 because the global `-k` filter also applied to the core files; scheduled a corrected unfiltered run.
- Corrected tests passed: 43 unfiltered proxy/deployment/permission/config tests and 5 filtered gateway tests; warnings remain limited to pytest-asyncio configuration.
- Reviewed semantic cache identity and Assistants/Threads reachability; found under-keyed cache correctness risk and an advertised-but-minimal compatibility surface.
- Added container privilege/sandbox hardening to the production enhancement set after verifying the image runs without a non-root user or capability restrictions.
- Prioritized unresolved defects and optional enhancements into four implementation batches; final follow-up report prepared.
- Completed current-state revalidation and began prioritizing the enhancement roadmap.

### Test Results
| Test | Expected | Actual | Status |
|------|----------|--------|--------|
| `python3 -m compileall -q src tests` | All modules compile | Exit 0 | PASS |
| Initial combined targeted pytest | Run all core files plus filtered gateway tests | 5 passed, 396 deselected due global `-k` | PARTIAL; corrected below |
| Curl HTTP-000 reproduction | Raise transport error | Returned `{}` | CONFIRMED DEFECT |
| 503 retry reproduction | Multiple attempts | 1 attempt | CONFIRMED DEFECT |
| Runtime default check | Match templates | false / 5 / 24000 | CONFIRMED DRIFT |
| Corrected core targeted suite | All selected tests pass | 43 passed | PASS |
| Gateway fan-out/marketplace/malformed subset | All matching tests pass | 5 passed, 353 deselected | PASS |

### Errors
| Error | Resolution |
|-------|------------|

## Session: 2026-07-16

### Current Status
- **Phase:** Complete
- **Started:** 2026-07-16

### Actions Taken
- Loaded the `planning-with-files` workflow and initialized analysis artifacts.
- Inventoried repository files and current git status.
- Confirmed the worktree has pre-existing changes; no product code has been modified.
- Read repository guidance, the main README, dependency list, and primary YAML configuration.
- Recorded the stated architecture, protocol scope, ownership boundary, and security-sensitive defaults.
- Measured module/test sizes and inventoried top-level classes/functions/import relationships.
- Read the startup facade, primary HTTP handler boundaries, upstream proxy, and error model.
- Identified concrete active-path reliability issues in curl transport/retry handling and stale entrypoint documentation.
- Traced HTTP authentication, request sizing/parsing, cache eligibility, streaming/non-streaming dispatch, admin mutation routes, and error mapping.
- Traced non-streaming orchestration, workspace scoping, planner/context preparation, protocol conversions, tool ownership/delegation, permission checking, result caching, and retry behavior.
- Reviewed protocol normalization/privacy stripping, text adapter construction, persistence/logging schemas, and semantic cache implementation.
- Computed internal dependency/long-function hotspots and reviewed streaming, context strategy boundaries, MCP process handling, and HTTP Action SSRF controls.
- Reviewed deterministic Agent Planner workflows/state and the exact fan-out/synthesis/quality-review implementation; identified ordering, truncation disclosure, and review-output semantics risks.
- Reviewed configuration defaults, secret encryption, permission identity, Docker/Compose/Nginx deployment, and deployment contract tests.
- Audited canonical built-in path/shell controls and compared them with the legacy Claude compatibility executor; found policy duplication and output/process-lifecycle risks.
- Quantified the test suite and compared coverage areas against identified runtime/security risks; found strong breadth but gaps around active proxy transport, identity wiring, fan-out semantics, TLS, and automated quality gates.
- Reviewed the existing uncommitted diff to separate current deployment hardening from baseline architectural/reliability issues.
- Full syntax compilation passed.
- Full pytest passed: 1058 passed, 2 skipped, 21 warnings in 54.15s.
- Ran isolated reproductions confirming curl HTTP-000 success misclassification, missing 503 retries, and package-mode marketplace import failure.
- Compared runtime-generated defaults with committed templates/Compose and found strict-planner/default-window drift.
- Completed executable verification and started final evidence cross-check/delivery.
- Cross-checked conclusions against source, tests, targeted reproductions, and the current dirty-worktree diff; final report prepared.
- Completed quality/risk review and began executable verification.
- Completed architecture/runtime tracing and moved into focused quality/security/deployment review.
- Completed repository discovery and began detailed runtime tracing.

### Test Results
| Test | Expected | Actual | Status |
|------|----------|--------|--------|
| `python3 -m compileall -q src tests` | All modules compile | Exit 0 | PASS |
| `python3 -m pytest -q` | Current suite passes | 1058 passed, 2 skipped, 21 warnings | PASS |
| Curl transport-failure reproduction | Raise transport error | Returned `{}` | CONFIRMED DEFECT |
| 503 retry reproduction | More than one attempt | 1 attempt | CONFIRMED DEFECT |
| Root/package marketplace import | Import local module | `ModuleNotFoundError` | CONFIRMED MODE-SPECIFIC DEFECT |

### Errors
| Error | Resolution |
|-------|------------|
## 2026-07-17 — Phase 20 remaining-risk audit
- Restored the persistent planning context and session catch-up state, then revalidated the dirty worktree, branch, and unchanged HEAD without modifying product code.
- Confirmed remaining runtime gaps in process-local rate/concurrency state, unbounded upstream non-streaming capture, MCP stderr/message/process-tree handling, and synthetic rather than true orchestrated streaming.
- Reconfirmed the intentionally limited Assistants/Threads surface, absence of enforced CI/type/lint gates, and major module-size/cycle hotspots.
- Prepared a risk-first enhancement order: bound upstream/MCP resources, add shared quota state, implement true streaming, then sandbox isolation and modularity/observability work.
- No alarm/reminder functionality was tested, no real-upstream call was made, no credentials were persisted, and no Git commit was created.

## 2026-07-17 — Phase 21 resource-boundary implementation started
- Added shared `gateway_process_ops.py` and migrated the existing bounded Bash/code runner to it without changing tool output semantics.
- Replaced upstream curl's unbounded `capture_output` path with bounded concurrent pipe draining, a curl download ceiling, bounded stderr, and an explicit oversized-response error.
- Added synchronized upstream/MCP byte-limit defaults to Python, JSON, YAML, `.env.example`, and both Compose variants.
- Hardened MCP stdio with continuous bounded stderr draining, inbound/outbound frame limits, failed-session disposal, unbuffered pipes, new process groups, and whole-group shutdown.
- Initial `py_compile`, JSON parsing, and `git diff --check` pass. Focused regressions are next.

## 2026-07-17 — Phase 21 completed
- Added direct regressions for multi-megabyte bounded capture, MCP stderr flood draining, inbound/outbound frame limits, MCP descendant cleanup, curl stderr disclosure, and a real local oversized HTTP response.
- Focused resource and compatibility verification: **59 passed**.
- Full suite: **1139 passed, 2 skipped in 69.65s**.
- Both Compose variants render with process-local dummy values; compileall and `git diff --check` pass.
- Managed local tool acceptance passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`; the isolated runtime directory was removed afterward.
- No real-upstream request, alarm/reminder validation, credential persistence, or Git commit occurred.

## 2026-07-17 — Phase 22 shared rate limiting implementation
- Added `gateway_rate_limit.py` with process-local sliding-window fallback and a cross-process SQLite token bucket.
- Added atomic consumption, restart persistence, continuous refill, state TTL cleanup, persistent monotonic rejection metrics, 0600 database mode, and hashed-only identity storage.
- Wired HTTP enforcement, 429 `Retry-After`, capabilities, and Admin Prometheus metrics to the active backend.
- Synchronized SQLite backend/path/fallback/busy-timeout/TTL defaults across Python, JSON, YAML, `.env.example`, and both Compose files.
- Added spawned multi-process, restart, refill, expiry, privacy, fallback, fail-closed, and HTTP error-contract regressions; focused verification currently passes 19 tests.

## 2026-07-17 — Phase 22 completed
- Corrected the live two-process test to use a Gateway-owned calculator request instead of forwarding accepted `/v1/models` requests to the intentionally unavailable fake upstream.
- Added an actual two-Gateway-process HTTP contract: two requests across different processes return 200 and jointly exhaust a shared RPM=2 bucket; the third returns 429 with `Retry-After` and `backend=sqlite`.
- Added pytest-wide isolation for the default rate-limit database so full tests do not pollute `.gateway_runtime`.
- Focused rate/config/capability verification: **21 passed**.
- Full suite: **1148 passed, 2 skipped in 69.56s**.
- Compileall, `git diff --check`, both Compose renders, and managed local real-tool acceptance pass.
- The isolated runtime directory was deleted; no real upstream, alarm/reminder validation, credential persistence, or Git commit occurred.

## 2026-07-17 — Phase 23 true streaming implementation
- Added `gateway_stream_state.py` with canonical Chat/Anthropic/Responses SSE accumulation, including fragmented tool arguments and complete-response fallback.
- Added `NativeProxyClient.stream()` with stream capability detection, bounded total/event/event-count limits, pre-first-event retries, timeout mapping, backpressure-by-iteration, and deterministic response close.
- Added protocol-specific incremental downstream emitters for Chat choices, Anthropic text/thinking blocks, and multiple Responses output/content items.
- Orchestrated plain safe text now reaches the client before upstream completion; tool-decision rounds stream from upstream into bounded state, execute/delegate tools under existing ownership rules, then continue subsequent rounds.
- Refactored passthrough mode onto the same bounded/cancellable transport and preserved Anthropic complete-tool-input normalization.
- Added real HTTP first-token timing, disconnect cleanup, oversized event, pre-output retry/no-replay, cross-protocol, multi-choice/item, fragmented tool-call, and `supports_streaming=false` fallback regressions.
- Current focused streaming/config suite passes 93 tests; dedicated new streaming suite passes 15 tests after the latest fallback coverage.

## 2026-07-17 — Phase 23 completed
- Full regression initially exposed two empty-message schema tests that no longer met the explicit implicit-tool contract and one concurrent SQLite WAL initialization race.
- Updated schema tests to carry client capability plus tool intent; added bounded exponential retry for cross-process SQLite initialization and ran its 4-process test ten consecutive times successfully.
- Final full suite: **1163 passed, 2 skipped in 72.04s**.
- Compileall, `git diff --check`, both Compose renders, checkout-artifact checks, and managed local real-tool acceptance pass.
- Managed acceptance again passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`; its isolated runtime was deleted.
- No real upstream, alarm/reminder validation, persisted credential, or Git commit was used.

## 2026-07-17 — Phase 24 post-streaming remaining-risk audit
- Revalidated branch `codex/gateway-owned-multitool`, unchanged HEAD `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`, and the existing dirty worktree without modifying product code.
- Confirmed the remaining executable-isolation gap: Shell/code/apply-patch/exec/MCP children share the Gateway namespace and inherit service environment secrets; current path/output/process controls are not a malicious-code sandbox.
- Confirmed an unsafe development-deployment combination: the ordinary Compose file binds all host interfaces while permitting no downstream key and the default Admin password. Production Compose remains loopback/fail-closed.
- Confirmed `gateway.cors_enabled` is not enforced; responses and OPTIONS always emit wildcard CORS.
- Reconfirmed process-local global concurrency, minimal metrics/no OpenTelemetry, missing enforced CI/lint/type/property/soak gates, large module/test hotspots, and intentionally minimal Assistants/Threads semantics.
- Focused current-state verification passed: **50 passed** across process boundaries, shared rate limiting, true streaming, HTTP validation, deployment contracts, and config synchronization.
- `compileall`, `git diff --check`, development Compose rendering, and production Compose rendering with process-only dummy values all passed. Development Compose emitted expected warnings that upstream/downstream variables default to blank, supporting the external-bind/auth finding.
- No real-upstream request, alarm/reminder validation, credential persistence, product-code edit, or Git commit occurred.

## 2026-07-17 — Phase 25 external binding and CORS safety completed
- Added `gateway_http_security.py` with canonical Origin parsing, loopback detection, explicit public-exposure modes, external credential validation, and shared CORS header decisions.
- Direct non-loopback listeners now fail before binding when the effective config uses the default Admin password or has no enabled downstream key. Added real child-process rejection and successful secure-start/SIGTERM tests.
- Ordinary Compose now publishes only `127.0.0.1`, the local launcher defaults to loopback, production declares external exposure, and Nginx/Gateway body limits are aligned at 64 MiB.
- Runtime security environment values now override stale persisted Admin/downstream credential state in memory, so credential rotation works with persistent volumes without persisting plaintext.
- Removed wildcard CORS. CORS defaults disabled and supports only exact HTTP(S) origin allowlists across JSON, text, errors, preflight, and SSE; invalid/`null`/unlisted origins fail closed. Admin writes retain their separate same-origin guard.
- Added synchronized Python/JSON/YAML/environment/Compose defaults, Admin UI fields, deployment documentation, capabilities metadata, and 9 new security regressions.
- Focused final batch: **32 passed**. Full suite: **1172 passed, 2 skipped in 74.94s**.
- Final compileall, `git diff --check`, both Compose renders, and checkout artifact checks passed. A first combined gate used a trailing cleanup `|| true`; it was immediately re-run without masking and passed cleanly.
- No real-upstream request, alarm/reminder validation, credential persistence, Git commit, or alarm/reminder code path was used.

## 2026-07-17 — Phase 26 environment-isolation batch completed
- Added `gateway_sandbox.py` with the versioned `gateway-sandbox-job-v1` job/resource/result/diff contract, command-redacted public serialization, minimal child environment builder, and truthful capability reporting.
- Migrated Shell, code interpreter, long-lived exec sessions, Git, apply_patch, and MCP to positive-allowlist environments. MCP receives only the safe base plus its own explicit Admin-configured environment; upstream curl remains an independent trusted transport process.
- Added `GATEWAY_TOOL_ENV_ALLOWLIST` as an explicit operator escape hatch and synchronized it across `.env.example`, both Compose variants, config contracts, and deployment guidance.
- Added real child-process tests proving Gateway/upstream/OpenAI secrets are absent from Bash, Python, exec, Git, and apply_patch. Added MCP-scoped override, job redaction, capability honesty, and contract-shape tests.
- Focused process/tool/config/deployment verification passed **48 tests**. Full suite passed **1177 tests, 2 skipped in 74.27s**.
- Managed isolated local acceptance passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`; temporary config/runtime/log data was deleted afterward.
- Exact re-read showed a suspected duplicated smoke-script argument was only a truncated-output artifact; no speculative code change was made.
- Phase 26 remains active: filesystem/network namespaces, per-job resource limits, cancellation worker boundary, and overlay diff commit are not yet enforced.

## 2026-07-17 — Current remaining-risk revalidation
- Re-read the persistent Phase 26 plan and current sandbox/process/MCP/capability implementations without modifying product code.
- Reconfirmed that environment sanitization is active while filesystem/network/resource/overlay isolation remains contract-only and truthfully disclosed.
- Ran current focused verification: `compileall`, 32 sandbox/rate-limit/true-streaming/HTTP-security tests, and `git diff --check` all passed.
- Identified a new concrete local-data permission gap: primary logging, persistence, stats, and planner SQLite creators do not enforce mode `0600`; current local files are `0777` or `0644` despite containing request, memory, cache, or planner data.
- Quantified long-running storage growth: `.gateway_runtime` is about 1.6 GiB with 593 top-level directories; `gateway_log.sqlite3` is about 148 MiB and the primary request-log table accounts for about 100 MiB. Existing maintenance does not retain/prune these primary stores or historical runtime directories.
- Revalidated remaining process-local concurrency, limited metrics/trace coverage, silent maintenance failures, absent CI/static/security/dependency-lock gates, large module hotspots, and intentionally minimal Assistants/Threads plus inactive Web2API/concurrency-module surfaces.
- No real-upstream request, alarm/reminder validation, persistent credential, Git commit, or alarm/reminder code path was used.

## 2026-07-17 — Phase 26 worker/resource batch started
- Restored the active Phase 26 plan and inspected the live bounded-process, Shell/code, long-lived exec, Git, apply_patch, and MCP spawn paths.
- Chose a separate stdlib worker that applies rlimits before `exec`, avoiding multithreaded `preexec_fn` hazards while preserving the current process-group cleanup boundary.
- Defined a split policy: default CPU/NOFILE/FSIZE limits for short jobs; no default cumulative CPU limit for long-lived exec/MCP; memory/PID limits remain explicit opt-in until platform-specific behavior is proven.
- Planned explicit worker setup/version errors and a cancellable process-runner path with bounded diagnostic output.

## 2026-07-17 — Phase 26 worker/resource implementation
- Added `gateway_sandbox_worker.py`, a stdlib-only versioned worker that applies POSIX rlimits before `exec` and fails closed with reserved exit 125 on unsupported/invalid policy setup.
- Added short-job CPU/NOFILE/FSIZE defaults, opt-in memory/PID limits, and a separate long-lived policy without a default cumulative CPU ceiling.
- Migrated bounded Shell/code/apply_patch, long-lived exec, Git, and MCP launches through the worker while retaining sanitized environments, bounded output, timeouts, process-group cleanup, and existing tool failure semantics.
- Added explicit process cancellation to the shared bounded runner and proved cancellation removes descendants.
- Added real CPU, file-size, open-file, memory-enforce-or-fail-closed, invalid-version, write-scope, network-policy, worker-crash, and MCP crash/non-reuse regressions.
- MCP crash testing exposed raw `EOFError` leakage; normalized initialization/request failures to `ToolExecutionError` and preserved sandbox setup classification.
- Synchronized worker resource environment controls across `.env.example`, both Compose variants, config-sync tests, capability reporting, and deployment documentation.
- Current focused evidence: 82 related process/tool/config/compatibility tests passed, followed by 20 direct sandbox/process adversarial tests passed.

## 2026-07-17 — Phase 26 apply_patch overlay/diff commit
- Moved external apply_patch execution into a temporary overlay populated only with prevalidated declared targets.
- Added overlay scanning that rejects undeclared regular files, directories, symlinks, and non-regular declared targets.
- Added explicit SHA-256 create/update/delete diff entries, no-op rejection, pre-commit byte/mode conflict detection, atomic per-file commit, parent fsync, and rollback of already committed targets on partial failure.
- Updated capability reporting to distinguish the apply_patch overlay from still-shared Shell/code/exec/MCP filesystem and network namespaces.
- Added regressions for undeclared writes, symlink/write-scope escape, external version conflicts, commit rollback success, rollback failure reporting, worker crashes, MCP crash non-reuse, and real resource limits.
- First isolated acceptance correctly failed because the secure localhost/private-network default blocked its WebFetch fixture; reran only the isolated test process with the documented private-network override and it passed.
- First full worker gate: **1188 passed, 2 skipped in 77.57s**. Final overlay gate: **1191 passed, 2 skipped in 79.05s**.
- Compileall, `git diff --check`, both Compose renders, and repeated isolated local tool acceptance passed. Acceptance reported `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`.
- No real upstream, alarm/reminder validation, persistent credential, Git commit, or alarm/reminder code path was used. Temporary acceptance directories were deleted.

## 2026-07-17 — Phase 26 OS filesystem/network isolation
- Rejected Bubblewrap after a real non-root Docker probe showed default container security blocks its namespace creation; no capability or privilege was added.
- Added automatic macOS sandbox-exec and Linux Landlock backends, with explicit resource-only `rlimit` compatibility mode.
- Added write-scope enforcement for Shell/code/exec/Git/apply_patch/MCP; Git/apply_patch force network denial, Shell/code/exec use the configured policy, and MCP keeps explicit per-server network/write controls.
- Added Linux libseccomp network denial for socket/socketpair/io_uring setup and fail-closed behavior when a requested backend/policy cannot be applied.
- Added Linux restricted read allowlists and macOS targeted secret-path deny rules; denied path metadata is redacted in public job serialization.
- Preserved PATH/symlink/shebang execution semantics and isolated Codex apply_patch state with a temporary per-job `CODEX_HOME`.
- Built `ai-gateway-phase26-sandbox-test` from the current Dockerfile and ran the worker as its actual non-root `gateway` user. The probe returned `secret-read-denied`, `outside-denied`, and `socket-denied` while the declared workspace write succeeded.
- Focused macOS/process/tool/config/deployment verification: **99 passed**.

## 2026-07-17 — Phase 26 completed
- Added concurrent tenant-root isolation and proved two parallel jobs cannot read or write sibling workspaces while preserving their own workspace operations.
- Added a stdlib bundled Codex-style apply_patch fallback and verified Add/Update/Move/Delete behavior through the same overlay/diff/OS-sandbox boundary.
- Built the production image and proved the bundled fallback works as the image's non-root `gateway` user even though no external apply_patch binary is installed.
- Final full suite: **1195 passed, 2 skipped in 78.29s**. Compileall, `git diff --check`, both Compose renders, production image build, Linux Landlock/seccomp probes, macOS sandbox probes, and isolated local real-tool acceptance passed.
- Removed the temporary verification image/build log and confirmed no Phase 26 temporary directories or Gateway/worker processes remain.
- No real upstream, alarm/reminder validation, persistent credential, Git commit, or alarm/reminder code path was used.

## 2026-07-17 — Phase 27 started
- Advanced the persistent plan to restrictive SQLite/runtime permissions, primary-log/memory retention, stale-runtime cleanup, capacity metrics, and observable maintenance failures.
## 2026-07-17 — Phase 27 SQLite permission hardening started
- Logged the previous atomic patch-context failure and replaced it with small live-context patches.
- Added `src/gateway_sqlite.py` and migrated logging, persistence, stats, and planner SQLite connections to restrictive file/runtime handling.
- Added `tests/test_gateway_sqlite_security.py` for `0700` runtime directories, `0600` DB/WAL/SHM files, shared-parent preservation, symlink rejection, permission failure, concurrent initialization, and legacy imports.
- Focused verification: **45 passed in 7.53s**.
- Existing historical runtime data was not deleted or pruned; no real upstream, alarm/reminder validation, credential persistence, or Git commit occurred.

## 2026-07-17 — Phase 27 retention and capacity implementation
- Added bounded dual-limit retention for primary logs/failures/memories and planner sessions/events, plus bounded cache/stat cleanup.
- Added passive WAL checkpoints, incremental vacuum requests, DB/row/space reporting, cumulative maintenance health state, stderr failure reporting, and Prometheus maintenance/capacity gauges.
- Added opt-in allowlisted stale-runtime cleanup with dry-run, entry-count, and per-entry-size safeguards; no existing repository runtime artifact was deleted.
- Synchronized maintenance defaults across Python, JSON, YAML, `.env.example`, and both Compose variants.
- Added retention, row-cap, dry-run, planner, safe-runtime, oversized-entry, and observable-failure regressions. Focused suite: **87 passed in 7.43s**.

## 2026-07-17 — Phase 27 completed
- Corrected SQLite initialization order after a real PRAGMA probe proved incremental auto-vacuum was not active; new databases now verify mode `2` in tests.
- Tightened the existing Gateway runtime directory to `0700` and known SQLite files to `0600` without deleting or rewriting records. Existing legacy files remain auto-vacuum mode `0` pending an explicit operator-controlled compaction design.
- Final focused capacity/security suite: **69 passed**. Final full suite: **1214 passed, 2 skipped in 77.62s**.
- `compileall`, `git diff --check`, development Compose rendering, production Compose rendering with process-only dummy values, and isolated local real-tool acceptance all passed.
- Acceptance again proved `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`. Temporary Gateway/runtime directories and processes were removed.
- Branch remains `codex/gateway-owned-multitool`; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`; no commit, real-upstream request, alarm/reminder validation, or credential persistence occurred.

## 2026-07-17 — Phase 28 legacy compaction audit started
- Restored the current plan/worktree and revalidated the unchanged branch/HEAD while preserving the existing dirty tree.
- Traced request admission, readiness, Admin routes, background maintenance, and logging/persistence/stats/planner connection locks.
- Selected an offline explicit compaction command plus read-only Admin preflight rather than an unsafe online destructive endpoint.

## 2026-07-17 — Phase 28 offline compaction implementation
- Added `gateway_sqlite_compact.py` with read-only preflight, POSIX advisory locking, exclusive SQLite access, disk-headroom checks, timeout cancellation, `VACUUM INTO`, integrity/schema/table-count validation, same-directory atomic replacement, hard-link rollback, ownership preservation, and restrictive modes.
- Added authenticated `/admin/storage.json` preflight and capability disclosure; destructive execution remains CLI-only and requires `--confirm-gateway-stopped`.
- Documented the stopped-Gateway workflow in `docs/DEPLOYMENT.md`.
- Initial focused compaction/security/config suite: **45 passed**.

## 2026-07-17 — Phase 28 compaction regression gate
- Added live SQLite writer rejection, deterministic timeout cancellation, installed-candidate rollback, rollback-failure backup preservation, and explicit SQLite error normalization.
- Extended candidate parity checks to `user_version`, `application_id`, encoding, `sqlite_sequence`, and bounded foreign-key violation results in addition to quick-check, schema hash, and table counts.
- Focused storage/deployment/config suite passed **118 tests**; dedicated compaction suite passed **15 tests**.
- Full suite passed **1229 tests, 2 skipped in 79.47s**.

## 2026-07-17 — Phase 28 shared admission implementation
- Added `gateway_admission.py` and migrated the live request concurrency slot from a process-local `BoundedSemaphore` to configurable shared SQLite leases.
- Added multi-process atomicity, strict-limit reconciliation, heartbeat, crash recovery, privacy/permission, backend outage/fallback, two-live-Gateway HTTP, and configuration regressions.
- Synchronized admission defaults across Python, JSON, YAML, `.env.example`, both Compose variants, capabilities, metrics, and operations documentation.
- Focused admission/rate/config tests passed **58**, then the primary Gateway/protocol/runtime selection passed **439** tests.
- The first full gate found one flaky concurrent WAL initialization lock. Added the shared retrying journal-mode helper, preserved in-memory persistence behavior, and ran the eight-thread first-open regression **20 consecutive times** successfully.

## 2026-07-17 — Phase 28 compaction and shared-admission batch completed
- Final full suite passed **1244 tests, 2 skipped in 81.88s** after the WAL retry correction.
- Compileall, `git diff --check`, development Compose, and production Compose with process-only dummy credentials passed.
- Built `ai-gateway-phase28-admission-test` and ran two real non-root Gateway containers against one shared admission database. A lease held by a short-lived crashed container made both Gateways return 429; after TTL, the next request returned 200 with calculator result `42`. Metrics reported 2 rejections and 1 expired lease reaped.
- Isolated local acceptance again passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with the shared SQLite admission backend active.
- Removed the temporary production image, containers, processes, and runtime directories. Existing historical databases were not compacted or pruned.
- Branch remains `codex/gateway-owned-multitool`; HEAD remains `4fb9e2b56fc70920e09cf157fd1e194ee27938a4`; no Git commit, real-upstream request, alarm/reminder validation, or credential persistence occurred.

## 2026-07-17 — Phase 28 bounded observability implementation
- Added `gateway_observability.py`, integrated HTTP lifecycle/request IDs, canonical tool execution, non-stream upstream requests, and streaming first-event/total timing.
- Added Prometheus rendering to `/admin/metrics`, authenticated `/admin/traces.json`, capability disclosure, and operations documentation.
- Initial focused observability/admission/proxy/stream/tool-trace suite: **62 passed**.

## 2026-07-17 — Phase 28 observability and CI batch completed
- Added request-ID propagation across HTTP response, SQLite request log, upstream request, tool span, and upstream span.
- Primary observability/protocol selection passed **448 tests**; full suite passed **1252 tests, 2 skipped in 83.15s**.
- Added `requirements-dev.txt`, `pyproject.toml`, executable `scripts/ci_gate.sh`, and `.github/workflows/gateway-ci.yml` with Python 3.10/3.11 jobs.
- Ran the complete CI gate locally in a clean temporary virtual environment: Ruff, Mypy, Bandit, `pip check`, `pip-audit`, full pytest (**1252 passed, 2 skipped**), JSON/YAML, tracked-secret check, both Compose renders, and Docker build all passed.
- Final isolated local acceptance with observability active passed both real-tool checks. Admin metrics contained HTTP/tool histograms and the trace ring contained privacy-filtered tool spans.
- Removed the temporary CI virtual environment, Docker image, Gateway process, and runtime directory. No real upstream, alarm/reminder validation, Git commit, or credential persistence occurred.

## 2026-07-17 — Phase 28 hotspot decomposition started
- Re-read HTTP Handler and Tool Runtime boundaries and selected low-coupling operations/admission seams for the first extraction.
- Found a duplicated maintenance-failure Prometheus sample during exact metrics-block inspection; a dedicated renderer regression will pin the correction.

## 2026-07-17 — Phase 28 current problem/enhancement review
- Restored the persistent plan and verified the current dirty branch/HEAD context without resetting, committing, contacting a real upstream, or exercising alarm/reminder functionality.
- Compiled the new Admin operations module, request-admission compatibility module, HTTP handler, and tool runtime successfully.
- Ran a focused Admin/admission/observability/storage/security/Gateway selection: **55 passed, 349 deselected in 19.42s**.
- The host interpreter does not have Ruff installed, so the chained ad-hoc lint command stopped before Mypy; the previously documented clean isolated CI gate remains the latest complete static/security/dependency evidence. No repeated global-environment installation was attempted.
- Confirmed the request-admission extraction is incomplete: the new module is currently unused and the old runtime wrappers remain live.
- Confirmed direct tests and Mypy coverage have not yet moved to the new Admin/admission seams.
- Measured current hotspots and static import coupling; one 23-module strongly connected component remains, and the largest test/runtime files remain 13.6k/5.1k/4.5k lines.
- Confirmed stale product documentation advertises a live `/api/web2api` route that the current capability contract truthfully marks as not integrated.
- Performed a read-only immutable SQLite/storage probe: restrictive `0600` modes remain in place; legacy primary/persistence/stats/planner files still use `auto_vacuum=0`; admission uses mode `2`; runtime storage remains about 1.6 GiB across 594 top-level directories. No database was compacted, pruned, or rewritten.
- `git diff --check` remains clean after the audit-record updates.

## 2026-07-17 — Phase 28 Admin/admission decomposition implementation
- Migrated Tool Runtime's legacy `_acquire_request_slot` and `_request_slot_scope` exports onto `gateway_request_admission.py`, deleting the duplicate live implementations while preserving Tool Runtime and `gateway_app` function identities.
- Added direct Admin operations contracts for unmatched routes, authentication on all six operations paths, metrics content type/readiness, stats/request/failure response shapes, offline-only storage preflight, trace limit normalization, and unique maintenance-failure metric emission.
- Added direct request-admission wrapper contracts for current configuration use, release on success/body exception, acquire failure, and legacy facade/runtime export identity.
- Added both new modules to the enforced Mypy list.
- Verification: focused boundary suite **62 passed**; primary Gateway/Claude/true-streaming/tool-runtime suite **435 passed**; full suite **1274 passed, 2 skipped in 83.95s**.
- A clean temporary development environment passed targeted Ruff and Mypy, then the complete CI gate: whole-tree Ruff, six-module Mypy, high-severity Bandit, `pip check`, `pip-audit` with no known vulnerabilities, full pytest **1274 passed, 2 skipped**, JSON/YAML/workflow parsing, secret/runtime tracking guard, both Compose renders, and production Docker build/removal.
- First acceptance wrapper was rejected before execution due explicit recursive cleanup text. The Python-temporary-directory replacement started correctly, but its first trusted-local tool request returned the expected cloud-safe HTTP 400 because `GATEWAY_EXECUTE_USER_SIDE_TOOLS` was not opted in. The next run uses the documented trusted-local-only flag; the production default remains unchanged.
- The second acceptance run passed local filesystem/shell execution and stopped at the WebFetch loopback fixture because the production SSRF default correctly rejects private addresses. The final isolated run opts into private-network tools only for that localhost fixture; no global/default configuration is weakened.
- Final isolated local acceptance passed both required proofs: `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool`. The local-only process explicitly opted into user-side execution and loopback network fixtures; production-safe defaults were not changed. Temporary Gateway processes/directories were terminated and removed automatically.
- Corrected current-facing README, running guide, implementation status, architecture, and historical class-architecture warnings so they no longer advertise `/api/web2api` or active legacy multi-upstream routing. Added explicit Assistants/Threads create-only scope and made `/capabilities` authoritative.
- Capability/Assistants/Threads focused verification passed **5 tests**; `git diff --check` remains clean.
- Phase 28 product-scope review is complete. The first low-coupling runtime/test decomposition slice is complete, but the broader hotspot item remains active because Tool Runtime, Planner, and the 13.6k-line Gateway test module still require incremental decomposition.

## 2026-07-17 — Phase 28 typed HTTP I/O decomposition completed
- Added `gateway_http_io.py` and migrated JSON/text response writing, safe error fallback, request-size/Content-Length handling, bounded complete reads, JSON-object decoding, constant-time equality, and Basic Auth parsing out of the main Handler.
- Preserved `gateway_http_handler`/`gateway_app` legacy response/auth exports and the existing Handler request-body-limit monkeypatch contract through thin read wrappers.
- Added `tests/test_gateway_http_io.py` with 22 direct contracts and placed the module in both Mypy and high-severity Bandit gates.
- Focused HTTP I/O/security/Admin/admission suite: **52 passed**. Main Handler/security/observability/admission/true-streaming suite: **408 passed**.
- Targeted Ruff and Mypy passed. Complete clean-environment CI passed whole-tree Ruff, seven-module Mypy, Bandit, `pip check`, `pip-audit` with no known vulnerabilities, full pytest **1296 passed, 2 skipped in 88.82s**, both Compose renders, and production Docker build/removal.
- Final isolated fake/local-upstream acceptance again passed `direct_tool_runtime_real_tools` and `native_tool_call_orchestration_executes_real_tool` with trusted-local-only opt-ins. Temporary processes, runtime data, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,916 lines, down from 2,009 before this slice; `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, or Git commit occurred.

## 2026-07-17 — Phase 28 typed HTTP authentication decomposition completed
- Added `gateway_http_auth.py` and migrated Admin Basic authentication/password-hash upgrade plus downstream Bearer/Basic/x-api-key verification, stable client identity, protocol ACL, and models compatibility out of the Handler.
- Preserved Handler/App `_check_admin` and `_check_downstream_key` entrypoints; Admin configuration failures still route through the Handler's canonical error mapper.
- Hardened malformed non-list `downstream_keys` to fail closed while preserving absent/empty-list auth-disabled semantics.
- Added `tests/test_gateway_http_auth.py` with 24 direct contracts and added the module to Mypy and high-severity Bandit gates.
- Focused auth/config/permission/I/O suite: **83 passed**. Handler/auth/I/O/security/permission/config/admission/rate suite: **481 passed**.
- Targeted Ruff/Mypy passed. Complete clean-environment CI passed whole-tree Ruff, eight-module Mypy, Bandit, dependency checks/audit, full pytest **1320 passed, 2 skipped in 85.94s**, both Compose renders, and production Docker build/removal.
- Isolated fake/local-upstream real-tool acceptance again passed both required checks. All temporary processes, runtime/config/database state, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,849 lines. `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, or Git commit occurred.

## 2026-07-17 — Phase 28 Admin origin/form decomposition completed
- Added `gateway_admin_security.py` and migrated request-origin derivation, browser same-origin checks, and authenticated Admin write composition out of the Handler.
- Hardened origin derivation to ignore untrusted `X-Forwarded-Host` while retaining Host plus proxy-overwritten `X-Forwarded-Proto` and configured `public_base_url` compatibility.
- Extended `gateway_http_io.py` with bounded JSON/urlencoded Admin form decoding; malformed JSON, non-object JSON, invalid UTF-8, and excessive field counts now fail as HTTP 400-class errors instead of falling through or surfacing as 500.
- Added 13 Admin-security contracts and 6 form/wrapper contracts; placed Admin security in Mypy and Bandit gates. Focused boundary suite: **90 passed**; Admin/HTTP/Handler/config/permission suite: **498 passed**.
- First Mypy pass exposed only a local union-narrowing issue; reading `config["gateway"]` once before `isinstance` narrowing fixed it without runtime behavior change.
- Complete clean-environment CI passed Ruff, nine-module Mypy, Bandit, dependency checks/audit, full pytest **1339 passed, 2 skipped in 87.43s**, both Compose renders, and production Docker build/removal.
- Isolated fake/local-upstream real-tool acceptance again passed both checks. Temporary processes, runtime/config/database state, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,824 lines. `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, database compaction, or Git commit occurred.

## 2026-07-17 — Phase 28 transactional Admin client mutation batch completed
- Added `gateway_admin_client_mutations.py` and migrated client snippet/config, Admin password rotation, and downstream key add/delete out of the Handler.
- Mutations are now copy-before-validate and single-save with optimistic revision. Invalid public origins, invalid/nonpositive numeric limits, auto-compact beyond context, malformed config structures, duplicate key names/hashes, missing fields, and unknown actions return explicit non-success results without mutating or saving the source config.
- Fixed stale credential behavior: clearing the client snippet key revokes the auto-managed `client-snippet` authentication entry but preserves explicit downstream keys.
- New downstream credentials receive a stable ID immediately and persist only secret hash/fingerprint, full protocol ACL, enabled state, and UTC timestamp; direct tests prove raw secrets are absent.
- Added 21 direct mutation contracts and placed the module in Mypy/Bandit gates. Focused mutation/security/auth/I/O/config/permission suite: **130 passed**; Handler/Admin/config/permission suite: **502 passed**.
- Complete clean-environment CI passed Ruff, ten-module Mypy, Bandit, dependency checks/audit, full pytest **1360 passed, 2 skipped in 89.37s**, both Compose renders, and production Docker build/removal.
- Isolated fake/local-upstream real-tool acceptance passed both required checks. Temporary processes, runtime/config/database state, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,775 lines. `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, database compaction, or Git commit occurred.

## 2026-07-17 — Phase 28 transactional Connector/Profile mutation batch completed
- Added `gateway_admin_connector_mutations.py` and migrated MCP add/delete/reload, HTTP Action add/delete, and upstream profile save/delete/activate out of the Handler.
- Fixed MCP runtime coherence: successful config mutation now saves before closing old sessions; failed save never reloads; explicit reload remains write-free.
- Added no-DNS Admin HTTP Action preflight while retaining full runtime DNS/IP/redirect validation, plus duplicate-name, method, private-literal, explicit private opt-in, malformed collection, missing-field, and unknown-action contracts.
- Fixed profile edit/activation coherence: omitted secrets/settings survive edits, active profile edits refresh live upstream, activation updates both active aliases plus upstream, active deletion is rejected, and invalid/missing profiles/actions fail before saving.
- Added 30 direct Connector/Profile contracts and placed the module in Mypy/Bandit gates. Focused Connector/Profile suite: **86 passed**; broad Handler/MCP/HTTP Action/Profile/config/stability suite: **687 passed**.
- Complete clean-environment CI passed Ruff, eleven-module Mypy, Bandit, dependency checks/audit, full pytest **1390 passed, 2 skipped in 88.10s**, both Compose renders, and production Docker build/removal.
- Isolated fake/local-upstream real-tool acceptance passed both required checks. Temporary processes, runtime/config/database state, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,711 lines. `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, database compaction, or Git commit occurred.

## 2026-07-17 — Phase 28 transactional General Admin Config batch completed
- Added `gateway_admin_config_mutations.py` and removed the large `/admin/config` mutation body from the Handler.
- Added atomic upstream/profile, Gateway runtime, CORS, and context/fan-out validation: URL credentials/query/fragment rejection, protocol/path checks, positive and bounded limits, output≤input, fanout chunk≤context, exact wildcard-free CORS origins, tool-mode allowlist, malformed collection rejection, and full profile-ID uniqueness.
- Preserved omitted numeric values and full-form checkbox semantics. Validation/save conflicts leave the loaded config unchanged and invoke no save before all invariants pass.
- First focused run exposed that duplicate IDs elsewhere in the profile list were not caught when editing a new ID; uniqueness now covers the complete existing collection before replacement/append.
- Added 28 direct General Admin Config contracts and placed the module in Mypy/Bandit gates. Focused config/security mutation suite: **107 passed**; Handler/Admin/context/config/permission suite: **607 passed**.
- Complete clean-environment CI passed Ruff, twelve-module Mypy, Bandit, dependency checks/audit, full pytest **1418 passed, 2 skipped in 90.74s**, both Compose renders, and production Docker build/removal.
- Isolated fake/local-upstream real-tool acceptance passed both required checks. Temporary processes, runtime/config/database state, CI environment, and image were removed.
- `gateway_http_handler.py` is now 1,648 lines. `git diff --check` remains clean. No real upstream, alarm-function validation, credential persistence, database compaction, or Git commit occurred.

## 2026-07-17 — Phase 28 Admin Catalog audit after extraction
- Restored the persistent planning context and compiled `gateway_admin_catalog_mutations.py`, `gateway_http_handler.py`, and `gateway_app.py` successfully.
- Existing Skill/Marketplace selection passed **10 tests**; broader Admin/Security/Config selection passed **153 tests**; the full suite passed **1418 tests, 2 skipped in 88.23s**.
- Reproduced two concrete Catalog defects in isolated temporary directories: existing `0755`/`0644` permissions are not tightened, and a post-check symlink swap redirects the supposedly confined write outside the catalog while the operation reports success.
- Confirmed Admin-installed `cwd/skills` is not part of the runtime Skill discovery contract for ordinary request workspaces, so successful installation is not reliably consumable.
- Confirmed the new Catalog module has no direct test file and is missing from CI Mypy/Bandit lists. Host Python still lacks Mypy, so no ad-hoc host type run was claimed; the last clean isolated CI evidence predates this module.
- Recomputed current static import coupling: the largest strongly connected component contains 31 modules. Current largest files remain `tests/test_gateway.py` (13,626), `gateway_tool_runtime.py` (5,061), `gateway_agent_planner.py` (4,463), and `gateway_builtin_tools.py` (2,231).
- No product code, real upstream, alarm/reminder function, historical database, credential, Git commit, or user-owned dirty change was modified during this audit; only persistent planning records were updated.
# 2026-07-24 Phase 29 current full-function audit
- Restored the existing planning context and ran the session catch-up helper; no unsynced context was reported.
- Confirmed the worktree is intentionally dirty at commit `0c47f1e` on `codex/gateway-owned-multitool`, with current changes in `src/gateway_admission.py`, `src/gateway_rate_limit.py`, `src/gateway_sqlite.py`, `tests/test_gateway_admission.py`, and `tests/test_gateway_rate_limit.py`.
- Started a fresh read-only completeness audit. Product code will not be edited, reverted, committed, or assumed correct merely because older audit phases reported success.
- Inventoried the repository (57 source files, 54 test-tree entries at top level plus integrations, CI workflow, deployment assets, runtime artifacts) and extracted the current advertised feature/limitation contract from README and the Agent Planner matrix.
- Confirmed the docs themselves disclose non-complete surfaces: minimal Assistants/Threads plus Web2API and multi-upstream modules not wired to the HTTP request path.
- Traced the live HTTP capability and route contract and inspected the five-file dirty functional change. No product edits were made.
- Re-audited the formerly open Admin catalog boundary and the intelligence integration. Catalog implementation/tests are now present; optional LLM intelligence still references a provider module absent from this checkout.
- Ran a 260-test focused suite covering current dirty changes and the principal explicit partial/inactive surfaces; all passed in 25.19 seconds.
- Ran the full repository suite: 1453 passed, 2 skipped in 120.30 seconds.
- Began the static/security/dependency gate. Compile and JSON/YAML parsing passed; the combined command stopped at missing `python3 -m ruff`, so the remaining tools are being resolved individually.
- Re-ran gates in an isolated `requirements-dev.txt` environment: Ruff/Bandit/pip-check/pip-audit passed, but Mypy failed on an unused `type: ignore` in the current uncommitted `gateway_sqlite.py` change. This is a current release-blocking gate failure, not an environment-only limitation.
- Ran the official Agent Planner smoke gate. It failed at the multi-round smoke; diagnosed the runtime-inside-workspace sandbox interaction and proved the core workflow succeeds when runtime storage is separated from the client workspace.
- Continued the integration set manually: strict protocol passed directly; public surface passed all 21 advertised paths with the current readiness event; remote multi-tenant pressure and long-context pressure passed with scoped audit/recall/streaming/compaction evidence.
- Compared live Admin routes/UI against README and running documentation. Found a dormant 9-tab web-config module and several documented `/api/*` endpoints that are not wired to the current Handler.
- Validated both Compose contracts, Dockerfile checks, full image build, and an actual container main-process startup/direct-tool/error-mapping smoke. Removed the temporary audit container/image afterwards.
- Confirmed the only two skipped tests are live-upstream E2E cases gated by `TEST_UPSTREAM_URL`.
- Completed the feature-by-feature classification and Phase 29 audit. Final conclusion: core Gateway scope is largely implemented and runnable, but the repository is not fully feature-complete or release-gate clean.
- Moved the five audit-created integration runtime directories (about 2.3 MiB total) to macOS Trash after verification. They are recoverable; pre-existing runtime/history data was not touched.
# 2026-07-24 — Phase 30 full-function remediation started
- The user authorized implementation of every incomplete or blocked item identified by the Phase 29 audit, with stable release-ready behavior as the completion condition.
- Restored the persistent plan and current dirty-worktree inventory. Existing changes in `src/gateway_admission.py`, `src/gateway_rate_limit.py`, `src/gateway_sqlite.py`, `tests/test_gateway_admission.py`, and `tests/test_gateway_rate_limit.py` are user-owned and must be preserved while overlapping fixes are made.
- Added Phase 30 covering release gates, acceptance harnesses, Assistants/Threads, Web2API, multi-upstream routing, LLM intelligence, Admin APIs/UI, documentation synchronization, comprehensive regressions, deployment verification, and a final requirement-level completion audit.
- Baseline SQLite/admission/rate-limit verification passed 35 focused tests. Removed the single unused Mypy suppression on the typed `fcntl` import without altering the user's synchronization/journal-mode behavior.
- The default Python lacks Mypy; a clean dev environment will be used for the authoritative static and release gates.
- Repaired the multiround smoke so Gateway runtime/config are outside the client workspace, preserving the production sandbox boundary while allowing pytest to inspect only client code.
- Repaired public-surface readiness setup/restore to use the current HTTP readiness Event.
- Both repaired smokes passed independently: multiround produced `Bash -> Read -> Read` with one upstream call and ignored unauthorized Edit; public surface exercised all 21 advertised paths successfully.
- The complete repository Agent Planner acceptance wrapper now passes, including strict protocol, multi-user pressure, long-context compaction/isolation, and its 85-test focused regression gate.
- Replaced the create-only Assistants helper with a private SQLite-backed resource service and began compatibility verification. The first legacy test exposed a missing non-content message-count field; the field and failed-create cleanup were restored before proceeding.
- Added persistence, tenant isolation, run completion, tool-output resumption, failure/cancel, cascade-delete, and full HTTP lifecycle regressions. Their first run exposed same-second ordering drift, now corrected with an insertion-order tie-breaker.
- Assistants/Threads focused verification now passes 39 tests across legacy compatibility, persistent CRUD, tenant isolation, messages, synchronous run execution, `requires_action`, tool-output resumption, terminal failure/cancel, cascade deletion, dynamic HTTP routing, route ACL, and truthful capabilities.
- Added and verified the multi-upstream profile-pool regressions together with existing proxy-error, true-streaming, and legacy concurrency coverage: **79 passed in 6.11s**.
- A source-inspection command used the stale root path `RUNNING_AND_TESTING.md`; the actual document is `docs/RUNNING_AND_TESTING.md`. The `&&` chain stopped before source output, so no code or state changed. Future documentation checks use the resolved path.
- The first upstream-status focused pytest command used the stale class name `GatewayTests`; the live capability contract belongs to `NativeGatewayTests`, so pytest collected zero tests. The selector was corrected before validation; no product behavior was inferred from that failed command.
- Wired the multi-upstream capability snapshot and authenticated status endpoint; focused Admin operations, pool, and capability verification passed **25 tests**.
- Added the Gateway-owned pluggable LLM intelligence provider, configuration parsing, provider runtime/status, rule fallback, strict failure propagation, and authenticated intelligence status endpoint. Compile plus provider/intelligence/Admin/capability verification passed **98 tests**.
- During Admin API implementation, source inspection caught `clear_persistent_caches()` inserted in the middle of `delete_tool_cache_scope()`. Although Python still compiled, the original scope deletion would have become unreachable; the function boundary was restored before any test claim.
- The first unified Admin API regression run passed 74/75 tests. Its single failure was a legacy UI-only fixture using `upstream.url`; the canonical runtime field is `upstream.base_url`. Added a read-only render migration mapping without persisting the obsolete key.
- A follow-up focused pytest command guessed a non-existent Agent Runtime scope test node and therefore collected zero tests. Located the exact live method `test_agent_runtime_audit_scope_contract_documents_non_conversation_exclusions` before rerunning the selection.
- The corrected 78-test run produced 76 passes and two later config-decryption failures. Root cause was the new Admin HTTP fixture leaving a temporary Fernet instance in process-global encryption state after restoring `GATEWAY_RUNTIME_DIR`; the fixture now saves, clears, and restores both encryption globals so subsequent tests reload the correct repository key.
- The next corrected unified Admin/config/cache/capability selection passed **78 tests**.
- The first Assistants cleanup test patch referenced a non-existent function context and was rejected atomically by `apply_patch`; no test file changed. Re-read the live file and added capacity/retention coverage at the actual insertion point.
- Added Assistants DB/retention/max-row defaults and bounded maintenance integration. Assistants lifecycle/cleanup, maintenance, Admin API, and config-sync focused verification passed **36 tests**.
# 2026-07-24 — Phase 30 resumed: configuration/deployment synchronization

- Restored the persistent planning context, ran the planning session catch-up helper, confirmed branch `codex/gateway-owned-multitool`, and preserved the existing dirty worktree.
- Validated `gateway.config.json` with `python3 -m json.tool`; the new Assistants/cache/intelligence/multi-upstream/stats/Web2API sections are valid JSON.
- Audited the environment and Compose templates and confirmed the newly integrated feature controls are not yet represented there; config-sync regression coverage also remains incomplete.
- Synchronized `gateway.config.yaml`, `.env.example`, `docker-compose.yml`, and `docker-compose.prod.yml` with persistence, Assistants retention, cache, intelligence provider, upstream pool/failover, stats, and Web2API controls.
- Added config-sync coverage that compares the new runtime sections to both JSON and YAML templates and pins every new environment/Compose default, including persistent container database paths and secure opt-in flags.
- Focused configuration and deployment verification passed: **22 passed in 7.22s**. Development Compose also renders successfully with only expected warnings for unset local credentials.
- Updated `README.md`, `docs/RUNNING_AND_TESTING.md`, and `docs/IMPLEMENTATION_STATUS.md` so Assistants, Web2API, multi-upstream failover, LLM provider, Config Center/Admin APIs, revision/same-origin semantics, and streaming failover limits match the live code. Removed the obsolete callable `_legacy_config_tabs()` schema from the production module.
- Documentation/Admin/Web2API/Assistants/provider/upstream focused regression passed **162 tests**; canonical config-schema/capability selection passed **45 tests**.
- Added `gateway_admin_api.py`, `gateway_assistants.py`, `gateway_llm.py`, and `gateway_upstream_pool.py` to the repository Mypy/Bandit gate. Ruff and focused Bandit pass, but the first focused Mypy run reported **33 type errors** caused by Optional/Any narrowing and one numeric conversion. These are now the active static-gate blockers; the gate will not be weakened or suppressed.
# 2026-07-24 — Phase 31 current completeness re-audit started

- The user requested a fresh audit of whether the current code fully implements every function.
- Treating the live dirty worktree as authoritative and Phase 30 history only as context.
- This phase is read-only for product code: no implementation edits, reversions, commits, or destructive cleanup are authorized.
- Initial snapshot already shows Phase 30 is unfinished and its latest recorded release-gate blocker is 33 Mypy errors in newly integrated modules; all conclusions will be revalidated against current source and executable gates.
- Read the current capability documentation, CI gate, live route declarations, and newly added implementation modules.
- Found current-vs-legacy documentation contradictions around Assistants, Web2API, and multi-upstream integration.
- Created an isolated audit virtual environment at `/tmp/ai_gateway_audit.VRnyBM/venv`; no repository dependencies or product files were changed.
- Focused new-feature suite passed: 111 tests in 21.49 seconds.
- Static/security/dependency gates passed: compile/config parsing, Ruff, selected-module Mypy, Bandit, `pip check`, and `pip-audit` are green.
- Official Agent Planner smoke acceptance passed, including its 92-test regression selection.
- Full pytest failed with 4 deterministic failures and 1485 passes, 2 skips. Re-ran all four exact nodes alone; all four failed again. Re-ran the three process/runtime nodes with host Python; all three failed again.
- First combined diff/secret/Compose diagnostic had a Python `-c` quoting syntax error in the audit command itself; reran the meaningful diff and both Compose checks successfully with a simpler command. No product state changed.
- Docker image build passed. Ephemeral container readiness and calculator execution passed; the audit assertion incorrectly read `supported_paths` from `/capabilities` instead of `/readyz`, but saved response inspection confirmed 24 ready paths, 67 builtins, and calculator output `42`. The exact smoke container was removed automatically.
- Confirmed the two skipped full-suite cases are live-upstream E2E checks gated by `TEST_UPSTREAM_URL`; no current real-upstream credential was supplied, so real provider behavior remains unverified in this audit.
- Completed Phase 31 with a negative completeness verdict: the broad feature set is substantially implemented and smokeable, but the repository is not currently fully functional/release-ready because four deterministic regressions keep the authoritative full gate red and documentation is inconsistent.
- Moved the exact audit-created virtual environment, saved smoke JSON files, and four Agent Planner runtime directories to macOS Trash; they are recoverable. Removed the exact audit Docker image. No user-owned runtime/history data, product code, or Git commit was changed.

# 2026-07-24 — Phase 32 release-blocking correction started

- The active goal now explicitly authorizes implementing all audited functionality, not only reporting gaps.
- Restored the live worktree and planning context; no unsynced session context was reported.
- First correction batch targets the four deterministic full-suite regressions: proxy pool-state compatibility, Bash timeout partial output, immediate exec failure classification, and initial exec-session output/readiness semantics.
- Existing dirty worktree changes remain in scope and must be preserved; no Git commit will be created unless explicitly requested.
- Added an internal sandbox worker readiness pipe and separated policy-worker startup from real command execution/read timing.
- Applied the readiness contract to bounded Bash/code/apply-patch jobs and long-lived exec sessions without changing stdout/stderr protocol or exposing Gateway secrets.
- Made proxy upstream-pool accounting optional for compatibility/minimal clients.
- Compilation and the four exact release-blocking regressions pass: 4 passed in 4.12 seconds.
- The first broader regression command used two stale/nonexistent tool-runtime filenames and collected no tests. Enumerated the live test files before retrying; no product behavior was inferred from that command.
- Hardened readiness-wait cancellation so an explicit cancellation during worker setup still terminates the complete process group and returns bounded captured output.
- Broader proxy/process/sandbox/tool-runtime verification passed 75 tests in 26.60 seconds.
- The first combined documentation patch failed atomically because overlapping source views made one Config Center line appear duplicated; split the patch against exact current text, then synchronized the live architecture/status documents and explicitly labeled the 2026-06-17 class analysis as a historical snapshot.
- Current-feature/proxy/process/sandbox/Admin/Assistants/Web2API/provider selection passed 160 tests.
- Full pytest passed: 1489 passed, 2 skipped in 105.32 seconds.
- Complete clean-environment `scripts/ci_gate.sh` passed, including a second full run (1489 passed, 2 skipped), all static/type/security/dependency gates, Compose, and Docker build/removal.
- Official Agent Planner acceptance passed after the fixes with its 92-test focused selection.
- Controlled live-upstream tests: models passed; chat failed with provider HTTP 401. Reproduced directly without proxy, kept the credential secret, and did not mutate the ignored local config. This is now the only external E2E evidence gap.

# 2026-07-24 — Phase 32 official `verify` repair and acceptance

- Repaired `scripts/mimo_gateway.sh verify` so trusted-local user-side tools and localhost fixtures are enabled only for acceptance stages, while the unit suite and final Claude/Codex ownership smoke retain production-safe defaults.
- Added environment-based Admin credential resolution in `tests/integration/security_gateway_checks.py`; the wrapper no longer passes Admin secrets on argv or adds them to the launchd plist.
- Removed the gate's dependency on PATH-resolved `env`, which was shadowed by a non-executing local script on this host. The unit suite now runs in a Bash subshell with explicit unset/export behavior and failure-safe temporary cleanup.
- Added macOS Bash 3.2 compatibility and regression contracts for verify-only overrides, normal safe defaults, secret handling, cleanup, and stage-five ownership isolation.
- Fixed completed exec-session reaper delivery by retaining bounded terminal snapshots and closing process streams deterministically. Strengthened the regression to force a reaper/client race and still require exit code 6.
- Stabilized scheduling-sensitive process tests while preserving behavioral coverage for partial timeout output, descendant cleanup, initial output, and live-session cache bypass.
- Focused wrapper/process/deployment verification passed 37 tests; the exact process regressions also pass under `unittest`.
- A standalone project-scope smoke passed every contract, including installed Claude/Codex CLIs.
- The complete official `./scripts/mimo_gateway.sh verify` passed all five stages in one run against an isolated local mock upstream: 509 unittest cases, real tool acceptance, security guardrails, concurrent direct/model traffic, and full project-scope/Skill/CLI validation.
- Stopped the isolated Gateway/mock server and moved nine exact audit-created temporary/runtime directories to macOS Trash, including the earlier clean CI environment and latest Agent Planner artifacts. No user-owned runtime/history data was touched.

# 2026-07-24 — Phase 32 final release completion

- Re-ran the authoritative full suite after the exec-session/wrapper corrections: **1492 passed, 2 skipped in 118.80s**.
- Built a fresh isolated development environment and ran the complete `scripts/ci_gate.sh`: compile/config parsing, Ruff, 17-module Mypy, Bandit, `pip check`, `pip-audit` with no known vulnerabilities, a second full pytest **1492 passed, 2 skipped**, `git diff --check`, tracked secret-like file guard, both Compose renders, Docker build, and image removal all passed.
- Recomputed the live capability snapshot: 24 supported public paths; persistent Assistants/Threads lifecycle; authenticated bounded Web2API; integrated upstream profile pool; all documented Config/Stats/Cache/Upstream/Intelligence operations present; intelligence defaults to intentional rule mode until enabled.
- Audited every Phase 30 requirement against implementation modules, route wiring, direct regressions, full suites, official wrappers, container evidence, configuration, and documentation. No advertised Gateway surface remains partial, inactive, defective, or undocumented.
- Updated README/running/status documentation with the final 2026-07-24 gate evidence and verify-specific security scope.
- The only remaining evidence limitation is external: the ignored local live-provider credential returns HTTP 401 for chat both through and directly around Gateway, while models succeeds. No credential was printed, persisted, or changed.
- Final high-risk focused selection passed **105 tests** after documentation/plan synchronization. `git diff --check` remains clean, ports 18888/19002 have no listeners, the final isolated CI environment was moved to Trash, and Git HEAD remains `0c47f1e` with no commit created.
