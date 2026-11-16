"""Tests for the speakers and volume endpoints, on the in-memory household."""

import threading
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from app.services.household_mirror import HouseholdMirror
from app.services.sonos_client import Group, SonosService, Volume
from tests.api.documents import json_schema, undescribed
from tests.fakes.sonos_client import FakeSonosClient, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
LOUNGE_SURROUNDS = ("RINCON_FAKE0000000211400", "RINCON_FAKE0000000221400")
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

GARAGE_ALONE = {
    "id": GARAGE,
    "name": "Garage",
    "model": "Play:3",
    "volume": 20,
    "muted": False,
    "group": {
        "coordinator_id": GARAGE,
        "member_ids": [GARAGE],
        "volume": 20,
        "muted": False,
    },
}
LOUNGE_ALONE = {
    "id": LOUNGE,
    "name": "Lounge",
    "model": "Playbase",
    "volume": 40,
    "muted": False,
    "group": {
        "coordinator_id": LOUNGE,
        "member_ids": [LOUNGE],
        "volume": 40,
        "muted": False,
    },
}

KITCHEN_ALONE = {
    "id": KITCHEN,
    "name": "Kitchen",
    "model": "One",
    "volume": 60,
    "muted": False,
    "group": {
        "coordinator_id": KITCHEN,
        "member_ids": [KITCHEN],
        "volume": 60,
        "muted": False,
    },
}

# What is no speaker of a route: an id no device has, a device hidden in a
# room, and a room's name.
NO_SPEAKERS = [NOWHERE, LOUNGE_SURROUNDS[0], "Garage"]

# What the join refuses as its body.
REFUSED_JOINS = [
    pytest.param({}, id="no speaker"),
    pytest.param({"speaker_id": None}, id="a speaker that is null"),
    pytest.param({"speaker_id": 101400}, id="a speaker as a number"),
    pytest.param({"speaker_id": [GARAGE]}, id="a speaker as a list"),
    pytest.param({"speaker_id": {"id": GARAGE}}, id="a speaker as an object"),
    pytest.param({"coordinator_id": GARAGE}, id="another field instead"),
    pytest.param({"speaker_id": GARAGE, "volume": 10}, id="a field more"),
    pytest.param([GARAGE], id="a list"),
    pytest.param(GARAGE, id="the id alone"),
]

# What a volume endpoint refuses, with why.
REFUSED_BODIES = [
    pytest.param({}, id="neither volume nor delta"),
    pytest.param({"muted": True}, id="the mute alone"),
    pytest.param({"volume": 10, "delta": 1}, id="both volume and delta"),
    pytest.param({"volume": None, "delta": None}, id="both null"),
    pytest.param({"volume": 101}, id="a volume over 100"),
    pytest.param({"volume": -1}, id="a volume under 0"),
    pytest.param({"delta": 101}, id="a delta over 100"),
    pytest.param({"delta": -101}, id="a delta under -100"),
    pytest.param({"volume": "10"}, id="a volume as text"),
    pytest.param({"volume": 10.5}, id="a volume with a fraction"),
    pytest.param({"volume": True}, id="a volume that is a boolean"),
    pytest.param({"delta": 0, "muted": "yes"}, id="a mute as text"),
    pytest.param({"delta": 0, "muted": 1}, id="a mute as a number"),
    pytest.param({"volume": 10, "mute": True}, id="a field that is not one"),
    pytest.param([10], id="a list"),
    pytest.param(10, id="a number"),
]


@pytest.fixture
def household(sonos: FakeSonosClient, mirror: HouseholdMirror) -> FakeSonosClient:
    """The Lounge, a room of three devices, and the Garage, mirrored."""
    sonos.add_room(
        LOUNGE, "Lounge", model="Playbase", volume=40, hidden_device_ids=LOUNGE_SURROUNDS
    )
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2)])
    mirror.start()
    sonos.settle()
    return sonos


@pytest.fixture
def three_rooms(household: FakeSonosClient) -> FakeSonosClient:
    """The household with a Kitchen that has a track of its own queued."""
    household.add_room(KITCHEN, "Kitchen", model="One", volume=60)
    household.load_queue(KITCHEN, [queue_track(9)])
    household.settle()
    return household


def calls_from_here(household: FakeSonosClient) -> list[str]:
    """The room of each call the test's thread makes to a speaker from here
    on, as they come. The mirror's reads of a room whose group changed are
    made on its own threads and are not among them."""
    thread = threading.current_thread()
    reached: list[str] = []

    def before_call(speaker_id: str) -> None:
        if threading.current_thread() is thread:
            reached.append(speaker_id)

    household.before_call = before_call
    return reached


