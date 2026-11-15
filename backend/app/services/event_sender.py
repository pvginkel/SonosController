"""Sends the app's events to the event stream, coalesced.

``docs/api-design.md`` section 4: every connection receives every event, and
"the backend coalesces: at most one event of a type for a speaker in 250 ms,
with the last one always sent". An event carries no state, so two events of
one type for one speaker are equal, and the last of a burst is the one that
goes out once the 250 ms since the previous one have passed.
"""

import threading
import time

from app.schemas.stream_event_schema import StreamEvent
from app.services.sse_connection_manager import (
    LOCAL_ADMIN_TARGET,
    SSEConnectionManager,
)
from app.utils.lifecycle_coordinator import LifecycleCoordinatorProtocol, LifecycleEvent

# The least time between two equal events.
COALESCE_SECONDS = 0.25


class EventSender:
    """The one way an app event reaches the clients."""

    def __init__(
        self,
        sse_connection_manager: SSEConnectionManager,
        lifecycle_coordinator: LifecycleCoordinatorProtocol,
    ) -> None:
        self._sse_connection_manager = sse_connection_manager
        self._lock = threading.Lock()
        # When each event was last sent, on time.perf_counter()'s clock.
        self._sent_at: dict[StreamEvent, float] = {}
        # The events held back, each with the timer that sends it.
        self._waiting: dict[StreamEvent, threading.Timer] = {}
        self._stopped = False
        lifecycle_coordinator.register_lifecycle_notification(self._on_lifecycle_event)

    def send(self, event: StreamEvent) -> None:
        """Send the event to every connection, now or once its 250 ms are over.

        An event equal to one that is waiting is that one. Nothing is sent
        after ``stop()``.
        """
        with self._lock:
            if self._stopped or event in self._waiting:
                return
            last = self._sent_at.get(event)
            wait = 0.0 if last is None else last + COALESCE_SECONDS - time.perf_counter()
            if wait > 0:
                timer = threading.Timer(wait, self._send_waiting, [event])
                timer.daemon = True
                self._waiting[event] = timer
                timer.start()
                return
            self._sent_at[event] = time.perf_counter()
        self._deliver(event)

    def stop(self) -> None:
        """Send nothing more: the events that wait are dropped."""
        with self._lock:
            self._stopped = True
            waiting = list(self._waiting.values())
            self._waiting.clear()
        for timer in waiting:
            timer.cancel()

    def _send_waiting(self, event: StreamEvent) -> None:
        with self._lock:
            if self._waiting.pop(event, None) is None:
                return
            self._sent_at[event] = time.perf_counter()
        self._deliver(event)

    def _deliver(self, event: StreamEvent) -> None:
        # The app has no users to tell apart: without OIDC every connection is
        # the local admin's, so this target is every connection (section 4).
        self._sse_connection_manager.send_event(
            LOCAL_ADMIN_TARGET,
            event.payload.model_dump(mode="json"),
            event_name=event.type,
            service_type="household",
        )

    def _on_lifecycle_event(self, event: LifecycleEvent) -> None:
        if event == LifecycleEvent.PREPARE_SHUTDOWN:
            self.stop()
