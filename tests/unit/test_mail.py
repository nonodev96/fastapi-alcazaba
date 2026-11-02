"""FastAPIMail and SMTPSender: messages that can't forge a header or a recipient, sent over SMTP
with TLS (STARTTLS and implicit) to a small server that speaks it, with a certificate made here."""

import base64
import datetime
import socket
import socketserver
import ssl
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from email import message_from_bytes
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from fastapi_alcazaba.mail import FastAPIMail, MailNotSent, MemoryMailSender, SMTPSender
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

    async def test_nobody_listening(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        sender = SMTPSender("localhost", port, timeout=1)
        with pytest.raises(MailNotSent, match="ConnectionRefusedError"):
            await FastAPIMail(sender, from_address="a@example.com").send("b@example.com", "s", "t")

    def test_configuration(self) -> None:
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
