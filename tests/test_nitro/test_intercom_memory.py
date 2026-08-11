"""The in-process Intercom backend.

It is the default, so it has to behave the way the Redis-backed one does: the
same delivery semantics, the same bounded queue, the same refusal of values
that could not cross a wire. These cases are deliberately written against the
behaviour rather than the implementation, so the same file could be pointed at
either backend.
"""

from __future__ import annotations

import asyncio

import pytest

from nitro.intercom import connect, get_intercom, reset_connections
from nitro.intercom.backends import MemoryIntercom, reset_store
from nitro.settings import settings


@pytest.fixture(autouse=True)
def clean_store():
    reset_store()
    yield
    reset_store()


@pytest.fixture
async def intercom():
    return await MemoryIntercom.connect(prefix="test", capacity=4, expiry=30)


class TestPushDelivery:
    async def test_a_subscriber_receives_what_is_published(self, intercom):
        listener = await intercom.subscribe("room")
        reached = await intercom.publish("room", {"event": "joined"})

        assert reached == 1
        assert await listener.receive() == {"event": "joined"}

    async def test_publishing_with_nobody_listening_reaches_nobody(self, intercom):
        assert await intercom.publish("room", "hello") == 0

    async def test_every_subscriber_receives_it(self, intercom):
        first = await intercom.subscribe("room")
        second = await intercom.subscribe("room")

        assert await intercom.publish("room", "hello") == 2
        assert await first.receive() == "hello"
        assert await second.receive() == "hello"

    async def test_a_closed_listener_receives_nothing_more(self, intercom):
        listener = await intercom.subscribe("room")
        await listener.close()

        assert await intercom.publish("room", "hello") == 0
        assert await listener.receive() is None

    async def test_it_iterates_until_closed(self, intercom):
        listener = await intercom.subscribe("room")
        await intercom.publish("room", "one")
        await intercom.publish("room", "two")

        received = []

        async def drain():
            async for message in listener:
                received.append(message)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.01)
        await listener.close()
        await asyncio.wait_for(task, timeout=1)

        assert received == ["one", "two"]

    async def test_a_prefix_keeps_two_applications_apart(self):
        mine = await MemoryIntercom.connect(prefix="mine")
        theirs = await MemoryIntercom.connect(prefix="theirs")

        listener = await theirs.subscribe("room")
        assert await mine.publish("room", "hello") == 0
        assert listener._messages.empty()


class TestQueuedDelivery:
    async def test_a_queued_message_waits_to_be_read(self, intercom):
        await intercom.send("jobs", {"task": "resize"})
        assert await intercom.receive("jobs") == {"task": "resize"}

    async def test_an_empty_channel_reads_as_none(self, intercom):
        assert await intercom.receive("jobs") is None

    async def test_the_oldest_is_read_first(self, intercom):
        await intercom.send("jobs", "one")
        await intercom.send("jobs", "two")

        assert await intercom.receive("jobs") == "one"
        assert await intercom.receive("jobs") == "two"

    async def test_the_oldest_is_discarded_once_full(self, intercom):
        for number in range(6):
            await intercom.send("jobs", number)

        # Capacity is four, so the first two are gone.
        assert [await intercom.receive("jobs") for _ in range(4)] == [2, 3, 4, 5]
        assert await intercom.receive("jobs") is None

    async def test_a_reader_takes_what_is_already_queued(self, intercom):
        await intercom.send("jobs", "waiting")
        reader = await intercom.reader("jobs")

        assert await reader.try_receive() == "waiting"
        assert await reader.try_receive() is None

    async def test_a_reader_waits_for_a_message(self, intercom):
        reader = await intercom.reader("jobs")

        async def send_shortly():
            await asyncio.sleep(0.02)
            await intercom.send("jobs", "late")

        asyncio.create_task(send_shortly())
        assert await reader.receive(timeout=2) == "late"

    async def test_a_reader_gives_up_at_its_timeout(self, intercom):
        reader = await intercom.reader("jobs")
        assert await reader.receive(timeout=0.05) is None

    async def test_an_expired_channel_is_forgotten(self):
        intercom = await MemoryIntercom.connect(expiry=0)
        await intercom.send("jobs", "stale")

        await asyncio.sleep(1.01)
        assert await intercom.receive("jobs") is None


