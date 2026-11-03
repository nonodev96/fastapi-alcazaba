# E-mail

!!! warning "Experimental"
    New in 0.7: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

The messages an app sends by itself: a password reset, a sign-in code, a receipt with its invoice.
`FastAPIMail` builds them so that nothing in them can add a header or a recipient, attachments
included, and `SMTPSender` sends them over SMTP, which every provider speaks (Amazon SES,
SendGrid, Postmark, Mailgun, your own server), always encrypted. Nothing to install. `SESSender`
sends them through Amazon SES's API instead.

Every class and function: [the API reference](../reference/mail.md).

```python
from fastapi_alcazaba import FastAPIMail
from fastapi_alcazaba.mail import SMTPSender

mail = FastAPIMail(
    SMTPSender(
        "email-smtp.eu-west-1.amazonaws.com",
        username=settings.smtp_user,
        password=settings.smtp_password,  # a SecretStr
    ),
    from_address="Example <no-reply@example.com>",
)

message_id = await mail.send(
    user.email,
    "Reset your password",
    f"Open {link} within an hour.",
    html=f'<p>Open <a href="{link}">this link</a> within an hour.</p>',
)
```

## The messages

- **One address per recipient,** `name <user@domain>` or `user@domain`, 50 at most: a string
  with a comma, a line break or two `@` is refused, so a form field can't add recipients.
- **Headers that can't be forged:** the subject is one line; `headers=` adds others
  (`List-Unsubscribe`...) but never `From`, `To`, `Cc`, `Bcc`, `Subject`, `Date`, `Message-ID`,
  `Reply-To` nor `Sender`, and nothing with a line break.
- **What FastAPIMail adds:** `Date`, a `Message-ID` on the sender's domain (`send` returns it),
  `Reply-To` when given, and `Auto-Submitted: auto-generated` (RFC 3834), so that out-of-office
  replies don't answer it.
- **Text, and HTML as its alternative:** mail clients show the one they can. Escape what goes
  into the HTML as you would in a page.

## Attachments

```python
from fastapi_alcazaba.mail import Attachment

await mail.send(
    user.email,
    "Your invoice",
    "Your invoice for October is attached.",
    attachments=[Attachment(pdf, f"Invoice {invoice.number}.pdf")],
)
```

- **The name the recipient sees** goes through [`safe_filename`](uploads.md): without a path,
  line breaks nor control characters (no header can come in through it), nor the right-to-left
  override that disguises an `.exe`; "attachment" when nothing is left. Names with accents or in
  other alphabets are encoded as RFC 2231 says.
- **The content type** is `type/subtype` without parameters (`application/pdf`, `text/csv`);
  guessed from the name when not given, `application/octet-stream` when the name doesn't tell.
  `multipart/*` and `message/*` are refused: they'd change the message's structure.
- **Limits:** 10 attachments a message (`MAX_ATTACHMENTS`), and 10 MiB of them together
  (`FastAPIMail(max_attachments_size=...)`), counted before base64, which adds a third: the
  providers count after it (SES up to 40 MB, most others 10 to 25 MB). Over the limits, or with
  a content that isn't `bytes`, `message` and `send` raise before anything is sent.
- **After the text and the HTML,** in a `multipart/mixed`. Images inside the HTML (`cid:`) are
  out of scope: link them.

## Sending

- **Encrypted:** STARTTLS on port 587 by default, or `tls="implicit"` on port 465, with the
  server's certificate checked against the system's authorities (`ssl_context` for one of your
  own). Plain SMTP, `tls="none"`, is only for `localhost`: a development server such as Mailpit.
- **A connection per message,** with `timeout` seconds for each step, in threads of the sender's
  own: `smtplib` blocks, and Starlette's threadpool (40 threads) is the one the app's sync
  endpoints and dependencies share, so a slow server would hold them. `connections` messages go
  at once (4 by default); the others wait for a thread. `await sender.aclose()` at shutdown lets
  them go.
- **When it isn't sent** (the server refuses every recipient, doesn't answer, the TLS or the
  login fails), `send` raises `MailNotSent` and logs who it was for, never the body: it holds the
  links and codes that open doors. When the server takes some recipients and refuses others, the
  message is sent: the refused ones are logged, and nothing is raised, so a retry doesn't send it
  to the others twice. Send from a [task](background.md#task-queue), which tries again:

```python
@queue.task(retries=5)
async def send_reset(email: str, link: str) -> None:
    await mail.send(email, "Reset your password", f"Open {link} within an hour.")
```

- **The metric** `fastapi_alcazaba.mail` counts the messages by outcome: sent or failed.

## Through a provider's API

`SESSender` sends through Amazon SES's API (SESv2 `SendEmail`) instead of SMTP, with aiobotocore
(`pip install "fastapi-alcazaba[ses]"`):

```python
from fastapi_alcazaba.mail import SESSender

sender = SESSender("eu-west-1", configuration_set="transactional")
mail = FastAPIMail(sender, from_address="Example <no-reply@example.com>")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await sender.connect()
    yield
    await sender.aclose()
```

- **The message goes whole,** as FastAPIMail built it (`Content.Raw`): its headers, its
  alternatives and its attachments, the same as over SMTP.
- **The credentials come from AWS's chain:** the task's role on ECS or Lambda, the instance's, the
  environment, a profile. No SMTP user to create and rotate, nor a port 587 to open from the
  app's network. Give the role `ses:SendEmail` and `ses:SendRawEmail` on the `From`'s identity:
  AWS's guide doesn't say which of them a raw message checks.
- **When SES refuses it** (an identity not verified, the account paused, too many messages) or
  can't be reached, `send` raises `MailNotSent` with SES's error code and message. SES sends to
  all the recipients or none.
- **One client,** opened on the first message or by `connect()` and closed by `aclose()`;
  `endpoint_url=` and the rest of `create_client`'s options go through (a VPC endpoint, an
  emulator).
- **Another provider:** a sender is any class with `async def send(self, message: EmailMessage)`
  that raises `MailNotSent` when the message isn't sent (`MailSender`). `message.as_bytes()` is
  the raw message, for APIs that take it (Mailgun's `messages.mime`); for those that take fields,
  `message.get_body(("plain",))`, `get_body(("html",))` and `iter_attachments()` give them.

## In tests

`MemoryMailSender` sends nothing, and keeps the messages, attachments included
(`message.iter_attachments()`):

```python
sender = MemoryMailSender()
mail = FastAPIMail(sender, from_address="no-reply@example.com")
...
(message,) = sender.messages
assert "reset?token=" in message.get_body(("plain",)).get_content()
```
