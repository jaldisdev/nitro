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

"""The helpers in `nitro.utils`.

These are for applications built on Nitro rather than for the framework, which
is exactly why none of them had a test: nothing in `nitro/` imports most of
them, so nothing exercised them either. Two were broken outright —
`to_camel_case` and `to_snake_case` called an `re` they never imported, and
`get_current_timezone` read an `_active` that did not exist — and every call
raised `NameError`.
"""

from __future__ import annotations

import datetime as datetime_module
import pickle
import zoneinfo

import pytest

from nitro.protocols.http import FileResponse, HttpResponse
from nitro.settings import settings
from nitro.utils import datetime as datetime_utils
from nitro.utils.crypto import (
    RANDOM_STRING_CHARS,
    InvalidAlgorithm,
    get_random_string,
    salted_hmac,
)
from nitro.utils.encoding import ensure_bytes, ensure_str, is_protected_type
from nitro.utils.http import content_disposition_header, patch_vary_headers
from nitro.utils.lazy import SimpleLazyObject, empty, lazystr
from nitro.utils.modules import import_string
from nitro.utils.text import capitalize_first, lower_first, to_camel_case, to_snake_case
from nitro.utils.tokens import base36_to_int, int_to_base36
from nitro.utils.version import get_complete_version, get_version


class TestText:
    """`to_camel_case` and `to_snake_case` raised NameError on every call."""

    def test_camel_case_joins_underscored_words(self):
        assert to_camel_case("user_id") == "userId"
        assert to_camel_case("a_b_c") == "aBC"

    def test_camel_case_can_capitalise_the_first_word(self):
        assert to_camel_case("user_id", capitalize=True) == "UserId"

    def test_camel_case_leaves_a_single_word_alone(self):
        assert to_camel_case("user") == "user"

    def test_camel_case_of_nothing_is_nothing(self):
        assert to_camel_case("") == ""

    def test_snake_case_separates_before_each_capital(self):
        assert to_snake_case("userId") == "user_id"
        assert to_snake_case("HTTPResponse") == "h_t_t_p_response"

    def test_snake_case_leaves_a_leading_capital_alone(self):
        assert to_snake_case("User") == "user"

    def test_the_two_round_trip(self):
        assert to_snake_case(to_camel_case("user_id")) == "user_id"

    def test_capitalising_the_first_letter_leaves_the_rest(self):
        assert capitalize_first("hello world") == "Hello world"
        assert capitalize_first("hELLO") == "HELLO", "unlike str.capitalize"
        assert capitalize_first("") == ""

    def test_lowering_the_first_letter_leaves_the_rest(self):
        assert lower_first("Hello World") == "hello World"
        assert lower_first("") == ""


