"""
Internationalization support for Nitro framework.

This module provides gettext-based translation functions without requiring
any external dependencies. It can work with or without actual gettext catalogs.
"""

import contextlib
import gettext as gettext_module
import re
from contextlib import ContextDecorator
from pathlib import Path

from .lazy import lazy

__all__ = [
    "activate",
    "check_for_language",
    "deactivate",
    "deactivate_all",
    "get_language",
    "get_language_bidi",
    "gettext",
    "gettext_lazy",
    "gettext_noop",
    "ngettext",
    "ngettext_lazy",
    "npgettext",
    "npgettext_lazy",
    "override",
    "pgettext",
    "pgettext_lazy",
    "to_language",
    "to_locale",
]


class TranslatorCommentWarning(SyntaxWarning):
    """Warning for translator comments in code."""


# Thread-local storage for active language
_active = None  # Can be replaced with threading.local() for thread safety


class NitroTranslation:
    """
    Wrapper around gettext.GNUTranslations that provides a convenient
    translation interface.
    """

    def __init__(
        self, language: str = "en", domain: str = "messages", localedirs: list | None = None
    ):
        """
        Create a translation object for the given language.

        Args:
            language: Language code (e.g., 'en', 'de', 'fr-ca')
            domain: Translation domain (default: 'messages')
            localedirs: List of directories to search for .mo files
        """
        self._language = language
        self._locale = to_locale(language)
        self._domain = domain
        self._catalog = None
        self._plural = lambda n: int(n != 1)  # Default plural rule

        # Try to load gettext catalog
        if localedirs:
            for localedir in localedirs:
                localedir_path = Path(localedir)
                locale_path = localedir_path / self._locale / "LC_MESSAGES"
                mo_file = locale_path / f"{domain}.mo"

                if mo_file.exists():
                    try:
                        with open(mo_file, "rb") as fp:
                            translation = gettext_module.GNUTranslations(fp)
                            self._catalog = translation._catalog
                            self._plural = translation.plural
                            break
                    except Exception:
                        pass

    def gettext(self, message: str) -> str:
        """Translate a message."""
        if self._catalog:
            return self._catalog.get(message, message)
        return message

    def ngettext(self, singular: str, plural: str, number: int) -> str:
        """Translate a message with plural forms."""
        if self._catalog:
            try:
                return self._catalog[(singular, self._plural(number))]
            except KeyError:
                pass
        return singular if number == 1 else plural

    def pgettext(self, context: str, message: str) -> str:
        """Translate a message with context."""
        msg_with_ctx = f"{context}\x04{message}"
        if self._catalog:
            result = self._catalog.get(msg_with_ctx)
            if result:
                return result
        return message

    def npgettext(self, context: str, singular: str, plural: str, number: int) -> str:
        """Translate a message with context and plural forms."""
        msg_with_ctx = f"{context}\x04{singular}"
        if self._catalog:
            try:
                return self._catalog[(msg_with_ctx, self._plural(number))]
            except KeyError:
                pass
        return singular if number == 1 else plural

    def gettext_noop(self, message: str) -> str:
        """Mark a string for translation without translating it."""
        return message


class NullTranslation:
    """
    No-op translation for when USE_I18N is False.
    This is purely for performance.
    """

    def __init__(self, language: str = "en"):
        self._language = language

    def gettext(self, message: str) -> str:
        return message

    def ngettext(self, singular: str, plural: str, number: int) -> str:
        return singular if number == 1 else plural

    def pgettext(self, context: str, message: str) -> str:
        return message

    def npgettext(self, context: str, singular: str, plural: str, number: int) -> str:
        return singular if number == 1 else plural

    def gettext_noop(self, message: str) -> str:
        return message


