"""Transactional e-mail over SMTP, with TLS: `FastAPIMail` builds the messages, `SMTPSender`
sends them, `MemoryMailSender` keeps them in tests."""

from fastapi_alcazaba.mail.sending import (
    MAX_RECIPIENTS,
    FastAPIMail,
    MailNotSent,
    MailSender,
    MemoryMailSender,
    SMTPSender,
)

__all__ = [
    "MAX_RECIPIENTS",
    "FastAPIMail",
    "MailNotSent",
    "MailSender",
    "MemoryMailSender",
    "SMTPSender",
]