class TestDatetime:
    """`get_current_timezone` read an `_active` that was never defined."""

    def test_the_default_comes_from_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Zurich", raising=False)
        assert datetime_utils.get_default_timezone().key == "Europe/Zurich"

    def test_the_current_zone_is_the_default_until_one_is_activated(self, monkeypatch):
        monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Zurich", raising=False)
        assert datetime_utils.get_current_timezone().key == "Europe/Zurich"

    def test_activating_changes_the_current_zone(self):
        try:
            datetime_utils.activate("America/New_York")
            assert datetime_utils.get_current_timezone().key == "America/New_York"
            # A ZoneInfo has no abbreviation without a moment to read it at,
            # so the zone's own name is what it reports.
            assert datetime_utils.get_current_timezone_name() == "America/New_York"
        finally:
            datetime_utils.deactivate()

    def test_override_restores_what_was_in_force(self, monkeypatch):
        monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Zurich", raising=False)

        with datetime_utils.override("Asia/Tokyo"):
            assert datetime_utils.get_current_timezone().key == "Asia/Tokyo"

        assert datetime_utils.get_current_timezone().key == "Europe/Zurich"

    def test_override_restores_even_when_the_body_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "TIME_ZONE", "Europe/Zurich", raising=False)

        with pytest.raises(RuntimeError), datetime_utils.override("Asia/Tokyo"):
            raise RuntimeError("deliberate")

        assert datetime_utils.get_current_timezone().key == "Europe/Zurich"

    def test_now_is_aware_when_the_setting_says_so(self, monkeypatch):
        monkeypatch.setattr(settings, "USE_TZ", True, raising=False)
        assert datetime_utils.is_aware(datetime_utils.now())

    def test_now_is_naive_when_the_setting_says_so(self, monkeypatch):
        monkeypatch.setattr(settings, "USE_TZ", False, raising=False)
        assert datetime_utils.is_naive(datetime_utils.now())

    def test_aware_and_naive_are_opposites(self):
        naive = datetime_module.datetime(2026, 1, 1, 12, 0)
        aware = naive.replace(tzinfo=datetime_module.UTC)

        assert datetime_utils.is_naive(naive) and not datetime_utils.is_aware(naive)
        assert datetime_utils.is_aware(aware) and not datetime_utils.is_naive(aware)

    def test_making_a_naive_datetime_aware(self):
        naive = datetime_module.datetime(2026, 1, 1, 12, 0)
        zone = zoneinfo.ZoneInfo("Europe/Zurich")

        assert datetime_utils.make_aware(naive, zone).tzinfo == zone

    def test_making_an_aware_datetime_aware_is_refused(self):
        aware = datetime_module.datetime(2026, 1, 1, 12, 0, tzinfo=datetime_module.UTC)
        with pytest.raises(ValueError, match="naive datetime"):
            datetime_utils.make_aware(aware)

    def test_making_an_aware_datetime_naive(self):
        aware = datetime_module.datetime(2026, 1, 1, 12, 0, tzinfo=datetime_module.UTC)
        naive = datetime_utils.make_naive(aware, zoneinfo.ZoneInfo("Europe/Zurich"))

        assert naive.tzinfo is None
        assert naive.hour == 13, "UTC+1 in January"

    def test_making_a_naive_datetime_naive_is_refused(self):
        with pytest.raises(ValueError, match="aware datetime"):
            datetime_utils.make_naive(datetime_module.datetime(2026, 1, 1))

    def test_localtime_moves_an_aware_datetime(self):
        aware = datetime_module.datetime(2026, 1, 1, 12, 0, tzinfo=datetime_module.UTC)
        moved = datetime_utils.localtime(aware, zoneinfo.ZoneInfo("Europe/Zurich"))

        assert moved.hour == 13

    def test_localtime_refuses_a_naive_datetime(self):
        with pytest.raises(ValueError, match="aware datetime"):
            datetime_utils.localtime(datetime_module.datetime(2026, 1, 1))


