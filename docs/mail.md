# Mail

Messages are built with the standard library's `EmailMessage` underneath, so
anything that library can express is available.

```python
EMAIL_BACKEND = "nitro.mail.backends.smtp.SMTPBackend"
EMAIL_HOST = "smtp.example.com"
EMAIL_PORT = 587
EMAIL_HOST_USER = "postmaster@example.com"
EMAIL_HOST_PASSWORD = "..."
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = "noreply@example.com"
```

```python
from nitro.mail import send_email

await send_email(
    subject="Welcome",
    body="Thanks for signing up.",
    to=["ada@example.com"],
)
```

## Building a message

```python
from nitro.mail import EmailMessage

message = EmailMessage(
    subject="Your report",
    body="Attached.",
    to=["ada@example.com"],
    cc=["team@example.com"],
    reply_to=["support@example.com"],
)
message.attach("report.pdf", pdf_bytes, "application/pdf")
message.attach_alternative("<p>Attached.</p>", "text/html")

await message.send()
```

## Backends

| Backend | Needs |
|---|---|
| `nitro.mail.backends.console.ConsoleBackend` | nothing — prints instead of sending |
| `nitro.mail.backends.smtp.SMTPBackend` | nothing |
| `nitro.mail.backends.oauth_smtp.OAuthSMTPBackend` | `nitro[email-oauth]` |
| `nitro.mail.backends.ses.SESBackend` | `nitro[aws]` |
| `nitro.mail.backends.sendgrid.SendGridBackend` | `nitro[sendgrid]` |

`ConsoleBackend` is the default, so a project that has not configured mail
prints its messages rather than failing or silently dropping them.

## Sending several

```python
from nitro.mail import send_mass_email

await send_mass_email([first, second, third])
```

One connection is opened for the batch rather than one per message.
