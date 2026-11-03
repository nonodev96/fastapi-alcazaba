# Feature Specification: Mail attachments and an HTTP-API sender

**Feature Branch**: `feature/mail-attachments-and-api` · **Created**: 2026-11-04 · **Status**: Done

**Input**: "Apps should be able to send receipts and reports by e-mail, through SMTP or a
provider's HTTP API, with the same header-injection guarantees as today."

## Clarifications (no question asked; no backward compatibility kept)

- `Attachment(content: bytes, filename, content_type=None)`; `attachments=` in `message` and
  `send`. The name goes through `safe_filename` (default "attachment"); the content type is a
  bare `type/subtype` (RFC 6838 restricted names), never `multipart/*` nor `message/*`, guessed
  with `mimetypes` otherwise `application/octet-stream`. `MAX_ATTACHMENTS = 10`;
  `max_attachments_size` (10 MiB, before base64) for all of them together.
- The HTTP-API interface is `MailSender` (`async send(EmailMessage)`, raising `MailNotSent`). The
  reference implementation is `SESSender`: SESv2 `SendEmail` with `Content.Raw` (the message as
  built, CRLF), aiobotocore (the `ses` extra), AWS's credential chain, one client opened lazily
  or by `connect()`, `ClientError`/`BotoCoreError` → `MailNotSent`. One provider only.
- `SMTPSender` runs in a `ThreadPoolExecutor` of its own (`connections`, 4) instead of
  Starlette's threadpool; `check()` becomes a coroutine (and `smtp_check` async); `aclose()`.
- Live tests against Mailpit (CI service and `make services-up`): attachments read back whole,
  a name with CRLF doesn't add a Bcc, several messages at once, the health check.
- Out of scope: templates, DKIM, bounces, inline (`cid:`) images, other providers.

## Acceptance

Unit tests (attachments, limits, content types, names, threads, SES stubbed with botocore's
Stubber); Mailpit live tests; mail.md, reference, CHANGELOG.
