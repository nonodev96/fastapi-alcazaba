# Feature Specification: A verifiable audit trail

**Feature Branch**: `feature/audit-verification` · **Created**: 2026-11-03 · **Status**: Done

**Input**: "An auditor should be able to verify the audit trail independently, without being
able to forge it."

## Clarifications (no question asked; no backward compatibility kept)

- Ed25519 signing keys (PEM or objects); HMAC secrets stay. Signatures are
  `<algorithm>:<kid>:<value>` over `fastapi_alcazaba/audit3` + RFC 8785 JSON: the verifier picks
  each event's key by its id, which makes rotation work.
- Checkpoints: every `checkpoint_interval`, with the next event, a process publishes its head
  in the backend (one JSON document per trail name, under a lock, heads kept 7 days) and writes
  an `audit.checkpoint` event with every stream's head. The verifier reports a stream that ends
  before a checkpointed head, a missing stream, or a head replaced. The tail after the last
  checkpoint is undetectable; `last_checkpoint()` and the command say when it was.
- `fastapi-alcazaba audit verify` (JSON lines, log prefixes skipped, keys from PEM files or
  environment variables) and `audit keygen` (private key 0600, no overwrite).
- Out of scope: retention and storage.

## Acceptance

Tampering, forging, reordering (existing), truncation, replacement and rotation tests; a live test
of shared checkpoints on every backend; audit.md "Verifying it".
