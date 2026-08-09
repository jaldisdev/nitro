"""Tests for the standalone Intercom package.

They need a Redis at the address below and skip when there is not one, so a
developer without it running is not blocked while CI still gets the coverage.
"""

import asyncio

import pytest

from nitro_intercom import Intercom

URL = "redis://127.0.0.1:6379"

# Subscribing is not instantaneous on the server; publishing sooner than this
# would be delivered to nobody.
SETTLE = 0.15


@pytest.fixture
async def intercom(request):
    """A client under a prefix nothing else uses."""
    prefix = f"nitro-test:{Intercom.new_channel(request.node.name)}"
    try:
        client = await Intercom.connect(URL, prefix=prefix, capacity=4, expiry=30)
        await client.ping()
    except (ConnectionError, OSError) as error:
        pytest.skip(f"no Redis at {URL}: {error}")

    yield client
    await client.flush()


class TestValueConversion:
    @pytest.mark.parametrize(
        "message",
        [
            None,
            True,
            False,
            0,
            -17,
            2**62,
            1.5,
            "text",
            "Grüße, 世界 🎉",
            b"\x00\x01\xff",
            [],
            [1, "two", None],
            {},
            {"event": "joined", "user": "ada"},
            {"nested": {"list": [1, {"deep": True}]}},
        ],
    )
    async def test_a_value_survives_a_round_trip(self, intercom, message):
        await intercom.send("values", message)
        assert await intercom.receive("values") == message

    async def test_a_tuple_arrives_as_a_list(self, intercom):
        await intercom.send("values", ("a", "b"))
        assert await intercom.receive("values") == ["a", "b"]

    async def test_booleans_do_not_arrive_as_numbers(self, intercom):
        await intercom.send("values", {"flag": True, "count": 1})
        received = await intercom.receive("values")

        assert received["flag"] is True
        assert received["count"] == 1
        assert not isinstance(received["count"], bool)

    async def test_an_unsupported_type_is_refused(self, intercom):
        with pytest.raises(TypeError, match="cannot be sent"):
            await intercom.send("values", object())

    async def test_an_unsupported_type_nested_inside_is_refused(self, intercom):
        with pytest.raises(TypeError, match="cannot be sent"):
            await intercom.send("values", {"inner": {1, 2, 3}})


class TestQueuedChannels:
    async def test_an_empty_channel_reads_as_none(self, intercom):
        assert await intercom.receive("quiet") is None

    async def test_messages_are_read_oldest_first(self, intercom):
        for index in range(3):
            await intercom.send("room", index)
        assert [await intercom.receive("room") for _ in range(3)] == [0, 1, 2]

    async def test_a_full_channel_keeps_the_newest(self, intercom):
        for index in range(6):
            await intercom.send("room", index)

        received = []
        while (message := await intercom.receive("room")) is not None:
            received.append(message)

        assert received == [2, 3, 4, 5]

    async def test_a_reader_waits_for_a_message(self, intercom):
        reader = await intercom.reader("room")

        async def deliver():
            await asyncio.sleep(0.1)
            await intercom.send("room", "late")

        asyncio.create_task(deliver())
        assert await reader.receive(timeout=5) == "late"

    async def test_a_reader_gives_up_at_its_timeout(self, intercom):
        reader = await intercom.reader("quiet")
        assert await reader.receive(timeout=1) is None

    async def test_a_reader_can_check_without_waiting(self, intercom):
        reader = await intercom.reader("room")
        assert await reader.try_receive() is None

        await intercom.send("room", "here")
        assert await reader.try_receive() == "here"


class TestPublishSubscribe:
    async def test_a_subscriber_receives_what_is_published(self, intercom):
        listener = await intercom.subscribe("live")
        await asyncio.sleep(SETTLE)

        assert await intercom.publish("live", {"now": True}) == 1
        assert await listener.receive() == {"now": True}

    async def test_a_subscription_can_be_iterated(self, intercom):
        listener = await intercom.subscribe("live")
        await asyncio.sleep(SETTLE)

        for index in range(3):
            await intercom.publish("live", index)

        received = []
        async for message in listener:
            received.append(message)
            if len(received) == 3:
                break

        assert received == [0, 1, 2]

    async def test_publishing_to_nobody_reaches_nobody(self, intercom):
        assert await intercom.publish("empty", "lost") == 0

    async def test_every_subscriber_is_reached(self, intercom):
        first = await intercom.subscribe("live")
        second = await intercom.subscribe("live")
        await asyncio.sleep(SETTLE)

        assert await intercom.publish("live", "both") == 2
        assert await first.receive() == "both"
        assert await second.receive() == "both"

    async def test_a_closed_listener_stops_iterating(self, intercom):
        listener = await intercom.subscribe("live")
        await listener.close()

        assert await listener.receive() is None
        with pytest.raises(StopAsyncIteration):
            await listener.__anext__()


class TestGroups:
    async def test_membership_is_recorded_without_duplicates(self, intercom):
        await intercom.group_add("room", "alice")
        await intercom.group_add("room", "bob")
        await intercom.group_add("room", "bob")

        assert sorted(await intercom.group_channels("room")) == ["alice", "bob"]

    async def test_a_member_can_be_removed(self, intercom):
        await intercom.group_add("room", "alice")
        await intercom.group_discard("room", "alice")
        assert await intercom.group_channels("room") == []

    async def test_an_unknown_group_is_empty(self, intercom):
        assert await intercom.group_channels("nowhere") == []

    async def test_a_group_send_reaches_every_member(self, intercom):
        await intercom.group_add("room", "alice")
        await intercom.group_add("room", "bob")
        await intercom.group_send("room", {"to": "everyone"})

        assert await intercom.receive("alice") == {"to": "everyone"}
        assert await intercom.receive("bob") == {"to": "everyone"}

    async def test_a_group_publish_reaches_every_listening_member(self, intercom):
        await intercom.group_add("room", "alice")
        await intercom.group_add("room", "bob")

        alice = await intercom.subscribe("alice")
        bob = await intercom.subscribe("bob")
        await asyncio.sleep(SETTLE)

        assert await intercom.group_publish("room", "live") == 2
        assert await alice.receive() == "live"
        assert await bob.receive() == "live"

    async def test_sending_to_an_empty_group_is_harmless(self, intercom):
        await intercom.group_send("nowhere", "lost")


class TestChannelNames:
    def test_a_generated_name_carries_its_prefix(self):
        assert Intercom.new_channel("socket").startswith("socket.")

    def test_generated_names_do_not_repeat(self):
        assert len({Intercom.new_channel("socket") for _ in range(1000)}) == 1000


class TestConnection:
    async def test_a_bad_address_is_reported(self):
        with pytest.raises((ConnectionError, OSError)):
            await Intercom.connect("not-a-url")

    async def test_an_unreachable_server_is_reported(self):
        with pytest.raises((ConnectionError, OSError)):
            await Intercom.connect("redis://127.0.0.1:1")

    async def test_flushing_leaves_other_prefixes_alone(self, intercom):
        try:
            other = await Intercom.connect(URL, prefix="nitro-test:other-prefix")
        except (ConnectionError, OSError) as error:
            pytest.skip(f"no Redis at {URL}: {error}")

        await intercom.send("room", "mine")
        await other.send("room", "theirs")

        await intercom.flush()
        assert await intercom.receive("room") is None
        assert await other.receive("room") == "theirs"

        await other.flush()
