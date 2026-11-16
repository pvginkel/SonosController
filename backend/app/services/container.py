"""Dependency injection container for services."""

from collections.abc import Callable
from typing import Any

from dependency_injector import containers, providers
from sqlalchemy.orm import sessionmaker

from app.app_config import AppSettings
from app.config import Settings
from app.services.artwork_service import ArtworkService
from app.services.event_sender import EventSender
from app.services.frontend_version_service import FrontendVersionService
from app.services.health_service import HealthService
from app.services.household_mirror import HouseholdMirror
from app.services.library_service import LibraryService
from app.services.metrics_service import MetricsService
from app.services.plex_client import PlexapiPlexClient
from app.services.queue_service import QueueService
from app.services.sonos_client import SoCoSonosClient
from app.services.speaker_service import SpeakerService
from app.services.sse_connection_manager import SSEConnectionManager
from app.services.task_service import TaskService
from app.services.testing_service import TestingService
from app.utils.lifecycle_coordinator import LifecycleCoordinator
from app.utils.temp_file_manager import TempFileManager

# Background service startup registry. Services register lambdas here
# (co-located with their provider definitions) that are invoked by
# start_background_services() during app startup.
_background_starters: list[Callable[[Any], None]] = []


def register_for_background_startup(fn: Callable[[Any], None]) -> None:
    """Register a callable to be invoked during background service startup."""
    _background_starters.append(fn)


class ServiceContainer(containers.DeclarativeContainer):
    """Container for service dependency injection."""

    # Configuration providers
    config = providers.Dependency(instance_of=Settings)
    app_config = providers.Dependency(instance_of=AppSettings)
    session_maker = providers.Dependency(instance_of=sessionmaker)
    db_session = providers.ContextLocalSingleton(session_maker.provided.call())

    # Lifecycle coordinator - manages startup and graceful shutdown
    lifecycle_coordinator = providers.Singleton(
        LifecycleCoordinator,
        graceful_shutdown_timeout=config.provided.graceful_shutdown_timeout,
    )

    # Health service - services register their own readiness checks with it
    health_service = providers.Singleton(
        HealthService,
        lifecycle_coordinator=lifecycle_coordinator,
        settings=config,
    )

    # Sonos client - the only way to the speakers; pytest overrides it with a
    # fake. Built at startup so that its readiness check is registered.
    sonos_client = providers.Singleton(
        SoCoSonosClient,
        settings=app_config,
        health_service=health_service,
    )
    register_for_background_startup(lambda c: c.sonos_client())

    # Plex client - the only way to Plex; pytest overrides it with a fake.
    # Built at startup so that its readiness check is registered.
    plex_client = providers.Singleton(
        PlexapiPlexClient,
        settings=app_config,
        health_service=health_service,
    )
    register_for_background_startup(lambda c: c.plex_client())

    # Testing service - test sessions, content generation, the SSE test hooks
    testing_service = providers.Factory(TestingService)

    # Temp file manager
    temp_file_manager = providers.Singleton(
        TempFileManager,
        lifecycle_coordinator=lifecycle_coordinator,
    )
    register_for_background_startup(
        lambda c: c.temp_file_manager().start_cleanup_thread()
    )

    # Metrics service - background thread for Prometheus metrics
    metrics_service = providers.Singleton(
        MetricsService,
        container=providers.Self(),
        lifecycle_coordinator=lifecycle_coordinator,
    )

    # SSE connection manager (always included - TaskService depends on it)
    sse_connection_manager = providers.Singleton(
        SSEConnectionManager,
        lifecycle_coordinator=lifecycle_coordinator,
        queue_max_frames=config.provided.sse_queue_max_frames,
    )

    # Task service - in-memory task management
    task_service = providers.Singleton(
        TaskService,
        lifecycle_coordinator=lifecycle_coordinator,
        sse_connection_manager=sse_connection_manager,
        max_workers=config.provided.task_max_workers,
        task_timeout=config.provided.task_timeout_seconds,
        cleanup_interval=config.provided.task_cleanup_interval_seconds,
    )
    register_for_background_startup(lambda c: c.task_service().startup())

    # Frontend version service - SSE version notifications
    frontend_version_service = providers.Singleton(
        FrontendVersionService,
        settings=config,
        lifecycle_coordinator=lifecycle_coordinator,
        sse_connection_manager=sse_connection_manager,
    )
    register_for_background_startup(lambda c: c.frontend_version_service())

    # Event sender - the app's events on the SSE stream, coalesced
    event_sender = providers.Singleton(
        EventSender,
        sse_connection_manager=sse_connection_manager,
        lifecycle_coordinator=lifecycle_coordinator,
    )

    # Household mirror - the speakers' state in memory, fed by their events.
    # Built at startup; it mirrors the household on the STARTUP event.
    household_mirror = providers.Singleton(
        HouseholdMirror,
        sonos_client=sonos_client,
        plex_client=plex_client,
        event_sender=event_sender,
        lifecycle_coordinator=lifecycle_coordinator,
    )
    register_for_background_startup(lambda c: c.household_mirror())

    # Artwork service - pictures by artwork id; it keeps the finished ones
    artwork_service = providers.Singleton(
        ArtworkService,
        household_mirror=household_mirror,
        sonos_client=sonos_client,
        plex_client=plex_client,
    )

    # Library service - the music library, read from Plex on each request
    library_service = providers.Factory(
        LibraryService,
        plex_client=plex_client,
        playlist_max_tracks=app_config.provided.playlist_max_tracks,
    )

    # Speaker service - playback and volume commands to the speakers
    speaker_service = providers.Factory(
        SpeakerService,
        household_mirror=household_mirror,
        sonos_client=sonos_client,
    )

    # Queue service - a group's queue, read from and changed on the speaker
    queue_service = providers.Factory(
        QueueService,
        household_mirror=household_mirror,
        sonos_client=sonos_client,
        plex_client=plex_client,
        task_service=task_service,
        playlist_max_tracks=app_config.provided.playlist_max_tracks,
    )


def start_background_services(container: Any) -> None:
    """Eagerly instantiate and start all registered background services."""
    for starter in _background_starters:
        starter(container)
