# FieldRecognition Agent Guide

## Start with the requested outcome

- This is an independent project for QR identities, instrument bindings, Receiver snapshots and traceable panel recognition. Read `README.md` and `AgentTools.md` before changing runtime or interface behavior. Keep work within this project; do not modify unrelated projects or their services.
- Complete authorized implementation, validation and requested Git synchronization. Ask only for missing information that changes the result, a concrete destructive action, or an actual unresolved permission boundary. Routine reversible choices and already authorized actions need no repeated confirmation.
- Inspect the Git root, branch, SHA, remotes and status before Git changes. Discover configured remote names and URLs; if no destination exists, ask for it instead of inventing one. Preserve unrelated work and stage named paths only. Report which refs were pushed and verify their SHA.

## Code structure and responsibilities

- Production code belongs in `field_recognition/`: `api`, `bindings`, `capture`, `recognition`, `measurements`, `archive` and `runtime`. Root Python files are public compatibility entry points. Add a root file only when an external startup/import contract requires it.
- Canonical package imports must not create databases, directories, threads or models. Create a runtime through the application factory. Use its explicit context/configuration; do not obtain dependencies from `globals()`, `vars(app)` or imports of the root ASGI entry point.
- HTTP, browser and Agent adapters call the same evidence transactions. Keep role/camera checks, optimistic revisions, QR provenance and immutable confirmations when moving code. Do not call HTTP route functions as an internal business service.
- Keep numeric grammar, metric units, fallback rules and presentation utilities in their shared modules. Configuration parsing has one authoritative implementation; pass a validated snapshot into workers/pipelines instead of silently rereading a different environment contract.
- First-clone defaults must be portable: never use a site-specific device ID, host directory or real account as an implicit runtime default. New production databases use explicitly supplied local registries; bundled samples belong to test mode or explicit sample selection. Registry metadata never proves a decoded QR match or creates scan evidence.
- Changes to startup or installation need a model-free cold-source regression with an empty home and a clone path containing spaces. Validate generated units with the actual systemd parser when available; do not download or invoke real models to prove clone readiness.
- Split files by coherent responsibility when that reduces coupling. Avoid new one-function wrappers, duplicate implementations, generic frameworks or artificial file-size targets. Preserve intentional compatibility facades and distinct operational tools.
- Reusable test fixtures/doubles belong in `tests/support/`; tests live in the matching domain folder. Do not import one test module from another. Historical reports live in `docs/history/`; keep their recorded evidence/version claims intact.
- See `docs/CodeStructure.md` for dependencies, resources, compatibility and maintenance checks.

## Skills support the task

- Use a skill when the user invokes it or its concrete capability improves the requested work. A keyword, mention of AI, or an available connector alone does not make a skill applicable. Ordinary repository edits, local OCR work and offline tests use the agent's native coding tools.
- Read only the relevant skill and conditional references. Do not load every skill, install a plugin, change provider, create an artifact elsewhere, or expand a task solely because a skill suggests it.
- Preserve the user's selected provider and existing authorization. Offline mocks, refactors and documentation do not require new API credentials. A real external call, creation of a credential or writing a secret follows the actual task's authorization and safe credential workflow.
- User instructions take precedence over skill guidance. Do not turn optional recommendations into approval gates, or ask again for an action already authorized. If a binding requirement truly blocks work, cite the exact skill/instruction and explain its concrete application; continue independent work.
- Improve task-specific guidance in maintained project files. Do not patch generated plugin caches as a durable customization or duplicate the native coding workflow in a catchall skill. Preserve real safeguards for secrets, evidence and live services.

## Uninterrupted operation — user requirement, 2026-09-21

