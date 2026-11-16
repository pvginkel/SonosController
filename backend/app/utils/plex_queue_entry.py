"""The address of a queue entry from the Plex service linked in Sonos.

``docs/api-design.md`` section 1.3: such an entry names its track as
``x-sonos-http:<machine>%3a<section>%3a<rating key>%3atrack.<ext>?…``, where
``<machine>`` is the machine identifier of the Plex server the track is on.
An account can reach more servers than the one the backend is configured for,
so an entry is a track of the backend's Plex only when its machine identifier
is that server's (``PlexClient.machine_identifier()``).
"""

import re
from dataclasses import dataclass

_ENTRY = re.compile(
    r"x-sonos-http:([^%?:]+)%3a([^%?:]+)%3a([0-9]+)%3atrack\.", re.IGNORECASE
)


@dataclass(frozen=True)
class PlexQueueEntry:
    """What a Plex queue entry's address names: a track, by its rating key,
    in a library section of a Plex server."""

    machine_identifier: str
    section_id: str
    rating_key: str


def parse_plex_queue_entry(uri: str) -> PlexQueueEntry | None:
    """What a queue entry's address names; None for an address that is not a
    track of the Plex service."""
    entry = _ENTRY.match(uri)
    if entry is None:
        return None
    return PlexQueueEntry(
        machine_identifier=entry.group(1),
        section_id=entry.group(2),
        rating_key=entry.group(3),
    )
