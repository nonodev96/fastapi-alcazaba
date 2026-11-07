"""Transactional e-mail: a password reset, a sign-in code, a receipt with its invoice attached.
Over SMTP, which every provider speaks (Amazon SES, SendGrid, Postmark, Mailgun, your own server),
with TLS; or a provider's HTTP API (`SESSender`).

    mail = FastAPIMail(
        SMTPSender("email-smtp.eu-west-1.amazonaws.com", username=settings.smtp_user,
                   password=settings.smtp_password),
        from_address="Example <no-reply@example.com>",
    )

    await mail.send(user.email, "Reset your password", f"Open {link} within an hour.")

The body is never logged: it holds links and codes that open doors. A message that can't be sent
raises MailNotSent: send from a task of the queue (`FastAPITaskQueue`), which tries again.
"""

import asyncio
import logging
import mimetypes
import re
import smtplib
import ssl
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from pydantic import SecretStr

from fastapi_alcazaba.telemetry.metrics import Metrics
from fastapi_alcazaba.uploads.checks import safe_filename

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: Recipients of one message at most: transactional mail is for one person, or a few.
MAX_RECIPIENTS = 50
#: Attachments of one message at most.
MAX_ATTACHMENTS = 10
#: A content type without parameters (RFC 6838's restricted names): `application/pdf`, `text/csv`.
_CONTENT_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
#: A header's name (RFC 5322's field-name): printable ASCII but the colon, no spaces.
_FIELD_NAME = re.compile(r"[!-9;-~]{1,76}")
#: Headers a message may not be given: they're the library's, or would forge where it comes from
#: or where it goes (as would any `Resent-` header, which smtplib takes the envelope from).
_RESERVED = frozenset(
    {
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "date",
        "message-id",
        "sender",
        "return-path",
        "reply-to",
        "received",
        "auto-submitted",
    }
)


class MailNotSent(Exception):
    """The message wasn't sent: the server refused it (every recipient), didn't answer, or the
    TLS failed. Its recipients are in the message; its body never is."""