class TestGroups:
    async def test_a_group_reports_its_channels(self, intercom):
        await intercom.group_add("room", "one")
        await intercom.group_add("room", "two")

        assert await intercom.group_channels("room") == ["one", "two"]
        assert await intercom.group_size("room") == 2

    async def test_adding_twice_changes_nothing(self, intercom):
        await intercom.group_add("room", "one")
        await intercom.group_add("room", "one")
        assert await intercom.group_channels("room") == ["one"]

    async def test_discarding_reports_whether_it_was_there(self, intercom):
        await intercom.group_add("room", "one")

        assert await intercom.group_discard("room", "one") is True
        assert await intercom.group_discard("room", "one") is False

    async def test_an_unknown_group_has_no_channels(self, intercom):
        assert await intercom.group_channels("nowhere") == []

    async def test_group_publish_reaches_every_listening_member(self, intercom):
        first = await intercom.subscribe("one")
        second = await intercom.subscribe("two")
        await intercom.group_add("room", "one")
        await intercom.group_add("room", "two")

        assert await intercom.group_publish("room", "hello") == 2
        assert await first.receive() == "hello"
        assert await second.receive() == "hello"

    async def test_group_send_queues_for_every_member(self, intercom):
        await intercom.group_add("room", "one")
        await intercom.group_add("room", "two")

        await intercom.group_send("room", "hello")

        assert await intercom.receive("one") == "hello"
        assert await intercom.receive("two") == "hello"

    async def test_a_channel_and_a_group_of_one_name_do_not_collide(self, intercom):
        await intercom.send("chat", "queued")
        await intercom.group_add("chat", "somewhere")

        assert await intercom.receive("chat") == "queued"
        assert await intercom.group_channels("chat") == ["somewhere"]


class TestMessages:
    @pytest.mark.parametrize(
        "value",
        [None, True, False, 0, 42, -1, 3.5, "text", b"bytes", [], {}, [1, "two"], {"a": [1]}],
    )
    async def test_a_value_survives_the_round_trip(self, intercom, value):
        await intercom.send("channel", value)
        assert await intercom.receive("channel") == value

    async def test_a_tuple_arrives_as_a_list(self, intercom):
        await intercom.send("channel", (1, 2))
        assert await intercom.receive("channel") == [1, 2]

    async def test_something_that_could_not_cross_a_wire_is_refused(self, intercom):
        class Arbitrary:
            pass

        with pytest.raises(TypeError, match="cannot be sent through a channel"):
            await intercom.send("channel", Arbitrary())

    async def test_a_sender_mutating_what_it_sent_does_not_change_it(self, intercom):
        payload = {"items": [1]}
        await intercom.send("channel", payload)
        payload["items"].append(2)

        assert await intercom.receive("channel") == {"items": [1]}


class TestHousekeeping:
    async def test_flush_removes_only_this_prefix(self):
        mine = await MemoryIntercom.connect(prefix="mine")
        theirs = await MemoryIntercom.connect(prefix="theirs")

        await mine.send("jobs", "a")
        await theirs.send("jobs", "b")

        assert await mine.flush() == 1
        assert await mine.receive("jobs") is None
        assert await theirs.receive("jobs") == "b"

    async def test_ping_always_answers(self, intercom):
        assert await intercom.ping() is None

    async def test_a_new_channel_name_is_unique(self):
        names = {MemoryIntercom.new_channel("socket") for _ in range(100)}
        assert len(names) == 100

    async def test_resetting_forgets_everything(self, intercom):
        await intercom.send("jobs", "a")
        reset_store()
        assert await intercom.receive("jobs") is None


class TestItIsTheDefault:
    async def test_the_shipped_settings_reach_the_memory_backend(self, monkeypatch):
        monkeypatch.setattr(settings, "INTERCOMS", None, raising=False)
        settings.INTERCOMS = {
            "default": {
                "BACKEND": "nitro.intercom.backends.MemoryIntercom",
                "LOCATION": "",
                "OPTIONS": {},
            }
        }
        reset_connections()

        client = await connect()
        assert isinstance(client, MemoryIntercom)

    async def test_the_project_handle_works_without_anything_running(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "INTERCOMS",
            {"default": {"BACKEND": "nitro.intercom.backends.MemoryIntercom"}},
            raising=False,
        )
        reset_connections()

        handle = get_intercom()
        async with handle.listen("socket", groups=["room"]) as messages:
            assert await handle.group_publish("room", {"event": "joined"}) == 1
            assert await messages.receive() == {"event": "joined"}

        reset_connections()