@contextmanager
def nothing_reported(household: FakeSonosClient) -> Generator[None]:
    """While this lasts, no event of the household is delivered: its speakers
    have taken what they were sent and have reported nothing of it yet."""
    held, released = threading.Event(), threading.Event()

    def hold(event: object) -> None:
        held.set()
        assert released.wait(5.0), "the household's events were not released"

    # A subscription starts with an event, and the fake delivers its events
    # one after another.
    subscription = household.subscribe(
        GARAGE, SonosService.ZONE_GROUP_TOPOLOGY, hold, lambda: None
    )
    assert held.wait(5.0), "the household's events were not held"
    try:
        yield
    finally:
        released.set()
        subscription.unsubscribe()


def join(client, speaker_id: str, member_id: str):
    return client.put(
        f"/api/speakers/{speaker_id}/group", json={"speaker_id": member_id}
    )


def leave(client, speaker_id: str):
    return client.delete(f"/api/speakers/{speaker_id}/group")


def groups(client) -> dict[str, list[str]]:
    """The groups `GET /api/speakers` serves: each coordinator's speakers."""
    listed = client.get("/api/speakers").get_json()["data"]
    served = {
        speaker["group"]["coordinator_id"]: speaker["group"]["member_ids"]
        for speaker in listed
    }
    # Every speaker carries the group it is listed in.
    for speaker in listed:
        assert speaker["id"] in served[speaker["group"]["coordinator_id"]]
    return served


def changed(events: list[dict]) -> list[tuple[str, str | None]]:
    """What a connection was told changed: each event once, with its
    speaker."""
    return sorted(
        {(event["type"], event["payload"].get("speaker_id")) for event in events},
        key=lambda event: (event[0], event[1] or ""),
    )


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


def speaker_not_found(speaker_id: str) -> dict:
    return {
        "error": f"Speaker {speaker_id} was not found",
        "details": {"message": "The requested resource could not be found"},
        "code": "SPEAKER_NOT_FOUND",
    }


def speaker_failed(operation: str) -> dict:
    return {
        "error": (
            f"Cannot {operation} because external service failed: the speaker "
            "did not answer (ConnectTimeout)"
        ),
        "details": {"message": "External service request failed"},
        "code": "EXTERNAL_SERVICE_ERROR",
    }