class TestCrypto:
    def test_a_random_string_has_the_length_asked_for(self):
        assert len(get_random_string(32)) == 32

    def test_a_random_string_uses_only_the_allowed_characters(self):
        assert set(get_random_string(200)) <= set(RANDOM_STRING_CHARS)
        assert set(get_random_string(50, "ab")) <= {"a", "b"}

    def test_two_random_strings_differ(self):
        assert get_random_string(32) != get_random_string(32)

    def test_a_salted_hmac_is_stable_for_the_same_input(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", "a-secret", raising=False)
        first = salted_hmac("salt", "value").hexdigest()
        second = salted_hmac("salt", "value").hexdigest()
        assert first == second

    def test_the_salt_changes_the_result(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", "a-secret", raising=False)
        assert salted_hmac("one", "value").hexdigest() != salted_hmac("two", "value").hexdigest()

    def test_the_secret_changes_the_result(self):
        assert (
            salted_hmac("salt", "value", secret="first").hexdigest()
            != salted_hmac("salt", "value", secret="second").hexdigest()
        )

    def test_an_unknown_algorithm_is_reported(self):
        with pytest.raises(InvalidAlgorithm):
            salted_hmac("salt", "value", secret="s", algorithm="not-an-algorithm")


class TestTokens:
    def test_base36_round_trips(self):
        for number in [0, 1, 35, 36, 1234, 999999]:
            assert base36_to_int(int_to_base36(number)) == number

    def test_base36_is_lower_case(self):
        assert int_to_base36(35) == "z"
        assert int_to_base36(36) == "10"

    def test_a_negative_number_is_refused(self):
        with pytest.raises(ValueError):
            int_to_base36(-1)

    def test_something_that_is_not_base36_is_refused(self):
        with pytest.raises(ValueError):
            base36_to_int("not base 36!")


class TestEncoding:
    def test_bytes_become_text(self):
        assert ensure_str(b"hello") == "hello"

    def test_text_stays_text(self):
        assert ensure_str("hello") == "hello"

    def test_text_becomes_bytes(self):
        assert ensure_bytes("hello") == b"hello"

    def test_bytes_stay_bytes(self):
        assert ensure_bytes(b"hello") == b"hello"

    def test_a_number_becomes_its_text(self):
        assert ensure_str(42) == "42"

    def test_protected_types_are_left_alone_when_asked(self):
        assert ensure_str(42, strings_only=True) == 42
        assert ensure_str(None, strings_only=True) is None

    def test_what_counts_as_protected(self):
        assert is_protected_type(None)
        assert is_protected_type(42)
        assert not is_protected_type("text")

    def test_undecodable_bytes_are_reported_with_their_object(self):
        from nitro.utils.encoding import NitroUnicodeDecodeError

        with pytest.raises(NitroUnicodeDecodeError):
            ensure_str(b"\xff\xfe", encoding="utf-8")


class TestModules:
    def test_it_imports_a_dotted_path(self):
        assert import_string("nitro.settings.ImproperlyConfigured") is not None

    def test_a_path_without_a_dot_is_reported(self):
        with pytest.raises(ImportError, match="look like a module path"):
            import_string("nodots")

    def test_a_missing_attribute_is_reported(self):
        with pytest.raises(ImportError, match="NoSuchThing"):
            import_string("nitro.settings.NoSuchThing")


class TestLazy:
    """The general-purpose deferred objects, distinct from `LazySettings`."""

    def test_it_is_not_built_until_it_is_used(self):
        built = []

        def make():
            built.append(True)
            return "value"

        wrapper = SimpleLazyObject(make)
        assert built == [], "constructing must not build it"

        assert str(wrapper) == "value"
        assert built == [True]

    def test_it_is_built_only_once(self):
        built = []
        wrapper = SimpleLazyObject(lambda: built.append(True) or "value")

        str(wrapper)
        str(wrapper)
        assert len(built) == 1

    def test_it_forwards_attributes(self):
        class Thing:
            name = "thing"

            def greet(self):
                return "hello"

        wrapper = SimpleLazyObject(Thing)
        assert wrapper.name == "thing"
        assert wrapper.greet() == "hello"

    def test_an_unbuilt_wrapper_reports_itself_as_such(self):
        wrapper = SimpleLazyObject(lambda: "value")
        assert wrapper._wrapped is empty

    def test_it_pickles_as_what_it_wraps(self):
        wrapper = SimpleLazyObject(lambda: {"a": 1})
        assert pickle.loads(pickle.dumps(wrapper)) == {"a": 1}

    def test_a_lazy_string_behaves_like_one(self):
        text = lazystr("hello")
        assert str(text) == "hello"
        assert f"{text} world" == "hello world"

    def test_it_compares_as_what_it_wraps(self):
        assert SimpleLazyObject(lambda: 42) == 42


class TestVersion:
    def test_a_final_release_reads_as_its_numbers(self):
        assert get_version((1, 2, 3, "final", 0)) == "1.2.3"

    def test_a_trailing_zero_patch_is_dropped(self):
        assert get_version((1, 2, 0, "final", 0)) == "1.2"

    def test_a_pre_release_carries_its_marker(self):
        assert get_version((1, 2, 0, "alpha", 1)).startswith("1.2a1")
        assert get_version((1, 2, 0, "beta", 2)).startswith("1.2b2")
        assert get_version((1, 2, 0, "rc", 1)).startswith("1.2rc1")

    def test_a_complete_version_is_five_parts(self):
        assert len(get_complete_version((1, 2, 3, "final", 0))) == 5

    def test_an_invalid_release_stage_is_refused(self):
        with pytest.raises(AssertionError):
            get_complete_version((1, 2, 3, "nonsense", 0))


class TestTranslation:
    def test_a_string_passes_through_without_a_catalogue(self):
        from nitro.utils.translation import gettext

        assert gettext("Hello") == "Hello"

    def test_marking_for_translation_changes_nothing_now(self):
        from nitro.utils.translation import gettext_noop

        assert gettext_noop("Hello") == "Hello"

    def test_plurals_choose_by_number(self):
        from nitro.utils.translation import ngettext

        assert ngettext("%d item", "%d items", 1) == "%d item"
        assert ngettext("%d item", "%d items", 2) == "%d items"

    def test_a_lazy_string_resolves_when_it_is_used(self):
        from nitro.utils.translation import gettext_lazy

        assert str(gettext_lazy("Hello")) == "Hello"

    def test_the_active_language_can_be_set_and_read(self):
        from nitro.utils.translation import activate, deactivate_all, get_language

        try:
            activate("de")
            assert get_language() == "de"
        finally:
            deactivate_all()

    def test_override_restores_the_previous_language(self):
        from nitro.utils.translation import activate, deactivate_all, get_language, override

        try:
            activate("en")
            with override("fr"):
                assert get_language() == "fr"
            assert get_language() == "en"
        finally:
            deactivate_all()

    def test_a_language_code_becomes_a_locale_name(self):
        from nitro.utils.translation import to_locale

        assert to_locale("en-us") == "en_US"

    def test_a_locale_name_becomes_a_language_code(self):
        from nitro.utils.translation import to_language

        assert to_language("en_US") == "en-us"


class TestHttp:
    def test_content_disposition_is_omitted_for_a_nameless_inline_response(self):
        assert content_disposition_header(False, None) is None

    def test_content_disposition_needs_no_name_to_be_an_attachment(self):
        assert content_disposition_header(True, None) == "attachment"
        assert content_disposition_header(True, "") == "attachment"

    def test_content_disposition_quotes_an_ascii_name(self):
        assert content_disposition_header(True, "report.pdf") == 'attachment; filename="report.pdf"'
        assert content_disposition_header(False, "report.pdf") == 'inline; filename="report.pdf"'

    def test_content_disposition_escapes_a_name_that_would_close_the_quoting(self):
        assert content_disposition_header(True, 'a"b.pdf') == 'attachment; filename="a\\"b.pdf"'
        assert content_disposition_header(True, "a\\b.pdf") == 'attachment; filename="a\\\\b.pdf"'

    def test_content_disposition_encodes_a_name_outside_ascii(self):
        assert content_disposition_header(True, "verslag-Ω.pdf") == (
            "attachment; filename*=utf-8''verslag-%CE%A9.pdf"
        )

    def test_vary_is_created_when_the_response_has_none(self):
        response = HttpResponse()
        patch_vary_headers(response, ("Authorization",))
        assert response.headers["vary"] == "Authorization"

    def test_vary_keeps_what_is_already_there(self):
        response = HttpResponse(headers={"Vary": "Accept-Encoding"})
        patch_vary_headers(response, ("Authorization", "Cookie"))
        assert response.headers["Vary"] == "Accept-Encoding, Authorization, Cookie"

    def test_vary_does_not_repeat_a_name_whatever_its_case(self):
        response = HttpResponse(headers={"vary": "accept-encoding"})
        patch_vary_headers(response, ("Accept-Encoding",))
        assert response.headers["vary"] == "accept-encoding"

    def test_vary_does_not_add_a_second_key_in_another_case(self):
        response = HttpResponse(headers={"Vary": "Cookie"})
        patch_vary_headers(response, ("Authorization",))
        assert [key for key in response.headers if key.lower() == "vary"] == ["Vary"]

    def test_vary_collapses_to_a_star(self):
        response = HttpResponse(headers={"Vary": "Cookie"})
        patch_vary_headers(response, ("*",))
        assert response.headers["Vary"] == "*"

    def test_vary_tolerates_loose_spacing(self):
        response = HttpResponse(headers={"Vary": "Cookie ,  Accept-Encoding"})
        patch_vary_headers(response, ("Cookie", "Authorization"))
        assert response.headers["Vary"] == "Cookie, Accept-Encoding, Authorization"


class TestFileResponseDisposition:
    def test_a_download_name_outside_ascii_is_encoded(self, tmp_path):
        path = tmp_path / "source.pdf"
        path.write_bytes(b"%PDF")
        response = FileResponse(path, filename="verslag-Ω.pdf", as_attachment=True)
        assert response.headers["content-disposition"] == (
            "attachment; filename*=utf-8''verslag-%CE%A9.pdf"
        )

    def test_no_disposition_without_a_reason_for_one(self, tmp_path):
        path = tmp_path / "source.pdf"
        path.write_bytes(b"%PDF")
        assert "content-disposition" not in FileResponse(path).headers
