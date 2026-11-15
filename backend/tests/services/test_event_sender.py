"""Tests for the event sender: who receives an event, and the coalescing.

``docs/api-design.md`` section 4: "at most one event of a type for a speaker
in 250 ms, with the last one always sent".
"""

import time
from queue import Empty

import pytest

from app.schemas.stream_event_schema import (
    PlaybackChangedEvent,
    SpeakerPayload,
    SpeakersChangedEvent,
    VolumeChangedEvent,
)
from app.services.event_sender import COALESCE_SECONDS, EventSender
from app.utils.lifecycle_coordinator import LifecycleCoordinator

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"


def volume_changed(speaker_id: str) -> VolumeChangedEvent:
    return VolumeChangedEvent(payload=SpeakerPayload(speaker_id=speaker_id))


@pytest.fixture
def sender(app):
    """The container's event sender, one of its own for the test."""
    app.container.event_sender.reset()
    sender = app.container.event_sender()
    try:
        yield sender
    finally:
        sender.stop()
        app.container.event_sender.reset()


class TestEventSender:
    def test_an_event_is_an_unnamed_message_of_type_and_payload(self, sender, connect):
        stream = connect()

        sender.send(volume_changed(GARAGE))

        assert stream.connection.next_frame(1) == (
            'data: {"type":"volume_changed","payload":{"speaker_id": "'
            + GARAGE
            + '"}}\n\n'
        )

    def test_an_event_about_the_household_has_an_empty_payload(self, sender, connect):
        stream = connect()

        sender.send(SpeakersChangedEvent())

        assert stream.events() == [{"type": "speakers_changed", "payload": {}}]

    def test_every_connection_receives_every_event(self, sender, connect):
        first, second = connect(), connect()

        sender.send(volume_changed(GARAGE))
        sender.send(PlaybackChangedEvent(payload=SpeakerPayload(speaker_id=LOUNGE)))

        expected = [
            {"type": "volume_changed", "payload": {"speaker_id": GARAGE}},
            {"type": "playback_changed", "payload": {"speaker_id": LOUNGE}},
        ]
        assert first.events() == expected
        assert second.events() == expected

    def test_the_target_is_the_local_admin(self, app, sender, connect):
        # Without OIDC the stream's route makes every connection the local
        # admin's; a connection that is not gets no domain event (SSE-2).
        manager = app.container.sse_connection_manager()
        other = manager.on_connect("test-not-local-admin", "someone")
        try:
            sender.send(volume_changed(GARAGE))

            with pytest.raises(Empty):
                other.next_frame(COALESCE_SECONDS)
        finally:
            manager.on_disconnect(other)

    def test_a_second_event_within_250_ms_waits_for_them_to_pass(self, sender, connect):
        stream = connect()
        started = time.perf_counter()

        sender.send(volume_changed(GARAGE))
        first = stream.connection.next_frame(1)
        sender.send(volume_changed(GARAGE))
        second = stream.connection.next_frame(1)
        arrived = time.perf_counter()

        assert first == second
        assert arrived - started >= COALESCE_SECONDS

    def test_a_burst_is_one_event_at_once_and_the_last_one_after(self, sender, connect):
        stream = connect()
        started = time.perf_counter()

        for _ in range(20):
            sender.send(volume_changed(GARAGE))
        windows = (time.perf_counter() - started) // COALESCE_SECONDS

        # Two, and one more for every 250 ms the burst itself took.
        events = stream.events()
        assert 2 <= len(events) <= 2 + windows
        assert events == [
            {"type": "volume_changed", "payload": {"speaker_id": GARAGE}}
        ] * len(events)

    def test_at_most_one_event_in_250_ms_while_changes_keep_coming(
        self, sender, connect
    ):
        stream = connect()
        started = time.perf_counter()

        sends = 0
        while time.perf_counter() - started < 3 * COALESCE_SECONDS:
            sender.send(volume_changed(GARAGE))
            sends += 1
            time.sleep(0.01)
        events = stream.events()
        elapsed = time.perf_counter() - started

        # One at once, then one for every 250 ms that passed.
        assert 3 <= len(events) <= elapsed // COALESCE_SECONDS + 1
        assert sends > 10 * len(events)

    def test_an_event_does_not_wait_for_one_of_another_speaker_or_type(
        self, sender, connect
    ):
        stream = connect()

        sender.send(volume_changed(GARAGE))
        sender.send(volume_changed(LOUNGE))
        sender.send(PlaybackChangedEvent(payload=SpeakerPayload(speaker_id=GARAGE)))
        sender.send(SpeakersChangedEvent())
        frames = [stream.connection.next_frame(COALESCE_SECONDS / 5) for _ in range(4)]

        assert [frame.split('"')[3] for frame in frames] == [
            "volume_changed",
            "volume_changed",
            "playback_changed",
            "speakers_changed",
        ]

    def test_an_event_after_the_250_ms_goes_out_at_once(self, sender, connect):
        stream = connect()

        sender.send(volume_changed(GARAGE))
        time.sleep(COALESCE_SECONDS)
        sender.send(volume_changed(GARAGE))

        assert stream.connection.next_frame(1) is not None
        assert stream.connection.next_frame(COALESCE_SECONDS / 5) is not None

    def test_stop_drops_what_waits_and_sends_nothing_more(self, sender, connect):
        stream = connect()
        sender.send(volume_changed(GARAGE))
        sender.send(volume_changed(GARAGE))

        sender.stop()
        sender.send(volume_changed(LOUNGE))

        assert len(stream.events()) == 1

    def test_it_stops_when_the_app_prepares_to_shut_down(self, app, connect):
        stream = connect()
        coordinator = LifecycleCoordinator(graceful_shutdown_timeout=1)
        sender = EventSender(
            sse_connection_manager=app.container.sse_connection_manager(),
            lifecycle_coordinator=coordinator,
        )

        coordinator.shutdown()
        sender.send(volume_changed(GARAGE))

        assert stream.events() == []
