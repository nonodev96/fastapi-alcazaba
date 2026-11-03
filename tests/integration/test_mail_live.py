"""FastAPIMail and SMTPSender against a real SMTP server, Mailpit, enabled by its variables:

    MAILPIT_URL=http://127.0.0.1:8025      (its API, to read the messages back)
    MAILPIT_SMTP_PORT=1025                 (its SMTP port, on localhost, plain)

Messages with alternatives and attachments arrive as they were built, several at once, and a
name that would add a header doesn't. Each test's messages have a subject of their own.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba.mail import Attachment, FastAPIMail, SMTPSender
from fastapi_alcazaba.observability import smtp_check

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.7\n" + bytes(range(256)) * 64


@pytest.fixture(name="mailpit")
async def fixture_mailpit() -> AsyncIterator[tuple[FastAPIMail, httpx.AsyncClient]]:
    url, port = os.environ.get("MAILPIT_URL"), os.environ.get("MAILPIT_SMTP_PORT")
    if not url or not port:
        pytest.skip("MAILPIT_URL and MAILPIT_SMTP_PORT not set")
    sender = SMTPSender("127.0.0.1", int(port), tls="none", connections=3)
    async with httpx.AsyncClient(base_url=url, timeout=10) as api:
        yield FastAPIMail(sender, from_address="Example <no-reply@example.com>"), api
    await sender.aclose()


async def received(api: httpx.AsyncClient, subject: str, count: int = 1) -> list[dict[str, Any]]:
    """The messages with `subject`, once `count` have arrived."""
    for _ in range(50):
        found = (await api.get("/api/v1/search", params={"query": f'subject:"{subject}"'})).json()
        if found["messages_count"] >= count:
            return [
                (await api.get(f"/api/v1/message/{summary['ID']}")).json()
                for summary in found["messages"]
            ]
        await asyncio.sleep(0.1)
    raise AssertionError(f"{count} messages {subject!r} didn't arrive")


async def test_attachments_arrive_whole(mailpit: tuple[FastAPIMail, httpx.AsyncClient]) -> None:
    mail, api = mailpit
    subject = f"Invoice {uuid.uuid4().hex}"
    message_id = await mail.send(
        "Ana <ana@example.com>",
        subject,
        "Your invoice is attached.",
        html="<p>Your invoice is <b>attached</b>.</p>",
        attachments=[
            Attachment(PDF, "Factura 2026-001 (año).pdf"),
            Attachment(b"a,b\r\n1,2\r\n", "report.csv", "text/csv"),
            Attachment(b"x", "evil.txt\r\nBcc: eve@example.com"),
        ],
    )
    (message,) = await received(api, subject)
    assert message["MessageID"] == message_id.strip("<>")
    assert [to["Address"] for to in message["To"]] == ["ana@example.com"]
    assert message["Bcc"] == []
    assert message["Text"].strip() == "Your invoice is attached."
    assert "<b>attached</b>" in message["HTML"]
    files = {part["FileName"]: part for part in message["Attachments"]}
    assert set(files) == {
        "Factura 2026-001 (año).pdf",
        "report.csv",
        "evil.txtBcc_ eve@example.com",
    }
    assert files["report.csv"]["ContentType"] == "text/csv"
    pdf = files["Factura 2026-001 (año).pdf"]
    assert pdf["ContentType"] == "application/pdf"
    content = await api.get(f"/api/v1/message/{message['ID']}/part/{pdf['PartID']}")
    assert content.content == PDF
    headers = (await api.get(f"/api/v1/message/{message['ID']}/headers")).json()
    assert headers["Auto-Submitted"] == ["auto-generated"]


async def test_several_at_once(mailpit: tuple[FastAPIMail, httpx.AsyncClient]) -> None:
    mail, api = mailpit
    subject = f"Code {uuid.uuid4().hex}"
    await asyncio.gather(*(mail.send(f"user{n}@example.com", subject, "1234") for n in range(8)))
    messages = await received(api, subject, 8)
    assert sorted(m["To"][0]["Address"] for m in messages) == sorted(
        f"user{n}@example.com" for n in range(8)
    )


async def test_the_health_check(mailpit: tuple[FastAPIMail, httpx.AsyncClient]) -> None:
    mail, _ = mailpit
    assert isinstance(mail.sender, SMTPSender)
    await smtp_check(mail.sender).check()  # type: ignore[misc]
