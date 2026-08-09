"""Standalone Intercom client for Python projects that do not run on Nitro.

Nitro projects should use ``nitro.intercom`` instead, which sources its
configuration from the project's settings object and reaches the same
publish/subscribe core in-process.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
