"""Transactional e-mail, with attachments: `FastAPIMail` builds the messages, `SMTPSender` sends
them over SMTP with TLS, `SESSender` through Amazon SES's API, `MemoryMailSender` keeps them in
tests.

`SESSender` needs the `ses` extra: it's imported when first used.
"""

from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.mail.sending import (
    MAX_ATTACHMENTS,
    MAX_RECIPIENTS,
    Attachment,
    FastAPIMail,
    MailNotSent,
    MailSender,
    MemoryMailSender,
    SMTPSender,
)

if TYPE_CHECKING:
    from fastapi_alcazaba.mail.ses import SESSender

__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_RECIPIENTS",
    "Attachment",
    "FastAPIMail",
    "MailNotSent",
    "MailSender",
    "MemoryMailSender",
    "SESSender",
    "SMTPSender",
]


def __getattr__(name: str) -> Any:
    if name == "SESSender":
        from fastapi_alcazaba.mail import ses

        return ses.SESSender
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
