"""Transactional e-mail: a password reset, a sign-in code, a receipt. Over SMTP, which every
provider speaks (Amazon SES, SendGrid, Postmark, Mailgun, your own server), with TLS.

    mail = FastAPIMail(
        SMTPSender("email-smtp.eu-west-1.amazonaws.com", username=settings.smtp_user,
                   password=settings.smtp_password),
        from_address="Example <no-reply@example.com>",
    )

    await mail.send(user.email, "Reset your password", f"Open {link} within an hour.")

The body is never logged: it holds links and codes that open doors. A message that can't be sent
raises MailNotSent: send from a task of the queue (`FastAPITaskQueue`), which tries again.
"""

import logging
import smtplib
import ssl
from collections import deque
from collections.abc import Mapping, Sequence
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from fastapi_alcazaba.telemetry.metrics import Metrics

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Recipients of one message at most: transactional mail is for one person, or a few.
MAX_RECIPIENTS = 50
#: Headers a message may not be given: they're the library's, or would forge where it comes from.
_RESERVED = frozenset(
    {"from", "to", "cc", "bcc", "subject", "date", "message-id", "sender", "return-path"}
)


class MailNotSent(Exception):
    """The message wasn't sent: the server refused it (every recipient), didn't answer, or the
    TLS failed. Its recipients are in the message; its body never is."""


class MailSender(Protocol):
    """Where messages go: `send` gets each one, whole, and raises MailNotSent when it isn't
    sent."""

    async def send(self, message: EmailMessage) -> None: ...


def _address(value: str, what: str) -> str:
    """One address, `name <user@domain>` or `user@domain`, without what could add headers or
    other recipients."""
    if not isinstance(value, str) or any(char in value for char in "\r\n\0"):
        raise ValueError(f"the {what} has no line breaks: {value!r}")
    name, address = parseaddr(value)
    if not address or "@" not in address or "," in address or value.count("@") != 1:
        raise ValueError(f"the {what} is one e-mail address: {value!r}")
    username, _, domain = address.rpartition("@")
    try:
        return str(Address(display_name=name, username=username, domain=domain))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"the {what} isn't an e-mail address: {value!r}") from exc


class FastAPIMail:
    """Builds the messages and sends them through `sender`.

    :param sender: where they go: an `SMTPSender`, or `MemoryMailSender` in tests.
    :param from_address: the `From` of every message (`"Example <no-reply@example.com>"`).
    :param reply_to: the address replies go to, when it isn't `from_address`.
    :param metrics: where to count the messages (`fastapi_alcazaba.mail`), by outcome;
        FastAPICache's by default.
    """

    def __init__(
        self,
        sender: MailSender,
        *,
        from_address: str,
        reply_to: str | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.sender = sender
        self.from_address = _address(from_address, "from_address")
        self.reply_to = _address(reply_to, "reply_to") if reply_to is not None else None
        self._metrics = metrics

    def message(
        self,
        to: str | Sequence[str],
        subject: str,
        text: str,
        *,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> EmailMessage:
        """The message, to `to` (an address or a few), with `text` and `html` as alternatives;
        `headers` adds others (`List-Unsubscribe`...), never those of the sender or the
        recipients. Raises ValueError for what would forge a header or a recipient."""
        recipients = [to] if isinstance(to, str) else list(to)
        if not 1 <= len(recipients) <= MAX_RECIPIENTS:
            raise ValueError(f"a message has 1 to {MAX_RECIPIENTS} recipients")
        if any(char in subject for char in "\r\n\0") or len(subject) > 998:
            raise ValueError("a subject is one line, 998 characters at most")
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = ", ".join(_address(recipient, "recipient") for recipient in recipients)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = make_msgid(domain=self.from_address.rpartition("@")[2].rstrip(">"))
        if self.reply_to is not None:
            message["Reply-To"] = self.reply_to
        message["Auto-Submitted"] = "auto-generated"  # RFC 3834: no auto-replies to it
        for name, value in (headers or {}).items():
            if name.lower() in _RESERVED or name.lower() == "reply-to":
                raise ValueError(f"the header {name} is FastAPIMail's")
            if any(char in name + value for char in "\r\n\0") or ":" in name:
                raise ValueError(f"the header {name!r} has no line breaks")
            message[name] = value
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
        return message

    async def send(
        self,
        to: str | Sequence[str],
        subject: str,
        text: str,
        *,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Build and send a message (see `message`); its Message-ID. Raises MailNotSent when it
        isn't sent."""
        message = self.message(to, subject, text, html=html, headers=headers)
        try:
            await self.sender.send(message)
        except MailNotSent:
            self._count("failed")
            logger.warning("Mail to %s not sent: %r", message["To"], message["Subject"])
            raise
        self._count("sent")
        return str(message["Message-ID"])

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            metrics = self._metrics
        else:
            from fastapi_alcazaba.response_cache.caches import FastAPICache

            metrics = FastAPICache.get_metrics()
        metrics.count("mail", outcome=outcome)


class SMTPSender:
    """Sends over SMTP, in a thread, a connection each message, always encrypted: STARTTLS
    on port 587 (the default), implicit TLS with `tls="implicit"` (port 465). The server's
    certificate is checked against the system's authorities; plain SMTP is only for
    `localhost` (`tls="none"`: a development server such as Mailpit).

    :param host: the SMTP server.
    :param port: its port: 587 for STARTTLS, 465 for implicit TLS.
    :param username: the account (an SES SMTP user, `apikey` for SendGrid...); None for none.
    :param password: its password (a SecretStr from your settings).
    :param tls: "starttls", "implicit", or "none" for localhost.
    :param timeout: seconds for each step of the conversation.
    :param ssl_context: the TLS context, for a server with an authority of its own.
    """

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        username: str | None = None,
        password: str | SecretStr | None = None,
        tls: str = "starttls",
        timeout: float = 10.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if tls not in ("starttls", "implicit", "none"):
            raise ValueError('tls is "starttls", "implicit" or "none"')
        if tls == "none" and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("plain SMTP is only for localhost: credentials and links travel in it")
        if (username is None) != (password is None):
            raise ValueError("give username and password, or neither")
        self.host = host
        self.port = port
        self.username = username
        self._password = SecretStr(password) if isinstance(password, str) else password
        self.tls = tls
        self.timeout = timeout
        self.ssl_context = ssl_context or ssl.create_default_context()

    async def send(self, message: EmailMessage) -> None:
        await run_in_threadpool(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        try:
            if self.tls == "implicit":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=self.ssl_context
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with client:
                client.ehlo()
                if self.tls == "starttls":
                    client.starttls(context=self.ssl_context)
                    client.ehlo()
                if self.username is not None and self._password is not None:
                    client.login(self.username, self._password.get_secret_value())
                refused = client.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailNotSent(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        if refused:
            # The others got it: MailNotSent would have the task send it to them again.
            logger.warning(
                "Mail %s sent, but the server refused %s",
                message["Message-ID"],
                sorted(refused),
            )


class MemoryMailSender:
    """Keeps the last `maxlen` messages in `messages`, sending nothing: for tests, and to look at
    them while developing."""

    def __init__(self, maxlen: int | None = 1000) -> None:
        self.messages: deque[EmailMessage] = deque(maxlen=maxlen)

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)
