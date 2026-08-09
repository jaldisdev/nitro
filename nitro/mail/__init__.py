from nitro.mail.backends.base import BaseEmailBackend
from nitro.mail.message import (
    EmailAttachment,
    EmailHeader,
    EmailMessage,
    EmailRecipient,
)
from nitro.mail.utils import get_connection, send_email, send_mass_email

__all__ = [
    # Core classes
    "EmailMessage",
    "EmailAttachment",
    "EmailRecipient",
    "EmailHeader",
    # Backend base
    "BaseEmailBackend",
    # Convenience functions
    "send_email",
    "send_mass_email",
    "get_connection",
]
