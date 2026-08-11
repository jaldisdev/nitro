"""Tests for the bundled Intercom.

They need a Redis at the address below and skip when there is not one.
"""

import asyncio

import pytest

from nitro.intercom import ChannelListener, connect, get_intercom, new_channel, reset_connections
from nitro.settings import ImproperlyConfigured, settings

URL = "redis://127.0.0.1:6379"
SETTLE = 0.15


class FakeSettings:
    def __init__(self, intercoms):
        self.INTERCOMS = intercoms


@pytest.fixture
def configured(request, monkeypatch):
    """Point the default alias at a prefix nothing else uses."""
    prefix = f"nitro-test:{new_channel(request.node.name)}"
    monkeypatch.setattr(
        settings,
        "INTERCOMS",
        {
            "default": {
                "BACKEND": "nitro.intercom.backends.RedisIntercom",
                "LOCATION": URL,
                "OPTIONS": {"PREFIX": prefix, "CAPACITY": 4, "EXPIRY": 30},
            }
        },
        raising=False,
    )
    reset_connections()
    yield prefix
    reset_connections()


@pytest.fixture
async def intercom(configured):
    handle = get_intercom()
    try:
        await handle.ping()
    except (ConnectionError, OSError) as error:
        pytest.skip(f"no Redis at {URL}: {error}")

    yield handle
    await handle.flush()


class TestConfiguration:
    async def test_settings_drive_the_connection(self, intercom, configured):
        await intercom.send("room", "hello")
        assert await intercom.receive("room") == "hello"

    async def test_the_prefix_from_options_is_applied(self, intercom, configured):
        # Another client without the prefix must not see this channel.
        from nitro._nitro import Intercom as Compiled

        await intercom.send("room", "mine")
        unprefixed = await Compiled.connect(URL)
        assert await unprefixed.receive("room") is None

    async def test_a_missing_alias_is_reported(self, configured):
        with pytest.raises(ImproperlyConfigured, match="nowhere"):
            await connect("nowhere")

    async def test_a_missing_location_is_reported_for_a_backend_that_needs_one(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            settings,
            "INTERCOMS",
            {
                "default": {
                    "BACKEND": "nitro.intercom.backends.RedisIntercom",
                    "LOCATION": "",
                }
            },
            raising=False,
        )
        with pytest.raises(ImproperlyConfigured, match="LOCATION"):
            await connect()

    async def test_a_backend_that_needs_no_location_connects_without_one(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "INTERCOMS", {"default": {"LOCATION": ""}}, raising=False
        )
        client = await connect()
        assert type(client).__name__ == "MemoryIntercom"

    async def test_an_unimportable_backend_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "INTERCOMS",
            {"default": {"BACKEND": "nitro.intercom.backends.NoSuchThing"}},
            raising=False,
        )
        with pytest.raises(ImproperlyConfigured, match="NoSuchThing"):
            await connect()

    async def test_connecting_is_deferred_until_first_use(self, configured):
        handle = get_intercom("default")
        assert "not connected" in repr(handle)

        await handle.ping()
        assert "connected" in repr(handle)

    async def test_resetting_forgets_the_connection(self, intercom):
        assert "not connected" not in repr(intercom)
        intercom.reset()
        assert "not connected" in repr(intercom)
        # Still usable; it simply reconnects.
        await intercom.ping()


class TestMessaging:
    async def test_a_queued_message_round_trips(self, intercom):
        await intercom.send("room", {"event": "joined"})
        assert await intercom.receive("room") == {"event": "joined"}

    async def test_a_published_message_reaches_a_subscriber(self, intercom):
        listener = await intercom.subscribe("live")
        await asyncio.sleep(SETTLE)

        assert await intercom.publish("live", "now") == 1
        assert await listener.receive() == "now"

    async def test_a_reader_waits_for_a_message(self, intercom):
        reader = await intercom.reader("room")

        async def deliver():
            await asyncio.sleep(0.1)
            await intercom.send("room", "late")

        asyncio.create_task(deliver())
        assert await reader.receive(timeout=5) == "late"

    async def test_group_messaging_reaches_members(self, intercom):
        await intercom.group_add("room", "alice")
        await intercom.group_send("room", "everyone")
        assert await intercom.receive("alice") == "everyone"

        await intercom.group_discard("room", "alice")
        assert await intercom.group_channels("room") == []


class TestChannelListener:
    async def test_groups_are_joined_and_left_around_the_listener(self, intercom):
        channel = new_channel("socket")

        async with intercom.listen(channel, groups=["room"]):
            assert await intercom.group_channels("room") == [channel]

        assert await intercom.group_channels("room") == []

    async def test_messages_published_to_a_group_arrive(self, intercom):
        channel = new_channel("socket")

        async with intercom.listen(channel, groups=["room"]) as messages:
            await asyncio.sleep(SETTLE)
            await intercom.group_publish("room", {"to": "the room"})
            assert await messages.receive() == {"to": "the room"}

    async def test_iteration_yields_messages_in_order(self, intercom):
        channel = new_channel("socket")

        async with intercom.listen(channel) as messages:
            await asyncio.sleep(SETTLE)
            for index in range(3):
                await intercom.publish(channel, index)

            received = []
            async for message in messages:
                received.append(message)
                if len(received) == 3:
                    break

        assert received == [0, 1, 2]

    async def test_groups_are_left_even_when_the_body_raises(self, intercom):
        channel = new_channel("socket")

        with pytest.raises(RuntimeError, match="deliberate"):
            async with intercom.listen(channel, groups=["room"]):
                raise RuntimeError("deliberate")

        assert await intercom.group_channels("room") == []

    async def test_using_it_outside_the_context_manager_is_refused(self, intercom):
        listener = ChannelListener(intercom, "socket")
        with pytest.raises(RuntimeError, match="async with"):
            await listener.receive()


class TestChannelNames:
    def test_a_generated_name_carries_its_prefix(self):
        assert new_channel("socket").startswith("socket.")

    def test_generated_names_do_not_repeat(self):
        assert len({new_channel("socket") for _ in range(1000)}) == 1000