class TranslationManager:
    """
    Manages the current translation state.
    Can be configured to use real translations or null translations.
    """

    def __init__(
        self,
        default_language: str = "en",
        use_i18n: bool = True,
        locale_paths: list | None = None,
        domain: str = "messages",
    ):
        self.default_language = default_language
        self.use_i18n = use_i18n
        self.locale_paths = locale_paths or []
        self.domain = domain
        self._translations = {}
        self._current_language = None

    def _get_translation(self, language: str):
        """Get or create a translation object for the given language."""
        if language not in self._translations:
            if self.use_i18n:
                self._translations[language] = NitroTranslation(
                    language, self.domain, self.locale_paths
                )
            else:
                self._translations[language] = NullTranslation(language)
        return self._translations[language]

    @property
    def _trans(self):
        """Get the current translation object."""
        lang = self._current_language or self.default_language
        return self._get_translation(lang)

    def activate(self, language: str):
        """Activate a language."""
        self._current_language = language

    def deactivate(self):
        """Deactivate the current language, falling back to default."""
        self._current_language = None

    def deactivate_all(self):
        """Deactivate all translations."""
        self._current_language = None

    def get_language(self) -> str:
        """Get the current active language."""
        return self._current_language or self.default_language

    def get_language_bidi(self) -> bool:
        """Check if the current language is bidirectional."""
        # List of known RTL languages
        RTL_LANGUAGES = {"ar", "fa", "he", "ur", "yi"}
        lang = self.get_language()
        return lang.split("-")[0] in RTL_LANGUAGES

    def check_for_language(self, lang_code: str) -> bool:
        """Check if a language is available."""
        # In a simple implementation, we just check if it looks valid
        return bool(re.match(r"^[a-z]{2}(-[a-z]{2})?$", lang_code.lower()))

    def gettext(self, message: str) -> str:
        return self._trans.gettext(message)

    def ngettext(self, singular: str, plural: str, number: int) -> str:
        return self._trans.ngettext(singular, plural, number)

    def pgettext(self, context: str, message: str) -> str:
        return self._trans.pgettext(context, message)

    def npgettext(self, context: str, singular: str, plural: str, number: int) -> str:
        return self._trans.npgettext(context, singular, plural, number)

    def gettext_noop(self, message: str) -> str:
        return self._trans.gettext_noop(message)


# Global translation manager
_manager = None


def configure_translation(
    default_language: str = "en",
    use_i18n: bool = True,
    locale_paths: list | None = None,
    domain: str = "messages",
):
    """
    Configure the translation system.

    Args:
        default_language: Default language code (e.g., 'en')
        use_i18n: Whether to use internationalization
        locale_paths: List of paths to search for translation files
        domain: Translation domain (default: 'messages')

    Example:
        configure_translation(
            default_language='en',
            use_i18n=True,
            locale_paths=['/path/to/locale'],
            domain='messages'
        )
    """
    global _manager
    _manager = TranslationManager(
        default_language=default_language,
        use_i18n=use_i18n,
        locale_paths=locale_paths,
        domain=domain,
    )


def _get_manager():
    """Get the global translation manager, creating a default one if needed."""
    global _manager
    if _manager is None:
        _manager = TranslationManager()
    return _manager


# Public API functions


def gettext(message: str) -> str:
    """
    Translate a message.

    Usage:
        from nitro.utils.translation import gettext as _
        text = _('Hello World')
    """
    return _get_manager().gettext(message)


def gettext_noop(message: str) -> str:
    """
    Mark a string for translation without translating it.
    Useful for strings that will be translated later.

    Usage:
        MESSAGES = [gettext_noop('Error'), gettext_noop('Warning')]
        # Later:
        translated = gettext(msg)
    """
    return _get_manager().gettext_noop(message)


def ngettext(singular: str, plural: str, number: int) -> str:
    """
    Translate a message with plural forms.

    Usage:
        msg = ngettext(
            '%(count)d item',
            '%(count)d items',
            count
        ) % {'count': count}
    """
    return _get_manager().ngettext(singular, plural, number)


def pgettext(context: str, message: str) -> str:
    """
    Translate a message with context.
    Context helps differentiate identical strings used in different ways.

    Usage:
        # 'May' the month vs 'May' the verb
        month = pgettext('month name', 'May')
        verb = pgettext('verb', 'May')
    """
    return _get_manager().pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, number: int) -> str:
    """
    Translate a message with context and plural forms.

    Usage:
        msg = npgettext(
            'email count',
            '%(count)d email',
            '%(count)d emails',
            count
        ) % {'count': count}
    """
    return _get_manager().npgettext(context, singular, plural, number)


def activate(language: str):
    """
    Activate a language for the current execution context.

    Usage:
        activate('de')  # Switch to German
    """
    _get_manager().activate(language)


def deactivate():
    """
    Deactivate the current language, falling back to default.
    """
    _get_manager().deactivate()


def deactivate_all():
    """
    Deactivate all translations.
    """
    _get_manager().deactivate_all()


def get_language() -> str:
    """
    Get the current active language code.

    Returns:
        Language code (e.g., 'en', 'de', 'fr-ca')
    """
    return _get_manager().get_language()


def get_language_bidi() -> bool:
    """
    Check if the current language is bidirectional (RTL).

    Returns:
        True if the language is RTL (e.g., Arabic, Hebrew)
    """
    return _get_manager().get_language_bidi()


def check_for_language(lang_code: str) -> bool:
    """
    Check if a language code is valid.

    Args:
        lang_code: Language code to check (e.g., 'en', 'de', 'fr-ca')

    Returns:
        True if the language code looks valid
    """
    return _get_manager().check_for_language(lang_code)


