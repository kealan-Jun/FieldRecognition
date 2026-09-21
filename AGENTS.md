# FieldRecognition Agent Guide

## Scope

- This is an independent local project for scene QR codes, instrument binding, receiver snapshots, and PaddleOCR panel recognition. The user requested local GPU OCR on 2026-09-14; the current deployment uses the RTX 3090 Ti with FIELD_OCR_DEVICE=gpu:0. CPU remains an explicit portable option.
- Read `README.md` and `AgentTools.md` before changing runtime or interface behavior. Do not modify VisionCortex or its services as part of work here.
- Verify the project's Git root, branch, SHA, remotes, and working-tree status before Git changes. No remote repository is configured by default; do not invent a publishing destination.
- Preserve unrelated work and stage named paths only.

## Uninterrupted operation (user requirement, 2026-09-21)

- Normal iteration must not interrupt ongoing use. Develop and test in an isolated worktree with temporary test data; completing a change does not authorize a disruptive deployment.
- Do not stop or restart the live API, camera/scanner, OCR or archive services to deploy an iteration. Do not modify their loaded source, dependencies, configuration or served assets in place. Do not use direct Git checkout/pull, install scripts or systemctl to bypass the upgrade guard.
- Inspect the tested revision with `scripts/upgrade_runtime.py --revision <SHA>`. The upgrade entry point must never stop services automatically. While services run, only its narrow allowlist of documentation, tests and the standalone upgrade tool may advance without a restart.
- Runtime changes remain staged until an uninterrupted replacement mechanism is implemented and verified. The current shared source deployment has no verified rolling or blue/green upgrade path. Do not claim zero downtime, lossless scan capture or continued bindings based only on process health or tests.
- A genuinely offline deployment is distinct from an iteration during normal use. Do not create an outage to satisfy the tool's offline precondition. Any future user-authorized service interruption must identify its effect on active bindings and ongoing scans before execution.
- Capture service identity and health before and after deployment. Existing QR evidence, timestamps, binding history and queued OCR/archive records must be preserved. Device/service failures and Beijing midnight still follow the user's automatic release policy; never restore expired bindings to conceal an upgrade outage.

## Data and evidence

- Preserve existing QR identities, source photos, timestamps, bindings, database records, and historical receipts.
- `Data/`, `Verification/`, `VerificationData/`, `output/`, and `tmp/` are local runtime or delivery material. Keep them, environments, model weights, logs, and secrets out of Git.
- Credentials belong in environment variables or an untracked local store. Do not print their values.
- Do not access NAS outside the user's requested task. GPU OCR deployment and bounded GPU inference validation are authorized for this project; do not modify other projects or their GPU services. Live camera access and real OCR invocation must be within the user's requested task.
- Tests and synthetic panels do not prove real instrument accuracy. Distinguish `PROVEN`, `PARTIAL_EVIDENCE`, and `NOT_PROVEN` with the evidence and missing gate.
- QR matches must come from decoding; never infer an identity from label text or the registry. Model confidence is not measured accuracy, and OCR text alone does not establish a physical action.

## Verification

- Use this project's `.venv`; its interpreter must not resolve through another project.
- Run `env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q tests`. Tests use temporary data; do not run them against `Data/`.
- Check changed Python with relevant tests and `python -m compileall`; check changed web JavaScript with `node --check static/app.js`.
- Documentation-only changes need `git diff --check` and referenced-path/command validation.
- Report the branch/SHA, checks, and remaining evidence boundaries. A service response alone is not end-to-end recognition proof.
