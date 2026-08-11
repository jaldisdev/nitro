from typing import TYPE_CHECKING, Any

from nitro.mail.backends.base import BaseEmailBackend
from nitro.utils.modules import import_string

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


def get_connection(
    backend: str | type[BaseEmailBackend] | None = None,
    fail_silently: bool = False,
    **kwargs: Any,
) -> BaseEmailBackend:
    """A backend built from the project's settings.

    Which settings those are is declared by the backend itself, in its
    ``settings_map``: a backend says what it wants rather than being guessed at
    from the shape of its import path, which used to hand AWS credentials to
    anything whose name happened to contain "ses".

    `kwargs` override whatever the settings say.

        async with get_connection() as connection:
            await connection.send_messages([first, second])
    """
    from nitro.settings import settings

    if backend is None:
        backend = settings.EMAIL_BACKEND

    backend_class = import_string(backend) if isinstance(backend, str) else backend

    configured: dict[str, Any] = {}
    for keyword, name in backend_class.settings_map.items():
        try:
            value = getattr(settings, name)
        except AttributeError:
            continue
        # A setting left as None means "not configured", and passing it would
        # override the backend's own default with nothing.
        if value is not None:
            configured[keyword] = value

    return backend_class(fail_silently=fail_silently, **{**configured, **kwargs})


async def send_email(
    subject: str,
    message: str,
    from_email: str | None = None,
    recipient_list: list[str] | None = None,
    fail_silently: bool = False,
    html_message: str | None = None,
) -> int:
    """
    Send a single email message.

    Args:
        subject: Email subject
        message: Plain text message body
        from_email: Sender email (uses DEFAULT_FROM_EMAIL if not provided)
        recipient_list: List of recipient email addresses
        fail_silently: If True, suppress exceptions
        html_message: Optional HTML version of the message

    Returns:
        Number of messages sent (0 or 1)

    Example:
        await send_email(
            subject='Welcome',
            message='Welcome to our service!',
            from_email='noreply@example.com',
            recipient_list=['user@example.com'],
            html_message='<p>Welcome to our service!</p>',
        )
    """
    from nitro.mail.message import EmailMessage

    email = EmailMessage(
        subject=subject,
        body=message,
        from_email=from_email,
        to=recipient_list or [],
    )

    if html_message:
        email.html = html_message

    return await email.send(fail_silently=fail_silently)


async def send_mass_email(
    datatuple: list[tuple],
    fail_silently: bool = False,
) -> int:
    """
    Send multiple email messages efficiently.

    Args:
        datatuple: List of tuples in format:
            (subject, message, from_email, recipient_list, html_message)
            The html_message is optional and can be None.
        fail_silently: If True, suppress exceptions

    Returns:
        Number of messages sent successfully

    Example:
        messages = [
            (
                'Welcome',
                'Welcome message',
                'noreply@example.com',
                ['user1@example.com'],
                '<p>Welcome</p>',
            ),
            (
                'Update',
                'Update message',
                'noreply@example.com',
                ['user2@example.com'],
                None,  # No HTML version
            ),
        ]

        num_sent = await send_mass_email(messages)
    """
    from nitro.mail.message import EmailMessage

    messages = []

    for data in datatuple:
        # Unpack with support for both 4 and 5 element tuples
        if len(data) == 5:
            subject, message, from_email, recipient_list, html_message = data
        else:
            subject, message, from_email, recipient_list = data
            html_message = None

        email = EmailMessage(
            subject=subject,
            body=message,
            from_email=from_email,
            to=recipient_list,
        )

        if html_message:
            email.html = html_message

        messages.append(email)

    # Send all messages using a single connection
    async with get_connection(fail_silently=fail_silently) as connection:
        return await connection.send_messages(messages, fail_silently=fail_silently)
