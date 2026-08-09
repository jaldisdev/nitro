import importlib
from typing import TYPE_CHECKING

from nitro.mail.backends.base import BaseEmailBackend

if TYPE_CHECKING:
    from nitro.mail.message import EmailMessage


def get_connection(
    backend: str | None = None,
    fail_silently: bool = False,
    **kwargs,
) -> BaseEmailBackend:
    """
    Get an email backend connection.

    Args:
        backend: Backend class path (uses EMAIL_BACKEND from settings if not provided)
        fail_silently: If True, suppress connection errors
        **kwargs: Additional backend-specific options

    Returns:
        Instance of email backend

    Example:
        async with get_connection() as connection:
            await connection.send_messages([message1, message2])
    """
    from nitro.settings import settings

    if backend is None:
        backend = settings.EMAIL_BACKEND

    # Import the backend class
    if isinstance(backend, str):
        module_path, class_name = backend.rsplit(".", 1)
        module = importlib.import_module(module_path)
        backend_class = getattr(module, class_name)
    else:
        backend_class = backend

    # Build connection parameters from settings
    connection_params = {
        "host": getattr(settings, "EMAIL_HOST", "localhost"),
        "port": getattr(settings, "EMAIL_PORT", None),
        "username": getattr(settings, "EMAIL_HOST_USER", ""),
        "password": getattr(settings, "EMAIL_HOST_PASSWORD", ""),
        "use_tls": getattr(settings, "EMAIL_USE_TLS", False),
        "use_ssl": getattr(settings, "EMAIL_USE_SSL", False),
        "timeout": getattr(settings, "EMAIL_TIMEOUT", None),
        "fail_silently": fail_silently,
    }

    # Add backend-specific parameters

    # AWS SES parameters
    if "ses" in backend.lower():
        connection_params.update(
            {
                "region_name": getattr(settings, "EMAIL_AWS_REGION", "us-east-1"),
                "aws_access_key_id": getattr(settings, "EMAIL_AWS_ACCESS_KEY_ID", None),
                "aws_secret_access_key": getattr(
                    settings, "EMAIL_AWS_SECRET_ACCESS_KEY", None
                ),
                "aws_session_token": getattr(settings, "EMAIL_AWS_SESSION_TOKEN", None),
                "configuration_set_name": getattr(
                    settings, "EMAIL_SES_CONFIGURATION_SET", None
                ),
            }
        )

    # SendGrid parameters
    elif "sendgrid" in backend.lower():
        connection_params.update(
            {
                "api_key": getattr(settings, "EMAIL_SENDGRID_API_KEY", None),
                "sandbox_mode": getattr(settings, "EMAIL_SENDGRID_SANDBOX_MODE", False),
            }
        )

    # OAuth SMTP parameters
    elif "oauth" in backend.lower():
        connection_params.update(
            {
                "oauth2_token": getattr(settings, "EMAIL_OAUTH2_TOKEN", None),
                "oauth2_token_callback": getattr(
                    settings, "EMAIL_OAUTH2_TOKEN_CALLBACK", None
                ),
            }
        )

    # Override with any explicitly provided kwargs
    connection_params.update(kwargs)

    # Remove None values and keys not relevant to this backend
    connection_params = {
        k: v
        for k, v in connection_params.items()
        if v is not None or k in ["fail_silently"]
    }

    return backend_class(**connection_params)


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
