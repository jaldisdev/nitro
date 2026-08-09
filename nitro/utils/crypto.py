#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Signing and random values, for application code."""

import hashlib
import hmac
import secrets

from nitro.settings import settings

__all__ = [
    "RANDOM_STRING_CHARS",
    "InvalidAlgorithm",
    "constant_time_compare",
    "get_random_string",
    "salted_hmac",
]


class InvalidAlgorithm(ValueError):
    """Algorithm is not supported by hashlib."""


def _as_bytes(value: str | bytes) -> bytes:
    """`value` as UTF-8 bytes. Not a coercion: signing `str(object)` would sign
    whatever its repr happened to be."""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def constant_time_compare(left: str | bytes, right: str | bytes) -> bool:
    """Whether two values are equal, in time that does not depend on how.

    A plain ``==`` on a signature stops at the first differing byte, and how
    long that took tells an attacker how much of a forgery was right. This
    takes the same time either way.
    """
    return hmac.compare_digest(_as_bytes(left), _as_bytes(right))


def salted_hmac(
    key_salt: str | bytes,
    value: str | bytes,
    secret: str | bytes | None = None,
    *,
    algorithm: str = "sha1",
) -> hmac.HMAC:
    """An HMAC over `value`, keyed by `key_salt` together with `secret`.

    `secret` defaults to ``SECRET_KEY``. `algorithm` is any name `hashlib`
    answers to.

    Give each use of this its own `key_salt`. One secret signs sessions,
    password resets and unsubscribe links alike, and the salt is what stops a
    signature minted for one of those from being accepted as another.
    """
    if secret is None:
        secret = settings.SECRET_KEY

    try:
        hasher = getattr(hashlib, algorithm)
    except AttributeError as error:
        raise InvalidAlgorithm(f"hashlib offers no algorithm called {algorithm!r}.") from error

    # The key is the digest of salt and secret rather than the two joined.
    # Joining would leave the key as long as its inputs, and `hmac` hashes any
    # key longer than the block size anyway — so the short case and the long
    # case would derive the key differently. Hashing here makes it one case.
    key = hasher(_as_bytes(key_salt) + _as_bytes(secret)).digest()
    return hmac.new(key, msg=_as_bytes(value), digestmod=hasher)


RANDOM_STRING_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def get_random_string(length: int, allowed_chars: str = RANDOM_STRING_CHARS) -> str:
    """
    Return a securely generated random string.

    The bit length of the returned value can be calculated with the formula:
        log_2(len(allowed_chars)^length)

    For example, with default `allowed_chars` (26+26+10), this gives:
      * length: 12, bit length =~ 71 bits
      * length: 22, bit length =~ 131 bits
    """
    return "".join(secrets.choice(allowed_chars) for i in range(length))
