# FieldRecognitionDemo Agent Guide

## Scope

- This is an independent local project for scene QR codes, instrument binding, receiver snapshots, and CPU PaddleOCR panel recognition.
- Read `README.md` and `AgentTools.md` before changing runtime or interface behavior. Do not modify VisionCortex or its services as part of work here.
- Verify the project's Git root, branch, SHA, remotes, and working-tree status before Git changes. No remote repository is configured by default; do not invent a publishing destination.
- Preserve unrelated work and stage named paths only.

## Data and evidence

- Preserve existing QR identities, source photos, timestamps, bindings, database records, and historical receipts.
- `Data/`, `Verification/`, `VerificationData/`, `output/`, and `tmp/` are local runtime or delivery material. Keep them, environments, model weights, logs, and secrets out of Git.
- Credentials belong in environment variables or an untracked local store. Do not print their values.
- Do not access NAS or start GPU jobs for this project. Live camera access and real OCR invocation must be within the user's requested task.
- Tests and synthetic panels do not prove real instrument accuracy. Distinguish `PROVEN`, `PARTIAL_EVIDENCE`, and `NOT_PROVEN` with the evidence and missing gate.
- QR matches must come from decoding; never infer an identity from label text or the registry. Model confidence is not measured accuracy, and OCR text alone does not establish a physical action.

## Verification

- Use this project's `.venv`; its interpreter must not resolve through another project.
- Run `env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q tests`. Tests use temporary data; do not run them against `Data/`.
- Check changed Python with relevant tests and `python -m compileall`; check changed web JavaScript with `node --check static/app.js`.
- Documentation-only changes need `git diff --check` and referenced-path/command validation.
- Report the branch/SHA, checks, and remaining evidence boundaries. A service response alone is not end-to-end recognition proof.
