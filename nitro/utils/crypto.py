import hashlib
import hmac
import secrets

from nitro.settings import settings
from nitro.utils.encoding import ensure_bytes


class InvalidAlgorithm(ValueError):
    """Algorithm is not supported by hashlib."""

    pass


def salted_hmac(
    key_salt: str | bytes,
    value: str | bytes,
    secret: str | bytes | None = None,
    *,
    algorithm: str = 'sha1'
) -> hmac.HMAC:
    """
    Return the HMAC of 'value', using a key generated from key_salt and a
    secret (which defaults to settings.SECRET_KEY). Default algorithm is SHA1,
    but any algorithm name supported by hashlib can be passed.

    A different key_salt should be passed in for every application of HMAC.
    """
    if secret is None:
        secret = settings.SECRET_KEY

    key_salt = ensure_bytes(key_salt)
    secret = ensure_bytes(secret)
    try:
        hasher = getattr(hashlib, algorithm)
    except AttributeError as e:
        raise InvalidAlgorithm(
            f'{algorithm} is not an algorithm accepted by the hashlib module.'
        ) from e
    # We need to generate a derived key from our base key. We can do this by
    # passing the key_salt and our base key through a pseudo-random function.
    key = hasher(key_salt + secret).digest()
    # If len(key_salt + secret) > block size of the hash algorithm, the above
    # line is redundant and could be replaced by key = key_salt + secret, since
    # the hmac module does the same thing for keys longer than the block size.
    # However, we need to ensure that we *always* do this.
    return hmac.new(key, msg=ensure_bytes(value), digestmod=hasher)


RANDOM_STRING_CHARS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'


def get_random_string(length: int, allowed_chars: str = RANDOM_STRING_CHARS) -> str:
    """
    Return a securely generated random string.

    The bit length of the returned value can be calculated with the formula:
        log_2(len(allowed_chars)^length)

    For example, with default `allowed_chars` (26+26+10), this gives:
      * length: 12, bit length =~ 71 bits
      * length: 22, bit length =~ 131 bits
    """
    return ''.join(secrets.choice(allowed_chars) for i in range(length))
