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
    message="Thanks for signing up.",
    recipient_list=["ada@example.com"],
    html_message="<p>Thanks for signing up.</p>",
)
```

## Building a message

```python
from nitro.mail import EmailAttachment, EmailMessage

message = EmailMessage(
    subject="Your report",
    body="Attached.",
    to=["ada@example.com"],
    cc=["team@example.com"],
    reply_to=["support@example.com"],
)
message.html = "<p>Attached.</p>"
message.attach(EmailAttachment(filename="report.pdf", content=pdf_bytes, mimetype="application/pdf"))

await message.send()
```

`EmailAttachment.from_file(path)` reads one from disk and guesses its type.

## Backends

| Backend | Needs |
|---|---|
| `nitro.mail.backends.console.ConsoleBackend` | nothing — prints instead of sending |
| `nitro.mail.backends.smtp.SMTPBackend` | nothing |
| `nitro.mail.backends.oauth_smtp.OAuth2SMTPBackend` | `nitro-framework[email-oauth]` |
| `nitro.mail.backends.ses.SESBackend` | `nitro-framework[aws]` |
| `nitro.mail.backends.sendgrid.SendGridBackend` | `nitro-framework[sendgrid]` |

`ConsoleBackend` is the default, so a project that has not configured mail
prints its messages rather than failing or silently dropping them.

## Sending several

```python
from nitro.mail import send_mass_email

await send_mass_email([
    ("Welcome", "Thanks for signing up.", None, ["ada@example.com"], "<p>Thanks.</p>"),
    ("Update", "Something changed.", None, ["grace@example.com"]),
])
```

Each entry is `(subject, message, from_email, recipient_list)` with an optional
HTML message as a fifth element; a `from_email` of `None` uses
`DEFAULT_FROM_EMAIL`.

One connection is opened for the batch rather than one per message.
