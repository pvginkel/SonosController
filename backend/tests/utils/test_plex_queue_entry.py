"""Tests for the reading of a Plex queue entry's address."""

import pytest

from app.utils.plex_queue_entry import PlexQueueEntry, parse_plex_queue_entry

MACHINE = "0123456789abcdef0123456789abcdef01234567"


class TestPlexQueueEntry:
    def test_an_entry_names_its_server_its_section_and_its_track(self):
        uri = f"x-sonos-http:{MACHINE}%3a3%3a48213%3atrack.mp3?sid=212&flags=8300&sn=4"

        assert parse_plex_queue_entry(uri) == PlexQueueEntry(
            machine_identifier=MACHINE, section_id="3", rating_key="48213"
        )

    def test_the_separators_are_read_in_either_case(self):
        uri = f"x-sonos-http:{MACHINE}%3A3%3A48213%3Atrack.flac?sid=212"

        assert parse_plex_queue_entry(uri) == PlexQueueEntry(
            machine_identifier=MACHINE, section_id="3", rating_key="48213"
        )

    def test_two_servers_tracks_differ_in_the_server_alone(self):
        ours = parse_plex_queue_entry("x-sonos-http:ours%3a1%3a1001%3atrack.mp3?sid=212")
        theirs = parse_plex_queue_entry(
            "x-sonos-http:theirs%3a1%3a1001%3atrack.mp3?sid=212"
        )

        assert (ours.machine_identifier, theirs.machine_identifier) == ("ours", "theirs")
        assert (ours.section_id, ours.rating_key) == (
            theirs.section_id,
            theirs.rating_key,
        )

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "x-sonos-spotify:spotify%3atrack%3a1?sid=9&flags=8224&sn=7",
            "x-rincon-queue:RINCON_FAKE0000000101400#0",
            "x-sonosapi-stream:s1234?sid=254&flags=8224&sn=0",
            # Another service's track over the same scheme.
            "x-sonos-http:track%3a1234.mp3?sid=2&flags=8224&sn=1",
            # No rating key, a container, and a section that is missing.
            f"x-sonos-http:{MACHINE}%3a3%3aabc%3atrack.mp3?sid=212",
            f"x-sonos-http:{MACHINE}%3a3%3a48213%3aalbum?sid=212",
            f"x-sonos-http:{MACHINE}%3a48213%3atrack.mp3?sid=212",
            # The shape further on in an address is not an entry.
            f"http://192.0.2.1/x-sonos-http:{MACHINE}%3a3%3a48213%3atrack.mp3",
        ],
    )
    def test_any_other_address_is_no_entry(self, uri):
        assert parse_plex_queue_entry(uri) is None