class MailSender(Protocol):
    """Where messages go: `send` gets each one, whole, and raises MailNotSent when it isn't
    sent. A provider's HTTP API is one more: `SESSender`, or a class of yours with this
    method."""

    async def send(self, message: EmailMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file attached to a message.

    :param content: its bytes.
    :param filename: the name the recipient sees; made safe with `safe_filename` (no path, no
        line breaks nor control characters), "attachment" when nothing is left.
    :param content_type: its type, `application/pdf`; guessed from the name when None,
        `application/octet-stream` when the name doesn't tell.
    """

    content: bytes
    filename: str
    content_type: str | None = None


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
    :param max_attachments_size: the bytes of a message's attachments, together, at most (before
        base64, which adds a third: providers count it, SES up to 40 MB, most others 10 to
        25 MB).
    :param metrics: where to count the messages (`fastapi_alcazaba.mail`), by outcome;
        FastAPICache's by default.
    """

    def __init__(
        self,
        sender: MailSender,
        *,
        from_address: str,
        reply_to: str | None = None,
        max_attachments_size: int = 10 * 1024 * 1024,
        metrics: Metrics | None = None,
    ) -> None:
        if max_attachments_size < 0:
            raise ValueError("max_attachments_size can't be negative")
        self.sender = sender
        self.from_address = _address(from_address, "from_address")
        self.reply_to = _address(reply_to, "reply_to") if reply_to is not None else None
        self.max_attachments_size = max_attachments_size
        self._metrics = metrics

    def message(
        self,
        to: str | Sequence[str],
        subject: str,
        text: str,
        *,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> EmailMessage:
        """The message, to `to` (an address or a few), with `text` and `html` as alternatives;
        `headers` adds others (`List-Unsubscribe`...), never those of the sender or the
        recipients; `attachments` after them. Raises ValueError for what would forge a header or
        a recipient, and for attachments over the limits."""
        files = self._attachments(attachments)
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
            if not _FIELD_NAME.fullmatch(name):
                raise ValueError(f"not a header's name: {name!r}")
            if name.lower() in _RESERVED or name.lower().startswith("resent-"):
                raise ValueError(f"the header {name} is FastAPIMail's")
            if any(char in value for char in "\r\n\0"):
                raise ValueError(f"the header {name} has no line breaks")
            message[name] = value
        message.set_content(text)
        if html is not None:
            message.add_alternative(html, subtype="html")
        for content, filename, content_type in files:
            maintype, _, subtype = content_type.partition("/")
            message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
        return message

    def _attachments(self, attachments: Sequence[Attachment]) -> list[tuple[bytes, str, str]]:
        """(content, safe name, content type) of each attachment, checked."""
        if len(attachments) > MAX_ATTACHMENTS:
            raise ValueError(f"a message has {MAX_ATTACHMENTS} attachments at most")
        files = []
        for attachment in attachments:
            if not isinstance(attachment.content, bytes):
                raise TypeError("an attachment's content is bytes")
            filename = safe_filename(attachment.filename, default="attachment")
            content_type = (
                attachment.content_type
                or mimetypes.guess_type(filename, strict=False)[0]
                or "application/octet-stream"
            ).lower()
            if not _CONTENT_TYPE.fullmatch(content_type) or content_type.startswith(
                ("multipart/", "message/")
            ):
                raise ValueError(f"not an attachment's content type: {content_type!r}")
            files.append((attachment.content, filename, content_type))
        if sum(len(content) for content, _, _ in files) > self.max_attachments_size:
            raise ValueError(
                f"the attachments take more than {self.max_attachments_size} bytes together"
            )
        return files

    async def send(
        self,
        to: str | Sequence[str],
        subject: str,
        text: str,
        *,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        """Build and send a message (see `message`); its Message-ID. Raises MailNotSent when it
        isn't sent."""
        message = self.message(
            to, subject, text, html=html, headers=headers, attachments=attachments
        )
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
    """Sends over SMTP, a connection each message, in threads of its own (not Starlette's, which
    the app's sync endpoints and dependencies share), always encrypted: STARTTLS
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
    :param connections: messages sent at once, each in a thread and a connection of its own; the
        others wait for one of them.
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
        connections: int = 4,
    ) -> None:
        if connections < 1:
            raise ValueError("connections must be at least 1")
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
        self.connections = connections
        self._executor: ThreadPoolExecutor | None = None

    async def send(self, message: EmailMessage) -> None:
        await self._in_thread(self._send, message)

    async def check(self) -> None:
        """Connect, start TLS, log in and say NOOP, as a send would, without sending anything;
        raises MailNotSent when any step fails (for a health check: `smtp_check`)."""
        await self._in_thread(self._check)

    async def aclose(self) -> None:
        """Let go of the threads, once the messages being sent are; the next `send` starts
        others."""
        executor, self._executor = self._executor, None
        if executor is not None:
            await asyncio.to_thread(executor.shutdown)

    async def _in_thread(self, function: Callable[..., None], *args: object) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                self.connections, thread_name_prefix="fastapi-alcazaba-smtp"
            )
        await asyncio.get_running_loop().run_in_executor(self._executor, function, *args)

    def _check(self) -> None:
        try:
            with self._connect() as client:
                code, _ = client.noop()
                if code != 250:
                    raise smtplib.SMTPResponseException(code, b"NOOP refused")
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailNotSent(f"{type(exc).__name__}: {str(exc)[:200]}") from exc

    def _connect(self) -> smtplib.SMTP:
        """A connection, past TLS and the login."""
        if self.tls == "implicit":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=self.ssl_context
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            client.ehlo()
            if self.tls == "starttls":
                client.starttls(context=self.ssl_context)
                client.ehlo()
            if self.username is not None and self._password is not None:
                client.login(self.username, self._password.get_secret_value())
        except BaseException:
            client.close()
            raise
        return client

    def _send(self, message: EmailMessage) -> None:
        try:
            with self._connect() as client:
                # The envelope from From and To, never from other headers.
                refused = client.send_message(
                    message,
                    from_addr=message["From"].addresses[0].addr_spec,
                    to_addrs=[address.addr_spec for address in message["To"].addresses],
                )
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
