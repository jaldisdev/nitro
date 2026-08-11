from nitro.mail.backends.base import BaseEmailBackend
from nitro.mail.message import (
    EmailAttachment,
    EmailHeader,
    EmailMessage,
    EmailRecipient,
)
from nitro.mail.utils import get_connection, send_email, send_mass_email

__all__ = [
    # Backend base
    "BaseEmailBackend",
    "EmailAttachment",
    "EmailHeader",
    # Core classes
    "EmailMessage",
    "EmailRecipient",
    "get_connection",
    # Convenience functions
    "send_email",
    "send_mass_email",
]
