"""FastAPIMail, SMTPSender and SESSender: messages that can't forge a header or a recipient, with
attachments, sent over SMTP with TLS (STARTTLS and implicit) to a small server that speaks it, with
a certificate made here, in threads of the sender's own; and to SES's API, stubbed."""

import asyncio
import base64
import datetime
import smtplib
import socket
import socketserver
import ssl
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba.mail import (
    Attachment,
    FastAPIMail,
    MailNotSent,
    MemoryMailSender,
    SESSender,
    SMTPSender,
)
from fastapi_alcazaba.telemetry import Metrics
from tests.unit.test_metrics import points

PASSWORD = "smtp-password"  # noqa: S105  # the test server's


def certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for localhost, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class Server(socketserver.ThreadingTCPServer):
    """An SMTP server: EHLO, STARTTLS (or TLS from the start), AUTH PLAIN, MAIL, RCPT, DATA."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, context: ssl.SSLContext, implicit: bool) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.context = context
        self.implicit = implicit
        self.received: list[dict[str, Any]] = []
        self.refuse: set[str] = set()


class Handler(socketserver.StreamRequestHandler):
    server: Server

    def setup(self) -> None:
        if self.server.implicit:
            self.request = self.server.context.wrap_socket(self.request, server_side=True)
        super().setup()

    def finish(self) -> None:
        super().finish()
        # socketserver closes the socket it accepted, which TLS has taken over: close TLS's.
        if isinstance(self.request, ssl.SSLSocket):
            self.request.close()

    def say(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())
        self.wfile.flush()

    def starttls(self) -> bool:
        """Whether the conversation goes on encrypted."""
        self.say("220 go ahead")
        self.rfile.close()
        self.wfile.close()
        try:
            self.request = self.server.context.wrap_socket(self.request, server_side=True)
        except ssl.SSLError:
            return False
        self.rfile = self.request.makefile("rb")
        self.wfile = self.request.makefile("wb")
        return True

    def handle(self) -> None:
        tls = self.server.implicit
        user = None
        envelope: dict[str, Any] = {"to": []}
        self.say("220 localhost ESMTP")
        while line := self.rfile.readline().decode().strip():
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                extensions = ["AUTH PLAIN"] if tls else ["STARTTLS"]
                self.wfile.write(b"250-localhost\r\n")
                self.say("250 " + extensions[-1])
            elif verb == "STARTTLS":
                if not self.starttls():
                    return  # the client didn't trust the certificate, and hung up
                tls = True
            elif verb == "AUTH":
                _, _, secret = base64.b64decode(line.split()[2]).split(b"\0")
                if not tls or secret.decode() != PASSWORD:
                    self.say("535 authentication failed")
                    continue
                user = "user"
                self.say("235 ok")
            elif verb == "MAIL":
                if user is None:
                    self.say("530 authenticate first")
                    continue
                envelope["from"] = line.split(":", 1)[1].strip("<> ")
                self.say("250 ok")
            elif verb == "RCPT":
                address = line.split(":", 1)[1].strip("<> ")
                if address in self.server.refuse:
                    self.say("550 no such user")
                    continue
                envelope["to"].append(address)
                self.say("250 ok")
            elif verb == "DATA":
                self.say("354 go on")
                data = b""
                while (chunk := self.rfile.readline()) != b".\r\n":
                    data += chunk
                envelope["data"] = data
                self.server.received.append(envelope)
                envelope = {"to": []}
                self.say("250 queued")
            elif verb == "QUIT":
                self.say("221 bye")
                return
            else:
                self.say("250 ok")


@pytest.fixture(name="tls")
def fixture_tls() -> Iterator[tuple[ssl.SSLContext, ssl.SSLContext]]:
    """The server's TLS context, and a client's that trusts its certificate."""
    with tempfile.TemporaryDirectory() as directory:
        cert, key = certificate(Path(directory))
        server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server.load_cert_chain(cert, key)
        client = ssl.create_default_context(cafile=str(cert))
        yield server, client


@contextmanager
def serve(context: ssl.SSLContext, implicit: bool = False) -> Iterator[Server]:
    """The server, answering in a thread until the block ends."""
    with Server(context, implicit) as server:
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        try:
            yield server
        finally:
            server.shutdown()


def smtp_mail(server: Server, client: ssl.SSLContext, **options: Any) -> FastAPIMail:
    sender = SMTPSender(
        "localhost",
        server.server_address[1],
        username="user",
        password=PASSWORD,
        ssl_context=client,
        **options,
    )
    return FastAPIMail(sender, from_address="Example <no-reply@example.com>")


class TestSMTP:
    async def test_over_starttls(self, tls: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
        with serve(tls[0]) as server:
            message_id = await smtp_mail(server, tls[1]).send(
                "Ana <ana@example.com>", "Reset your password", "Open the link.", html="<p>Open</p>"
            )
        assert len(server.received) == 1
        received = server.received[0]
        assert (received["from"], received["to"]) == ("no-reply@example.com", ["ana@example.com"])
        parsed = message_from_bytes(received["data"])
        assert parsed["Subject"] == "Reset your password"
        assert parsed["Message-ID"] == message_id
        assert parsed["Auto-Submitted"] == "auto-generated"
        assert parsed.is_multipart()

    async def test_over_implicit_tls(self, tls: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
        with serve(tls[0], implicit=True) as server:
            await smtp_mail(server, tls[1], tls="implicit").send("ana@example.com", "Hi", "Text")
        assert len(server.received) == 1

    async def test_a_certificate_nobody_vouches_for(
        self,
        tls: tuple[ssl.SSLContext, ssl.SSLContext],
    ) -> None:
        with serve(tls[0]) as server:
            with pytest.raises(MailNotSent, match="SSLCertVerificationError|SSLError"):
                await smtp_mail(server, ssl.create_default_context()).send(
                    "ana@example.com", "Hi", "x"
                )
        assert server.received == []

    async def test_a_wrong_password(self, tls: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
        with serve(tls[0]) as server:
            sender = SMTPSender(
                "localhost",
                server.server_address[1],
                username="user",
                password=PASSWORD + "-wrong",
                ssl_context=tls[1],
            )
            mail = FastAPIMail(sender, from_address="a@example.com")
            with pytest.raises(MailNotSent, match="SMTPAuthenticationError"):
                await mail.send("b@example.com", "s", "t")

    async def test_a_recipient_refused(self, tls: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
        with serve(tls[0]) as server:
            server.refuse.add("gone@example.com")
            with pytest.raises(MailNotSent):
                await smtp_mail(server, tls[1]).send("gone@example.com", "Hi", "Text")

    async def test_some_recipients_refused(
        self,
        tls: tuple[ssl.SSLContext, ssl.SSLContext],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The others got it: MailNotSent would have a task retry send it to them again."""
        with serve(tls[0]) as server:
            server.refuse.add("gone@example.com")
            mail = smtp_mail(server, tls[1])
            await mail.send(["kept@example.com", "gone@example.com"], "Hi", "Text")
        assert [envelope["to"] for envelope in server.received] == [["kept@example.com"]]
        assert "refused ['gone@example.com']" in caplog.text

    async def test_nobody_listening(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        sender = SMTPSender("localhost", port, timeout=1)
        with pytest.raises(MailNotSent, match="ConnectionRefusedError"):
            await FastAPIMail(sender, from_address="a@example.com").send("b@example.com", "s", "t")

    async def test_in_threads_of_its_own(
        self,
        tls: tuple[ssl.SSLContext, ssl.SSLContext],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not Starlette's threadpool, which sync endpoints share: `connections` at once."""
        names: list[str] = []
        send_message = smtplib.SMTP.send_message

        def recorded(client: smtplib.SMTP, *args: Any, **kwargs: Any) -> Any:
            names.append(threading.current_thread().name)
            return send_message(client, *args, **kwargs)

        monkeypatch.setattr(smtplib.SMTP, "send_message", recorded)
        with serve(tls[0]) as server:
            mail = smtp_mail(server, tls[1], connections=2)
            await asyncio.gather(*(mail.send("ana@example.com", "Hi", "x") for _ in range(5)))
            sender = mail.sender
            assert isinstance(sender, SMTPSender)
            await sender.aclose()
            await sender.aclose()
            await mail.send("ana@example.com", "Hi again", "x")  # threads of its own again
            await sender.aclose()
        assert len(server.received) == 6
        assert all(name.startswith("fastapi-alcazaba-smtp") for name in names)
        assert len(set(names[:5])) <= 2

    def test_configuration(self) -> None:
        with pytest.raises(ValueError, match="connections"):
            SMTPSender("localhost", connections=0)
        with pytest.raises(ValueError, match="only for localhost"):
            SMTPSender("smtp.example.com", tls="none")
        with pytest.raises(ValueError, match="starttls"):
            SMTPSender("localhost", tls="ssl")
        with pytest.raises(ValueError, match="username and password"):
            SMTPSender("localhost", username="user")


class TestMessages:
    def mail(self) -> tuple[FastAPIMail, MemoryMailSender]:
        sender = MemoryMailSender()
        return FastAPIMail(
            sender, from_address="Example <no-reply@example.com>", reply_to="help@example.com"
        ), sender

    async def test_a_message(self) -> None:
        mail, sender = self.mail()
        await mail.send(
            ["ana@example.com", "Ben <ben@example.com>"],
            "Hello",
            "Text",
            headers={"List-Unsubscribe": "<https://example.com/u>"},
        )
        (message,) = sender.messages
        assert message["To"] == "ana@example.com, Ben <ben@example.com>"
        assert message["Reply-To"] == "help@example.com"
        assert message["List-Unsubscribe"] == "<https://example.com/u>"
        assert message["Message-ID"].endswith("@example.com>")

    @pytest.mark.parametrize(
        ("to", "error"),
        [
            ("ana@example.com\r\nBcc: eve@example.com", "no line breaks"),
            ("ana@example.com, eve@example.com", "one e-mail address"),
            ("not an address", "one e-mail address"),
            ("a@b@example.com", "one e-mail address"),
            ([], "1 to 50 recipients"),
            (["a@example.com"] * 51, "1 to 50 recipients"),
        ],
    )
    def test_recipients_that_arent_one_address(self, to: Any, error: str) -> None:
        mail, _ = self.mail()
        with pytest.raises(ValueError, match=error):
            mail.message(to, "Hello", "Text")

    @pytest.mark.parametrize(
        ("subject", "headers", "error"),
        [
            ("Hello\r\nBcc: eve@example.com", {}, "one line"),
            ("x" * 999, {}, "998 characters"),
            ("Hello", {"Bcc": "eve@example.com"}, "FastAPIMail's"),
            ("Hello", {"From": "boss@example.com"}, "FastAPIMail's"),
            ("Hello", {"X-Note": "a\r\nBcc: eve@example.com"}, "no line breaks"),
            ("Hello", {"X:Odd": "a"}, "no line breaks"),
        ],
    )
    def test_what_would_forge_a_header(
        self,
        subject: str,
        headers: dict[str, str],
        error: str,
    ) -> None:
        mail, _ = self.mail()
        with pytest.raises(ValueError, match=error):
            mail.message("ana@example.com", subject, "Text", headers=headers)

    def test_the_from_address(self) -> None:
        with pytest.raises(ValueError, match="from_address"):
            FastAPIMail(MemoryMailSender(), from_address="nobody")

    async def test_counted_and_the_body_never_logged(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Failing:
            async def send(self, message: Any) -> None:
                raise MailNotSent("down")

        reader = InMemoryMetricReader()
        metrics = Metrics(meter_provider=MeterProvider(metric_readers=[reader]))
        failing = FastAPIMail(Failing(), from_address="a@example.com", metrics=metrics)
        with pytest.raises(MailNotSent):
            await failing.send("b@example.com", "Reset", "https://example.com/reset?token=s3cret")
        await FastAPIMail(MemoryMailSender(), from_address="a@example.com", metrics=metrics).send(
            "b@example.com", "s", "t"
        )
        assert "s3cret" not in caplog.text
        counted = {
            a["fastapi_alcazaba.outcome"]: v for a, v in points(reader, "fastapi_alcazaba.mail")
        }
        assert counted == {"failed": 1, "sent": 1}


class TestAttachments:
    def mail(self, **options: Any) -> tuple[FastAPIMail, MemoryMailSender]:
        sender = MemoryMailSender()
        return FastAPIMail(sender, from_address="no-reply@example.com", **options), sender

    async def test_attached_after_the_alternatives(self) -> None:
        mail, sender = self.mail()
        pdf = b"%PDF-1.7\n..."
        await mail.send(
            "ana@example.com",
            "Your invoice",
            "Attached.",
            html="<p>Attached.</p>",
            attachments=[
                Attachment(pdf, "Invoice 2026-001.pdf"),
                Attachment(b"a,b\n1,2\n", "report.csv", "text/csv"),
                Attachment(b"\x00\x01", "data"),
            ],
        )
        (message,) = sender.messages
        assert message.get_content_type() == "multipart/mixed"
        plain, html = message.get_body(("plain",)), message.get_body(("html",))
        assert plain is not None
        assert html is not None
        assert plain.get_content() == "Attached.\n"
        assert html.get_content() == "<p>Attached.</p>\n"
        files = [
            (a.get_filename(), a.get_content_type(), a.get_payload(decode=True))
            for a in message.iter_attachments()
        ]
        assert files == [
            ("Invoice 2026-001.pdf", "application/pdf", pdf),
            ("report.csv", "text/csv", b"a,b\n1,2\n"),
            ("data", "application/octet-stream", b"\x00\x01"),
        ]

    def test_names_that_would_add_headers_or_paths(self) -> None:
        mail, _ = self.mail()
        message = mail.message(
            "ana@example.com",
            "Files",
            "x",
            attachments=[
                Attachment(b"x", "invoice.pdf\r\nBcc: eve@example.com"),
                Attachment(b"x", "../../etc/passwd"),
                Attachment(b"x", "\u202etxt.exe"),
                Attachment(b"x", "\r\n"),
                Attachment(b"x", "factura añoñ.pdf"),
            ],
        )
        names = [part.get_filename() for part in message.iter_attachments()]
        assert names == [
            "invoice.pdfBcc_ eve@example.com",
            "passwd",
            "txt.exe",
            "attachment",
            "factura añoñ.pdf",
        ]
        wire = message.as_bytes(policy=policy.SMTP)
        assert b"\r\nBcc:" not in wire
        assert message_from_bytes(wire)["Bcc"] is None

    @pytest.mark.parametrize(
        "content_type",
        [
            "text/html; charset=utf-8",
            "application/pdf\r\nBcc: x",
            "pdf",
            "multipart/mixed",
            "message/rfc822",
            "a/" + "b" * 200,
        ],
    )
    def test_content_types(self, content_type: str) -> None:
        mail, _ = self.mail()
        with pytest.raises(ValueError, match="content type"):
            mail.message(
                "a@example.com", "s", "t", attachments=[Attachment(b"x", "a", content_type)]
            )

    def test_limits(self) -> None:
        mail, _ = self.mail(max_attachments_size=10)
        ten = [Attachment(b"12345", "a"), Attachment(b"12345", "b")]
        assert (
            len(list(mail.message("a@example.com", "s", "t", attachments=ten).iter_attachments()))
            == 2
        )
        with pytest.raises(ValueError, match="more than 10 bytes"):
            mail.message("a@example.com", "s", "t", attachments=[*ten, Attachment(b"1", "c")])
        with pytest.raises(ValueError, match="10 attachments at most"):
            self.mail()[0].message(
                "a@example.com", "s", "t", attachments=[Attachment(b"", "a")] * 11
            )
        with pytest.raises(TypeError, match="bytes"):
            mail.message("a@example.com", "s", "t", attachments=[Attachment("text", "a")])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="negative"):
            self.mail(max_attachments_size=-1)


class TestSES:
    @staticmethod
    def sender(**options: Any) -> SESSender:
        return SESSender(
            "eu-west-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",  # noqa: S106  # no AWS is called: the client is stubbed
            **options,
        )

    async def test_the_raw_message(self) -> None:
        from botocore.stub import ANY, Stubber

        sender = self.sender(configuration_set="transactional")
        mail = FastAPIMail(sender, from_address="no-reply@example.com")
        client = await sender.connect()
        assert await sender.connect() is client
        sent: list[bytes] = []
        with Stubber(client) as stubber:
            stubber.add_response(
                "send_email",
                {"MessageId": "ses-1"},
                {"Content": {"Raw": {"Data": ANY}}, "ConfigurationSetName": "transactional"},
            )
            client.meta.events.register(
                "before-parameter-build.sesv2.SendEmail",
                lambda params, **_: sent.append(params["Content"]["Raw"]["Data"]),
            )
            message_id = await mail.send(
                "ana@example.com",
                "Invoice",
                "Attached.",
                attachments=[Attachment(b"%PDF", "a.pdf")],
            )
            stubber.assert_no_pending_responses()
        assert len(sent) == 1
        data = sent[0]
        assert b"\r\n" in data
        assert b"\n" not in data.replace(b"\r\n", b"")
        parsed = message_from_bytes(data, policy=policy.default)
        assert parsed["Message-ID"] == message_id
        assert [a.get_filename() for a in parsed.iter_attachments()] == ["a.pdf"]
        await sender.aclose()
        await sender.aclose()

    async def test_refused(self) -> None:
        from botocore.stub import Stubber

        sender = self.sender()
        client = await sender.connect()
        with Stubber(client) as stubber:
            stubber.add_client_error(
                "send_email", "MessageRejected", "Email address is not verified."
            )
            with pytest.raises(MailNotSent, match="MessageRejected: Email address is not verified"):
                await FastAPIMail(sender, from_address="a@example.com").send(
                    "b@example.com", "s", "t"
                )
        await sender.aclose()

    async def test_unreachable(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        from botocore.config import Config

        sender = self.sender(
            endpoint_url=f"http://127.0.0.1:{port}",
            config=Config(retries={"max_attempts": 1}, connect_timeout=1),
        )
        with pytest.raises(MailNotSent, match="EndpointConnectionError"):
            await FastAPIMail(sender, from_address="a@example.com").send("b@example.com", "s", "t")
        await sender.aclose()