- Develop and test in an isolated worktree with temporary data. Do not modify loaded source, dependencies, configuration or served assets in the live checkout. Completing or pushing an iteration does not authorize disruptive deployment.
- Do not stop/restart the live API, camera/scanner, OCR or archive services. Do not bypass the upgrade guard with checkout/pull, installers or systemctl.
- Inspect the tested revision with `scripts/upgrade_runtime.py --project-root <production-checkout> --revision <SHA>`. This inspection must not stop services. While services run, only its narrow allowlist of documentation, tests and the standalone upgrade tool may advance without restart.
- Runtime changes remain staged until an uninterrupted replacement mechanism is implemented and verified. The shared source deployment currently has no verified rolling/blue-green path. Process health and passing tests do not prove zero downtime, lossless scans or retained active bindings.
- Do not create an outage to satisfy offline deployment preconditions. An explicitly authorized service interruption must identify its effect on active bindings and ongoing scans before execution. Capture service identity/health before and after a deployment.
- Preserve QR evidence, timestamps, binding history and queued OCR/archive records. Device/service failures and Beijing midnight still follow automatic release policy; never restore expired bindings to hide an outage.

## Data, storage and evidence

- Preserve QR identities, source bytes/photos, timestamps, bindings, databases and historical receipts. New fingerprints resolve canonical source files while retaining legacy receipt keys; never rewrite old fingerprints as if they were new runs.
- Keep code, this project's environment, current models and live SQLite/queues/locks on local storage. Use explicitly configured, verified archive targets and bounded temporary caches; do not relocate active resources during an unrelated refactor.
- `Data/`, `Verification/`, `VerificationData/`, `output/`, `tmp/`, environments, weights, logs and secrets stay out of Git. Secrets belong in environment variables or ignored local stores; never print their values.
- Do not access NAS, live cameras or real models outside the requested task. Before authorized NAS access verify the actual share and volume identity; fail closed on an absent/wrong mount. CPU is the default; GPU requires explicit device selection and compatible dependencies. Discover the actual deployment configuration instead of assuming particular hardware or host paths.
- Tests and synthetic panels do not prove real instrument accuracy. Report `PROVEN`, `PARTIAL_EVIDENCE` and `NOT_PROVEN` with evidence and missing gates. QR identity must come from decoding. Model confidence is not measured accuracy; OCR text does not establish a physical action.

## Verification and completion

- Use this project's `.venv`; never another project's interpreter. An isolated worktree may reuse the same project's environment without installing into it. Use temporary data and bounded caches, never production `Data/`.
- Run `env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q tests`. Unix socket tests need a short temporary root; `tests/conftest.py` uses short per-test names. Record baseline failures separately from regressions.
- Run relevant tests while developing, then the complete suite for runtime changes. Check Python with `compileall` and the configured Ruff checks; run `node --check` on every changed JavaScript file. Changes to module/resource layout also require import, compatibility entry point and fingerprint checks.
- Documentation-only edits need `git diff --check` and referenced-path/command validation. Update current navigation after moves; do not rewrite historical conclusions.
- For runtime changes or deployment candidates, inspect the tested revision with the upgrade guard after checks pass. Complete authorized synchronization and report branch/SHA, checks and deployment state. A service response alone is not end-to-end recognition proof.

## Storage placement — authorized 2026-10-10

- Keep active code, Python/OCR runtimes, current weights, SQLite/WAL, queues, binding state and current evidence work local. Do not modify or relocate live Data/ opportunistically or bypass the uninterrupted-operation upgrade guard.
- Completed delivery packages and historical bulk material belong on verified `//RealityLoop.local/backup_data` under `/mnt/realityloop-backup-data/LocalSSDOffload/x1/FieldRecognition`. Verify the CIFS source and host `.storage-volume.json` before access; never create a fallback beneath an unmounted NAS path. Existing business evidence archive ownership and provenance remain unchanged.
- The inactive `output/Delivery-20261009-b27982e` was moved with full SHA256, source-stability and active-user checks. Its original path is a compatibility link; receipt is under the host NAS root's `receipts/FieldRecognition/`. New delivery work should use a bounded temporary SSD workspace under `/srv/sentinel-data/LocalWorkspaces/FieldRecognition` and save the completed package on NAS. Preserve at least 50 GiB free on `/` and 80 GiB on `/srv/sentinel-data` before bulk work.