class TestListSpeakers:
    def test_every_room_by_name(self, client, household):
        response = client.get("/api/speakers")

        assert response.status_code == 200
        assert response.get_json() == {"data": [GARAGE_ALONE, LOUNGE_ALONE]}

    def test_a_hidden_device_is_not_listed(self, client, household):
        listed = client.get("/api/speakers").get_json()["data"]

        assert [speaker["id"] for speaker in listed] == [GARAGE, LOUNGE]

    def test_the_rooms_of_a_group_carry_the_group(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        group = {
            "coordinator_id": GARAGE,
            "member_ids": [GARAGE, LOUNGE],
            "volume": 30,
            "muted": False,
        }
        assert client.get("/api/speakers").get_json() == {
            "data": [GARAGE_ALONE | {"group": group}, LOUNGE_ALONE | {"group": group}]
        }

    def test_the_list_is_empty_while_no_speaker_answers(self, client, sonos, mirror):
        sonos.add_room(GARAGE, "Garage")
        sonos.reachable = False
        mirror.start()

        response = client.get("/api/speakers")

        assert response.status_code == 200
        assert response.get_json() == {"data": []}

    def test_a_name_is_nfc(self, client, household):
        household.rename_room(GARAGE, "Café")
        household.settle()

        listed = client.get("/api/speakers").get_json()["data"]

        assert [speaker["name"] for speaker in listed] == ["Café", "Lounge"]


class TestGetSpeaker:
    def test_one_room(self, client, household):
        response = client.get(f"/api/speakers/{LOUNGE}")

        assert response.status_code == 200
        assert response.get_json() == LOUNGE_ALONE

    def test_the_id_stays_when_the_room_is_renamed(self, client, household):
        household.rename_room(GARAGE, "Workshop")
        household.settle()

        assert client.get(f"/api/speakers/{GARAGE}").get_json() == GARAGE_ALONE | {
            "name": "Workshop"
        }

    def test_a_change_made_elsewhere_is_served(self, client, household):
        household.set_volume(GARAGE, 33)
        household.set_muted(GARAGE, True)
        household.settle()

        speaker = client.get(f"/api/speakers/{GARAGE}").get_json()

        assert (speaker["volume"], speaker["muted"]) == (33, True)
        assert (speaker["group"]["volume"], speaker["group"]["muted"]) == (33, True)

    @pytest.mark.parametrize("speaker_id", NO_SPEAKERS)
    def test_an_unknown_speaker_is_not_found(self, client, household, speaker_id):
        response = client.get(f"/api/speakers/{speaker_id}")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(
            speaker_id
        )

    def test_every_speaker_is_not_found_while_no_speaker_answers(
        self, client, sonos, mirror
    ):
        sonos.add_room(GARAGE, "Garage")
        sonos.reachable = False
        mirror.start()

        response = client.get(f"/api/speakers/{GARAGE}")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(GARAGE)


class TestJoinGroup:
    def test_the_room_joins_the_group(self, client, household):
        response = join(client, LOUNGE, GARAGE)
        household.settle()

        assert response.status_code == 204
        assert response.data == b""
        # The Lounge's three devices are one speaker of the group.
        group = {
            "coordinator_id": GARAGE,
            "member_ids": [GARAGE, LOUNGE],
            "volume": 30,
            "muted": False,
        }
        assert client.get("/api/speakers").get_json() == {
            "data": [GARAGE_ALONE | {"group": group}, LOUNGE_ALONE | {"group": group}]
        }

    def test_the_room_plays_what_the_group_plays(self, client, household):
        join(client, LOUNGE, GARAGE)
        household.settle()

        playback = client.get(f"/api/speakers/{LOUNGE}/playback").get_json()
        assert playback["track"]["title"] == "Track 1"
        assert playback == client.get(f"/api/speakers/{GARAGE}/playback").get_json()

    def test_the_command_goes_to_the_room_that_moves(self, client, household):
        sent = calls_from_here(household)

        join(client, LOUNGE, GARAGE)

        assert sent == [LOUNGE]

    def test_the_group_is_named_by_any_room_of_it(self, client, three_rooms):
        join(client, LOUNGE, GARAGE)
        three_rooms.settle()

        response = join(client, KITCHEN, LOUNGE)
        three_rooms.settle()

        assert response.status_code == 204
        assert groups(client) == {GARAGE: [GARAGE, LOUNGE, KITCHEN]}

    def test_a_room_moves_from_one_group_to_another(self, client, three_rooms):
        join(client, LOUNGE, GARAGE)
        three_rooms.settle()

        join(client, LOUNGE, KITCHEN)
        three_rooms.settle()

        assert groups(client) == {GARAGE: [GARAGE], KITCHEN: [LOUNGE, KITCHEN]}

    def test_a_coordinator_joins_another_group(self, client, three_rooms, connect):
        three_rooms.load_queue(LOUNGE, [queue_track(7)])
        join(client, LOUNGE, GARAGE)
        three_rooms.settle()
        stream = connect()
        stream.events()

        response = join(client, GARAGE, KITCHEN)
        three_rooms.settle()

        assert response.status_code == 204
        # The Lounge, which the Garage led, is in a group of the list still.
        assert groups(client) == {LOUNGE: [LOUNGE], KITCHEN: [GARAGE, KITCHEN]}
        titles = {
            speaker_id: client.get(f"/api/speakers/{speaker_id}/playback").get_json()[
                "track"
            ]["title"]
            for speaker_id in (GARAGE, KITCHEN, LOUNGE)
        }
        assert titles == {GARAGE: "Track 9", KITCHEN: "Track 9", LOUNGE: "Track 7"}
        # Every room's group changed, and the Kitchen plays what it played.
        assert changed(stream.events()) == [
            ("playback_changed", GARAGE),
            ("playback_changed", LOUNGE),
            ("speakers_changed", None),
            ("volume_changed", GARAGE),
            ("volume_changed", LOUNGE),
            ("volume_changed", KITCHEN),
        ]

    def test_the_list_shows_the_group_once_the_speakers_report_it(
        self, client, household
    ):
        with nothing_reported(household):
            response = join(client, LOUNGE, GARAGE)
            sent = household.read_household().groups
            served = groups(client)
        household.settle()

        assert response.status_code == 204
        assert sent == (Group(GARAGE, (GARAGE, LOUNGE)),)
        assert served == {GARAGE: [GARAGE], LOUNGE: [LOUNGE]}
        assert groups(client) == {GARAGE: [GARAGE, LOUNGE]}

    def test_the_speakers_events_reach_every_connection(
        self, client, household, connect
    ):
        streams = [connect(), connect()]
        for stream in streams:
            stream.events()

        join(client, LOUNGE, GARAGE)
        household.settle()

        # Both rooms' group changed, and the Lounge plays what the Garage
        # plays.
        for stream in streams:
            assert changed(stream.events()) == [
                ("playback_changed", LOUNGE),
                ("speakers_changed", None),
                ("volume_changed", GARAGE),
                ("volume_changed", LOUNGE),
            ]

    def test_the_group_volume_moves_every_room_of_the_group(self, client, household):
        join(client, LOUNGE, GARAGE)
        household.settle()

        from_the_lounge = client.post(
            f"/api/speakers/{LOUNGE}/group/volume", json={"volume": 60}
        )
        from_the_garage = client.post(
            f"/api/speakers/{GARAGE}/group/volume", json={"delta": -30}
        )
        household.settle()

        assert from_the_lounge.get_json() == {"volume": 60, "muted": False}
        assert from_the_garage.get_json() == {"volume": 30, "muted": False}
        garage = client.get(f"/api/speakers/{GARAGE}").get_json()
        lounge = client.get(f"/api/speakers/{LOUNGE}").get_json()
        assert (garage["volume"], lounge["volume"]) == (20, 40)
        assert garage["group"]["volume"] == lounge["group"]["volume"] == 30

    def test_a_rooms_own_volume_moves_that_room_alone(self, client, household):
        join(client, LOUNGE, GARAGE)
        household.settle()

        response = client.post(f"/api/speakers/{LOUNGE}/volume", json={"volume": 10})
        household.settle()

        assert response.get_json() == {"volume": 10, "muted": False}
        garage = client.get(f"/api/speakers/{GARAGE}").get_json()
        lounge = client.get(f"/api/speakers/{LOUNGE}").get_json()
        assert (garage["volume"], lounge["volume"]) == (20, 10)
        assert garage["group"]["volume"] == lounge["group"]["volume"] == 15

    @pytest.mark.parametrize(
        ("speaker_id", "member_id"),
        [
            pytest.param(LOUNGE, LOUNGE, id="the room itself"),
            pytest.param(LOUNGE, GARAGE, id="the coordinator of its group"),
            pytest.param(GARAGE, LOUNGE, id="a room of the group it leads"),
            pytest.param(GARAGE, GARAGE, id="the coordinator itself"),
            pytest.param(KITCHEN, KITCHEN, id="a room alone, itself"),
        ],
    )
    def test_a_join_to_the_group_the_room_plays_in_sends_nothing(
        self, client, three_rooms, connect, speaker_id, member_id
    ):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.settle()
        stream = connect()
        stream.events()
        sent = calls_from_here(three_rooms)

        response = join(client, speaker_id, member_id)
        three_rooms.settle()

        assert response.status_code == 204
        assert response.data == b""
        assert sent == []
        assert groups(client) == {GARAGE: [GARAGE, LOUNGE], KITCHEN: [KITCHEN]}
        assert stream.events() == []

    @pytest.mark.parametrize("body", REFUSED_JOINS)
    def test_a_body_that_does_not_name_a_speaker_is_refused(
        self, client, household, body
    ):
        sent = calls_from_here(household)

        response = client.put(f"/api/speakers/{LOUNGE}/group", json=body)

        assert response.status_code == 400
        assert sent == []

    def test_a_request_without_a_json_body_is_refused(self, client, household):
        sent = calls_from_here(household)

        without = client.put(f"/api/speakers/{LOUNGE}/group")
        text = client.put(
            f"/api/speakers/{LOUNGE}/group",
            data=f"speaker_id={GARAGE}",
            content_type="text/plain",
        )
        broken = client.put(
            f"/api/speakers/{LOUNGE}/group",
            data='{"speaker_id": ',
            content_type="application/json",
        )

        assert (without.status_code, text.status_code, broken.status_code) == (
            400,
            400,
            400,
        )
        assert sent == []

    @pytest.mark.parametrize("speaker_id", NO_SPEAKERS)
    def test_an_unknown_speaker_in_the_path_is_not_found(
        self, client, household, speaker_id
    ):
        sent = calls_from_here(household)

        response = join(client, speaker_id, GARAGE)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(
            speaker_id
        )
        assert sent == []

    @pytest.mark.parametrize("member_id", [*NO_SPEAKERS, ""])
    def test_an_unknown_speaker_in_the_body_is_not_found(
        self, client, household, member_id
    ):
        sent = calls_from_here(household)

        response = join(client, LOUNGE, member_id)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(
            member_id
        )
        assert sent == []

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        household.silence_room(LOUNGE)
        try:
            response = join(client, LOUNGE, GARAGE)
        finally:
            household.answer_again(LOUNGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == speaker_failed(
            f"join the group of speaker {GARAGE} with speaker {LOUNGE}"
        )
        assert groups(client) == {GARAGE: [GARAGE], LOUNGE: [LOUNGE]}

    def test_only_the_room_that_moves_has_to_answer(self, client, household):
        household.silence_room(GARAGE)
        try:
            response = join(client, LOUNGE, GARAGE)
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 204
        assert household.read_household().groups == (Group(GARAGE, (GARAGE, LOUNGE)),)

    def test_a_join_is_a_put(self, client, household):
        response = client.post(
            f"/api/speakers/{LOUNGE}/group", json={"speaker_id": GARAGE}
        )

        assert response.status_code == 405


class TestLeaveGroup:
    def test_the_room_leaves_its_group_and_plays_alone(self, client, household):
        join(client, LOUNGE, GARAGE)
        household.settle()

        response = leave(client, LOUNGE)
        household.settle()

        assert response.status_code == 204
        assert response.data == b""
        assert client.get("/api/speakers").get_json() == {
            "data": [GARAGE_ALONE, LOUNGE_ALONE]
        }

    def test_a_room_leaves_a_group_made_elsewhere(self, client, household):
        household.join(GARAGE, LOUNGE)
        household.settle()
        assert groups(client) == {LOUNGE: [GARAGE, LOUNGE]}
        sent = calls_from_here(household)

        response = leave(client, GARAGE)
        household.settle()

        assert response.status_code == 204
        assert sent == [GARAGE]
        assert groups(client) == {GARAGE: [GARAGE], LOUNGE: [LOUNGE]}

    def test_the_other_rooms_stay_a_group(self, client, three_rooms):
        join(client, LOUNGE, GARAGE)
        join(client, KITCHEN, GARAGE)
        three_rooms.settle()

        leave(client, LOUNGE)
        three_rooms.settle()

        assert groups(client) == {GARAGE: [GARAGE, KITCHEN], LOUNGE: [LOUNGE]}

    def test_a_coordinator_leaves_the_group_it_leads(
        self, client, three_rooms, connect
    ):
        three_rooms.load_queue(LOUNGE, [queue_track(7)])
        join(client, LOUNGE, GARAGE)
        join(client, KITCHEN, GARAGE)
        three_rooms.settle()
        stream = connect()
        stream.events()

        response = leave(client, GARAGE)
        three_rooms.settle()

        assert response.status_code == 204
        # The rooms the Garage led are a group of the list still.
        assert groups(client) == {GARAGE: [GARAGE], LOUNGE: [LOUNGE, KITCHEN]}
        titles = {
            speaker_id: client.get(f"/api/speakers/{speaker_id}/playback").get_json()[
                "track"
            ]["title"]
            for speaker_id in (GARAGE, KITCHEN, LOUNGE)
        }
        assert titles == {GARAGE: "Track 1", KITCHEN: "Track 7", LOUNGE: "Track 7"}
        # Every room's group changed, and the Garage plays what it played.
        assert changed(stream.events()) == [
            ("playback_changed", LOUNGE),
            ("playback_changed", KITCHEN),
            ("speakers_changed", None),
            ("volume_changed", GARAGE),
            ("volume_changed", LOUNGE),
            ("volume_changed", KITCHEN),
        ]

    def test_the_list_shows_the_room_alone_once_the_speakers_report_it(
        self, client, household
    ):
        household.join(LOUNGE, GARAGE)
        household.settle()

        with nothing_reported(household):
            response = leave(client, LOUNGE)
            sent = household.read_household().groups
            served = groups(client)
        household.settle()

        assert response.status_code == 204
        assert sent == (Group(GARAGE, (GARAGE,)), Group(LOUNGE, (LOUNGE,)))
        assert served == {GARAGE: [GARAGE, LOUNGE]}
        assert groups(client) == {GARAGE: [GARAGE], LOUNGE: [LOUNGE]}

    def test_the_speakers_events_reach_every_connection(
        self, client, household, connect
    ):
        join(client, LOUNGE, GARAGE)
        household.settle()
        streams = [connect(), connect()]
        for stream in streams:
            stream.events()

        leave(client, LOUNGE)
        household.settle()

        # Both rooms' group changed, and the Lounge plays its own again.
        for stream in streams:
            assert changed(stream.events()) == [
                ("playback_changed", LOUNGE),
                ("speakers_changed", None),
                ("volume_changed", GARAGE),
                ("volume_changed", LOUNGE),
            ]

    def test_a_leave_by_a_room_alone_sends_nothing(self, client, household, connect):
        stream = connect()
        stream.events()
        sent = calls_from_here(household)

        response = leave(client, LOUNGE)
        household.settle()

        assert response.status_code == 204
        assert response.data == b""
        assert sent == []
        assert groups(client) == {GARAGE: [GARAGE], LOUNGE: [LOUNGE]}
        assert stream.events() == []

    @pytest.mark.parametrize("speaker_id", NO_SPEAKERS)
    def test_an_unknown_speaker_is_not_found(self, client, household, speaker_id):
        sent = calls_from_here(household)

        response = leave(client, speaker_id)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(
            speaker_id
        )
        assert sent == []

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        join(client, LOUNGE, GARAGE)
        household.settle()
        household.silence_room(LOUNGE)
        try:
            response = leave(client, LOUNGE)
        finally:
            household.answer_again(LOUNGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == speaker_failed(
            f"ungroup speaker {LOUNGE}"
        )
        assert groups(client) == {GARAGE: [GARAGE, LOUNGE]}


class TestChangeVolume:
    def test_a_volume_is_set(self, client, household):
        response = client.post(f"/api/speakers/{GARAGE}/volume", json={"volume": 35})

        assert response.status_code == 200
        assert response.get_json() == {"volume": 35, "muted": False}
        assert household.read_volume(GARAGE) == Volume(35, False)

    @pytest.mark.parametrize("volume", [0, 100])
    def test_the_volumes_at_the_limits_are_set(self, client, household, volume):
        response = client.post(
            f"/api/speakers/{GARAGE}/volume", json={"volume": volume}
        )

        assert response.status_code == 200
        assert response.get_json() == {"volume": volume, "muted": False}

    def test_the_steps_of_two_clients_add_up(self, client, household):
        first = client.post(f"/api/speakers/{GARAGE}/volume", json={"delta": 4})
        second = client.post(f"/api/speakers/{GARAGE}/volume", json={"delta": 4})

        assert first.get_json() == {"volume": 24, "muted": False}
        assert second.get_json() == {"volume": 28, "muted": False}

    @pytest.mark.parametrize(("delta", "volume"), [(-100, 0), (100, 100), (0, 20)])
    def test_the_speaker_keeps_a_step_within_its_range(
        self, client, household, delta, volume
    ):
        response = client.post(f"/api/speakers/{GARAGE}/volume", json={"delta": delta})

        assert response.status_code == 200
        assert response.get_json() == {"volume": volume, "muted": False}

    def test_the_mute_changes_with_it(self, client, household):
        muted = client.post(
            f"/api/speakers/{GARAGE}/volume", json={"delta": 0, "muted": True}
        )
        kept = client.post(f"/api/speakers/{GARAGE}/volume", json={"volume": 10})
        unmuted = client.post(
            f"/api/speakers/{GARAGE}/volume", json={"volume": 12, "muted": False}
        )

        assert muted.get_json() == {"volume": 20, "muted": True}
        assert kept.get_json() == {"volume": 10, "muted": True}
        assert unmuted.get_json() == {"volume": 12, "muted": False}

    def test_it_moves_this_room_alone_in_a_group(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        response = client.post(f"/api/speakers/{LOUNGE}/volume", json={"volume": 10})
        household.settle()

        assert response.get_json() == {"volume": 10, "muted": False}
        assert client.get(f"/api/speakers/{GARAGE}").get_json()["volume"] == 20
        lounge = client.get(f"/api/speakers/{LOUNGE}").get_json()
        assert (lounge["volume"], lounge["group"]["volume"]) == (10, 15)

    def test_the_speakers_event_brings_the_change_to_the_speaker(
        self, client, household, connect
    ):
        stream = connect()
        stream.events()

        client.post(f"/api/speakers/{GARAGE}/volume", json={"volume": 35})
        household.settle()

        # The room's volume and its group's each report the change.
        events = stream.events()
        assert events != []
        assert all(
            event == {"type": "volume_changed", "payload": {"speaker_id": GARAGE}}
            for event in events
        )
        assert client.get(f"/api/speakers/{GARAGE}").get_json()["volume"] == 35

    @pytest.mark.parametrize("body", REFUSED_BODIES)
    def test_a_body_that_is_not_a_change_is_refused(self, client, household, body):
        response = client.post(f"/api/speakers/{GARAGE}/volume", json=body)

        assert response.status_code == 400
        assert household.read_volume(GARAGE) == Volume(20, False)

    def test_a_request_without_a_json_body_is_refused(self, client, household):
        without = client.post(f"/api/speakers/{GARAGE}/volume")
        text = client.post(
            f"/api/speakers/{GARAGE}/volume", data="volume=10", content_type="text/plain"
        )
        broken = client.post(
            f"/api/speakers/{GARAGE}/volume",
            data='{"volume": ',
            content_type="application/json",
        )

        assert (without.status_code, text.status_code, broken.status_code) == (
            400,
            400,
            400,
        )
        assert household.read_volume(GARAGE) == Volume(20, False)

    def test_an_unknown_speaker_is_not_found(self, client, household):
        response = client.post(f"/api/speakers/{NOWHERE}/volume", json={"volume": 35})

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(NOWHERE)

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        household.silence_room(GARAGE)
        try:
            response = client.post(
                f"/api/speakers/{GARAGE}/volume", json={"volume": 35}
            )
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == speaker_failed(
            f"set the volume of speaker {GARAGE}"
        )


class TestChangeGroupVolume:
    def test_for_a_room_alone_it_is_the_rooms_volume(self, client, household):
        response = client.post(
            f"/api/speakers/{GARAGE}/group/volume", json={"volume": 35}
        )
        household.settle()

        assert response.status_code == 200
        assert response.get_json() == {"volume": 35, "muted": False}
        speaker = client.get(f"/api/speakers/{GARAGE}").get_json()
        assert (speaker["volume"], speaker["group"]["volume"]) == (35, 35)

    def test_it_moves_every_room_of_the_group(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        response = client.post(
            f"/api/speakers/{LOUNGE}/group/volume", json={"volume": 60}
        )
        household.settle()

        assert response.get_json() == {"volume": 60, "muted": False}
        garage = client.get(f"/api/speakers/{GARAGE}").get_json()
        lounge = client.get(f"/api/speakers/{LOUNGE}").get_json()
        assert (garage["volume"], lounge["volume"]) == (40, 80)
        assert garage["group"]["volume"] == lounge["group"]["volume"] == 60

    def test_a_delta_is_a_step_of_the_group(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        louder = client.post(f"/api/speakers/{GARAGE}/group/volume", json={"delta": 10})
        silent = client.post(
            f"/api/speakers/{GARAGE}/group/volume", json={"delta": -100}
        )

        assert louder.get_json() == {"volume": 40, "muted": False}
        assert silent.get_json() == {"volume": 0, "muted": False}

    def test_the_mute_is_every_rooms(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        response = client.post(
            f"/api/speakers/{LOUNGE}/group/volume", json={"delta": 0, "muted": True}
        )
        household.settle()

        assert response.get_json() == {"volume": 30, "muted": True}
        for speaker_id in (GARAGE, LOUNGE):
            speaker = client.get(f"/api/speakers/{speaker_id}").get_json()
            assert (speaker["muted"], speaker["group"]["muted"]) == (True, True)

    def test_every_room_of_the_group_gets_the_event(self, client, household, connect):
        household.join(LOUNGE, GARAGE)
        household.settle()
        stream = connect()
        stream.events()

        client.post(f"/api/speakers/{GARAGE}/group/volume", json={"delta": 10})
        household.settle()

        events = stream.events()
        assert {event["payload"]["speaker_id"] for event in events} == {
            GARAGE,
            LOUNGE,
        }
        assert {event["type"] for event in events} == {"volume_changed"}

    @pytest.mark.parametrize("body", REFUSED_BODIES)
    def test_a_body_that_is_not_a_change_is_refused(self, client, household, body):
        response = client.post(f"/api/speakers/{GARAGE}/group/volume", json=body)

        assert response.status_code == 400
        assert household.read_group_volume(GARAGE) == Volume(20, False)

    def test_a_request_without_a_json_body_is_refused(self, client, household):
        response = client.post(f"/api/speakers/{GARAGE}/group/volume")

        assert response.status_code == 400

    def test_an_unknown_speaker_is_not_found(self, client, household):
        response = client.post(
            f"/api/speakers/{NOWHERE}/group/volume", json={"delta": 1}
        )

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == speaker_not_found(NOWHERE)

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        household.silence_room(GARAGE)
        try:
            response = client.post(
                f"/api/speakers/{GARAGE}/group/volume", json={"delta": 1}
            )
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == speaker_failed(
            f"change the group volume of speaker {GARAGE}"
        )


class TestSpeakersDocument:
    @pytest.fixture
    def paths(self, openapi_document) -> dict:
        return openapi_document["paths"]

    def test_the_list(self, openapi_document, paths):
        operation = paths["/api/speakers"]["get"]

        assert operation["summary"] == "Every speaker of the household."
        assert "`speakers_changed`" in operation["description"]
        assert operation["parameters"] == []
        assert sorted(operation["responses"]) == ["200"]
        answer = operation["responses"]["200"]
        assert answer["description"] == "Every speaker, by name."
        assert undescribed(openapi_document, json_schema(openapi_document, answer)) == []

    def test_one_speaker(self, openapi_document, paths):
        operation = paths["/api/speakers/{speaker_id}"]["get"]

        assert operation["summary"] == "One speaker, with its volume and its group."
        assert "`volume_changed`" in operation["description"]
        assert sorted(operation["responses"]) == ["200", "404"]
        speaker = json_schema(openapi_document, operation["responses"]["200"])
        assert sorted(speaker["properties"]) == [
            "group",
            "id",
            "model",
            "muted",
            "name",
            "volume",
        ]
        assert sorted(speaker["required"]) == sorted(speaker["properties"])
        assert undescribed(openapi_document, speaker) == []
        assert "does not change" in speaker["properties"]["id"]["description"]

    @pytest.mark.parametrize(
        ("path", "summary"),
        [
            ("/api/speakers/{speaker_id}/volume", "Change the speaker's own volume."),
            (
                "/api/speakers/{speaker_id}/group/volume",
                "Change the volume of the speaker's group.",
            ),
        ],
    )
    def test_a_volume_change(self, openapi_document, paths, path, summary):
        operation = paths[path]["post"]

        assert operation["summary"] == summary
        assert sorted(operation["responses"]) == ["200", "400", "404", "502"]
        change = json_schema(openapi_document, operation["requestBody"])
        assert operation["requestBody"]["required"] is True
        assert sorted(change["properties"]) == ["delta", "muted", "volume"]
        assert "one of the two" in change["description"]
        assert change["additionalProperties"] is False
        assert undescribed(openapi_document, change) == []
        volume = json_schema(openapi_document, operation["responses"]["200"])
        assert sorted(volume["required"]) == ["muted", "volume"]
        assert (volume["properties"]["volume"]["minimum"], 100) == (
            0,
            volume["properties"]["volume"]["maximum"],
        )
        assert undescribed(openapi_document, volume) == []

    def test_the_join(self, openapi_document, paths):
        operation = paths["/api/speakers/{speaker_id}/group"]["put"]

        assert operation["summary"] == "Have the speaker join another speaker's group."
        description = " ".join(operation["description"].split())
        for part in (
            "The speaker in the path moves",
            "until the speakers have reported the new ones",
            "`speakers_changed`",
            "`volume_changed`",
            "`playback_changed`",
        ):
            assert part in description
        assert sorted(operation["responses"]) == ["204", "400", "404", "502"]
        assert "content" not in operation["responses"]["204"]
        assert "the body" in operation["responses"]["404"]["description"]
        assert operation["requestBody"]["required"] is True
        body = json_schema(openapi_document, operation["requestBody"])
        assert body["description"] != ""
        assert sorted(body["properties"]) == ["speaker_id"]
        assert body["required"] == ["speaker_id"]
        assert body["properties"]["speaker_id"]["type"] == "string"
        assert body["additionalProperties"] is False
        assert undescribed(openapi_document, body) == []

    def test_the_leave(self, paths):
        operation = paths["/api/speakers/{speaker_id}/group"]["delete"]

        assert operation["summary"] == (
            "Have the speaker leave its group and play alone."
        )
        description = " ".join(operation["description"].split())
        for part in (
            "until the speakers have reported the new ones",
            "`speakers_changed`",
            "`volume_changed`",
            "`playback_changed`",
        ):
            assert part in description
        assert sorted(operation["responses"]) == ["204", "404", "502"]
        assert "content" not in operation["responses"]["204"]
        assert "requestBody" not in operation

    def test_the_group_takes_a_put_and_a_delete_alone(self, paths):
        assert sorted(paths["/api/speakers/{speaker_id}/group"]) == ["delete", "put"]

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/api/speakers/{speaker_id}", "get"),
            ("/api/speakers/{speaker_id}/group", "put"),
            ("/api/speakers/{speaker_id}/group", "delete"),
            ("/api/speakers/{speaker_id}/volume", "post"),
            ("/api/speakers/{speaker_id}/group/volume", "post"),
        ],
    )
    def test_every_status_and_the_speaker_are_described(
        self, openapi_document, paths, path, method
    ):
        operation = paths[path][method]

        (speaker_id,) = operation["parameters"]
        assert (speaker_id["name"], speaker_id["in"]) == ("speaker_id", "path")
        assert "its Sonos id" in speaker_id["description"]
        for status, answer in operation["responses"].items():
            assert answer["description"] != "", status
            if status not in ("200", "204"):
                schema = answer["content"]["application/json"]["schema"]
                assert "ApiErrorResponseSchema" in schema["$ref"], status
        assert "`SPEAKER_NOT_FOUND`" in operation["responses"]["404"]["description"]