class override(ContextDecorator):
    """
    Context manager/decorator to temporarily activate a language.

    Usage as context manager:
        with override('de'):
            text = gettext('Hello')  # Returns German translation

    Usage as decorator:
        @override('de')
        def my_view():
            return gettext('Hello')
    """

    def __init__(self, language: str | None, deactivate: bool = False):
        self.language = language
        self.deactivate = deactivate
        self.old_language = None

    def __enter__(self):
        self.old_language = get_language()
        if self.language is not None:
            activate(self.language)
        else:
            deactivate_all()

    def __exit__(self, exc_type, exc_value, traceback):
        if self.old_language is None:
            deactivate_all()
        elif self.deactivate:
            deactivate()
        else:
            activate(self.old_language)


# Lazy translation functions

gettext_lazy = lazy(gettext, str)
gettext_lazy.__doc__ = """
Lazy version of gettext. The translation is only performed when the
string is used/evaluated.

Usage:
    WELCOME_MSG = gettext_lazy('Welcome')  # Not translated yet
    # Later, when the string is used:
    print(WELCOME_MSG)  # Now it's translated
"""

pgettext_lazy = lazy(pgettext, str)
pgettext_lazy.__doc__ = """
Lazy version of pgettext. The translation is only performed when the
string is used/evaluated.
"""


def lazy_number(func, resultclass, number=None, **kwargs):
    """
    Helper for creating lazy plural translations that can be formatted later.
    """
    if isinstance(number, int):
        kwargs["number"] = number
        proxy = lazy(func, resultclass)(**kwargs)
    else:
        original_kwargs = kwargs.copy()

        class NumberAwareString(resultclass):
            def __bool__(self):
                return bool(kwargs["singular"])

            def _get_number_value(self, values):
                try:
                    return values[number]
                except KeyError as error:
                    raise KeyError(
                        f"Your dictionary lacks key '{number}'. Please provide "
                        "it, because it is required to determine whether "
                        "string is singular or plural."
                    ) from error

            def _translate(self, number_value):
                kwargs["number"] = number_value
                return func(**kwargs)

            def format(self, *args, **kwargs):
                number_value = self._get_number_value(kwargs) if kwargs and number else args[0]
                return self._translate(number_value).format(*args, **kwargs)

            def __mod__(self, rhs):
                if isinstance(rhs, dict) and number:
                    number_value = self._get_number_value(rhs)
                else:
                    number_value = rhs
                translated = self._translate(number_value)
                # The string may carry no placeholder for the number, which
                # is not an error: it simply has nothing to substitute.
                with contextlib.suppress(TypeError):
                    translated %= rhs
                return translated

        proxy = lazy(lambda **kwargs: NumberAwareString(), NumberAwareString)(**kwargs)
        proxy.__reduce__ = lambda: (
            _lazy_number_unpickle,
            (func, resultclass, number, original_kwargs),
        )
    return proxy


def _lazy_number_unpickle(func, resultclass, number, kwargs):
    return lazy_number(func, resultclass, number=number, **kwargs)


def ngettext_lazy(singular: str, plural: str, number=None):
    """
    Lazy version of ngettext for plural forms.

    Usage:
        msg = ngettext_lazy('%(count)d item', '%(count)d items', 'count')
        # Later:
        print(msg % {'count': 5})  # Translates to '5 items'
    """
    return lazy_number(ngettext, str, singular=singular, plural=plural, number=number)


def npgettext_lazy(context: str, singular: str, plural: str, number=None):
    """
    Lazy version of npgettext for plural forms with context.

    Usage:
        msg = npgettext_lazy('items', '%(count)d item', '%(count)d items', 'count')
    """
    return lazy_number(
        npgettext, str, context=context, singular=singular, plural=plural, number=number
    )


# Utility functions


def to_language(locale: str) -> str:
    """
    Turn a locale name (en_US) into a language name (en-us).

    Args:
        locale: Locale name (e.g., 'en_US', 'de_DE')

    Returns:
        Language name (e.g., 'en-us', 'de-de')
    """
    p = locale.find("_")
    if p >= 0:
        return locale[:p].lower() + "-" + locale[p + 1 :].lower()
    else:
        return locale.lower()


def to_locale(language: str) -> str:
    """
    Turn a language name (en-us) into a locale name (en_US).

    Args:
        language: Language name (e.g., 'en-us', 'de-de')

    Returns:
        Locale name (e.g., 'en_US', 'de_DE')
    """
    lang, _, country = language.lower().partition("-")
    if not country:
        return language[:3].lower() + language[3:]
    # A language with > 2 characters after the dash only has its first
    # character after the dash capitalized; e.g. sr-latn becomes sr_Latn.
    # A language with 2 characters after the dash has both characters
    # capitalized; e.g. en-us becomes en_US.
    country, _, tail = country.partition("-")
    country = country.title() if len(country) > 2 else country.upper()
    if tail:
        country += "-" + tail
    return lang + "_" + country


# Convenience alias
_ = gettext
