from datetime import datetime

from nitro.settings import settings
from nitro.utils.crypto import constant_time_compare, salted_hmac


class TokenGenerator:
    """
    Strategy object used to generate and check tokens for the password
    reset mechanism.
    """

    key_salt = 'nitro.utils.tokens.TokenGenerator'
    algorithm = None
    _secret = None
    _secret_fallbacks = None

    def __init__(self):
        self.algorithm = self.algorithm or 'sha256'

    def _get_secret(self):
        return self._secret or settings.SECRET_KEY

    def _set_secret(self, secret):
        self._secret = secret

    secret = property(_get_secret, _set_secret)

    def _get_fallbacks(self):
        if self._secret_fallbacks is None:
            return settings.SECRET_KEY_FALLBACKS
        return self._secret_fallbacks

    def _set_fallbacks(self, fallbacks):
        self._secret_fallbacks = fallbacks

    secret_fallbacks = property(_get_fallbacks, _set_fallbacks)

    def make_token(self, user):
        """
        Return a token that can be used once to do a password reset
        for the given user.
        """
        return self._make_token_with_timestamp(
            user,
            self._num_seconds(self._now()),
            self.secret,
        )

    def check_token(self, user, token):
        """
        Check that a password reset token is correct for a given user.
        """
        if not (user and token):
            return False
        # Parse the token
        try:
            ts_b36, _ = token.split('-')
        except ValueError:
            return False

        try:
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False

        # Check that the timestamp/uid has not been tampered with
        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(
                self._make_token_with_timestamp(user, ts, secret),
                token,
            ):
                break
        else:
            return False

        # Check the timestamp is within limit.
        if (self._num_seconds(self._now()) - ts) > settings.PASSWORD_RESET_TIMEOUT:
            return False

        return True

    def _make_token_with_timestamp(self, user, timestamp, secret):
        # timestamp is number of seconds since 2001-1-1. Converted to base 36,
        # this gives us a 6 digit string until about 2069.
        ts_b36 = int_to_base36(timestamp)
        hash_string = salted_hmac(
            self.key_salt,
            self._make_hash_value(user, timestamp),
            secret=secret,
            algorithm=self.algorithm,
        ).hexdigest()[
            ::2
        ]  # Limit to shorten the URL.
        return f'{ts_b36}-{hash_string}'

    def _make_hash_value(self, user, timestamp):
        """
        Hash the user's primary key, email (if available), and some user state
        that's sure to change after a password reset to produce a token that is
        invalidated when it's used:
        1. The password field will change upon a password reset (even if the
           same password is chosen, due to password salting).
        2. The last_login field will usually be updated very shortly after
           a password reset.
        Failing those things, settings.PASSWORD_RESET_TIMEOUT eventually
        invalidates the token.

        Running this data through salted_hmac() prevents password cracking
        attempts using the reset token, provided the secret isn't compromised.
        """
        # Truncate microseconds so that tokens are consistent even if the
        # database doesn't support microseconds.
        login_timestamp = (
            ''
            if user.last_login is None
            else user.last_login.replace(microsecond=0, tzinfo=None)
        )
        email_field = user.get_email_field_name()
        email = getattr(user, email_field, "") or ""
        return f'{user.id}{user.password}{login_timestamp}{timestamp}{email}''

    def _num_seconds(self, dt):
        return int((dt - datetime(2001, 1, 1)).total_seconds())

    def _now(self):
        # Used for mocking in tests
        return datetime.now()


def base36_to_int(s: str) -> int:
    """
    Convert a base 36 string to an int. Raise ValueError if the input won't fit
    into an int.
    """
    # To prevent overconsumption of server resources, reject any
    # base36 string that is longer than 13 base36 digits (13 digits
    # is sufficient to base36-encode any 64-bit integer)
    if len(s) > 13:
        raise ValueError('Base36 input too large')
    return int(s, 36)


def int_to_base36(i: int) -> str:
    """Convert an integer to a base36 string."""
    char_set = '0123456789abcdefghijklmnopqrstuvwxyz'
    if i < 0:
        raise ValueError('Negative base36 conversion input.')
    if i < 36:
        return char_set[i]
    b36_parts = []
    while i != 0:
        i, n = divmod(i, 36)
        b36_parts.append(char_set[n])
    return ''.join(reversed(b36_parts))
