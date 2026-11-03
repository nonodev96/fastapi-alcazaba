# Feature Specification: Verifying the webhooks of the senders apps use

**Feature Branch**: `feature/webhook-verifiers` · **Created**: 2026-11-03 · **Status**: Draft

**Input**: "Apps should verify incoming webhooks from the senders they actually use, with the same
safety guarantees as today's Standard Webhooks check."

## Clarifications

### Session 2026-11-03

No question was put to the maintainer, who asked for it to be built and merged; the choices are
in research.md. No backward compatibility is kept (nobody uses the library).

- One dependency, `FastAPIWebhooks(secret, scheme=...)`: "standard" (v1 HMAC and v1a Ed25519),
  "github", "stripe", "slack" (R1).
- GitHub signs no time: its deliveries are checked for replays by delivery id within the same
  window, and the guide says a capture can be replayed later (R3).
- Senders that send no id (Stripe, Slack): the message's id is a digest of what was signed (R3).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Standard Webhooks v1a (Priority: P1)

**Acceptance**: a message signed with an Ed25519 key (`whsk_`) is accepted by a verifier given
the public key (`whpk_`); `WebhookSigner` signs with either kind; v1 and v1a in one header both
count.

### User Story 2 - GitHub, Stripe, Slack (Priority: P1)

**Acceptance**: each provider's published example verifies (GitHub's and Slack's documentation
vectors; Stripe's algorithm); a body or a header changed, a timestamp out of `tolerance`, a
replay, all get the same 401; missing or malformed headers a 400.

### Edge Cases

- A secret of the wrong kind for the scheme (a `whpk_` for GitHub): refused when set up.
- Several secrets: any of them verifies (rotation), for every scheme.
- Ed25519 without `cryptography` installed: a clear ImportError when set up.

## Requirements *(mandatory)*

- **FR-001**: Schemes standard (v1, v1a), github (`X-Hub-Signature-256`), stripe
  (`Stripe-Signature`, `t=` within `tolerance`), slack (`v0`, `X-Slack-Request-Timestamp`).
- **FR-002**: Constant-time comparison, `tolerance`, replay marks, `max_body_size`, raw body: the
  same code for every scheme; one 401 that doesn't say which check failed.
- **FR-003**: Lists of secrets rotate for every scheme.
- **FR-004**: The metric `webhooks` says the scheme.

## Success Criteria

- **SC-001**: Tests with the published vectors; the fuzz target covers every scheme.
- **SC-002**: The webhooks guide has a section per sender.
