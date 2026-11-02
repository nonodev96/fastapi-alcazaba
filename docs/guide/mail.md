# E-mail

!!! warning "Experimental"
    New in 0.7: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

The messages an app sends by itself: a password reset, a sign-in code, a receipt. `FastAPIMail`
builds them so that nothing in them can add a header or a recipient, and `SMTPSender` sends them
over SMTP, which every provider speaks (Amazon SES, SendGrid, Postmark, Mailgun, your own
server), always encrypted. Nothing to install.

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

## Sending

- **Encrypted:** STARTTLS on port 587 by default, or `tls="implicit"` on port 465, with the
  server's certificate checked against the system's authorities (`ssl_context` for one of your
  own). Plain SMTP, `tls="none"`, is only for `localhost`: a development server such as Mailpit.
- **A connection per message,** in a thread, with `timeout` seconds for each step.
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

## In tests

`MemoryMailSender` sends nothing, and keeps the messages:

```python
sender = MemoryMailSender()
mail = FastAPIMail(sender, from_address="no-reply@example.com")
...
(message,) = sender.messages
assert "reset?token=" in message.get_body(("plain",)).get_content()
```
