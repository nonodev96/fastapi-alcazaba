"""Sending through Amazon SES's HTTP API (SESv2 `SendEmail`), with aiobotocore:
pip install "fastapi-alcazaba[ses]".

    mail = FastAPIMail(SESSender("eu-west-1"), from_address="Example <no-reply@example.com>")

The message goes as FastAPIMail built it (raw MIME: its headers, alternatives and attachments),
and the credentials come from AWS's usual chain: the task's role on ECS or Lambda, the instance's,
the environment, a profile. No SMTP user to create and rotate, nor port 587 to open.
"""

import asyncio
import contextlib
import email.policy
import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from fastapi_alcazaba.mail.sending import MailNotSent

try:
    from aiobotocore.session import AioSession, get_session
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError as exc:  # pragma: no cover  # the tests have aiobotocore
    raise ImportError('SESSender needs aiobotocore: pip install "fastapi-alcazaba[ses]"') from exc

if TYPE_CHECKING:
    from types_aiobotocore_sesv2 import SESV2Client

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class SESSender:
    """Sends each message with SES's `SendEmail`, whole (`Content.Raw`), through one client
    opened on the first message (or `connect()`) and closed by `aclose()`. SES takes the
    recipients from the message's headers, and refuses it whole or sends it to all of them.

    The identity of the `From` (its domain, or the address) must be verified in SES, and, while
    the account is in the sandbox, every recipient too.

    :param region: SES's region (`eu-west-1`); AWS's configuration's by default.
    :param configuration_set: the configuration set to send with, for its event destinations
        (bounces, complaints...) and dedicated IPs.
    :param session: an aiobotocore session of your own; a new one by default.
    :param client_options: more for `create_client`: `endpoint_url` (a VPC endpoint, an
        emulator), credentials of their own...
    """

    def __init__(
        self,
        region: str | None = None,
        *,
        configuration_set: str | None = None,
        session: AioSession | None = None,
        **client_options: Any,
    ) -> None:
        self.region = region
        self.configuration_set = configuration_set
        self.session = session or get_session()
        self._client_options = client_options
        self._client: SESV2Client | None = None
        self._exit_stack = contextlib.AsyncExitStack()
        self._lock = asyncio.Lock()

    async def connect(self) -> "SESV2Client":
        """The client, opened now (at the app's start, say) when it isn't yet."""
        async with self._lock:
            if self._client is None:
                self._client = await self._exit_stack.enter_async_context(
                    self.session.create_client(
                        "sesv2", region_name=self.region, **self._client_options
                    )
                )
            return self._client

    async def send(self, message: EmailMessage) -> None:
        client = self._client or await self.connect()
        options: dict[str, Any] = {}
        if self.configuration_set is not None:
            options["ConfigurationSetName"] = self.configuration_set
        # The SMTP policy: CRLF line endings, as MIME has them on the wire.
        data = message.as_bytes(policy=email.policy.SMTP)
        try:
            await client.send_email(Content={"Raw": {"Data": data}}, **options)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            reason = f"{error.get('Code', 'ClientError')}: {str(error.get('Message', ''))[:200]}"
            raise MailNotSent(reason) from exc
        except BotoCoreError as exc:
            raise MailNotSent(f"{type(exc).__name__}: {str(exc)[:200]}") from exc

    async def aclose(self) -> None:
        """Close the client; the next message opens another."""
        async with self._lock:
            self._client = None
            await self._exit_stack.aclose()
