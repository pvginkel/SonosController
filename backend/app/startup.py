"""App-specific startup hooks.

Hook points called by create_app():
  - create_container()
  - register_blueprints()
  - register_error_handlers()
  - register_root_blueprints()

Hook points called by CLI command handlers:
  - register_cli_commands()
  - post_migration_hook()
  - load_test_data_hook()
"""

from __future__ import annotations

import click
from flask import Blueprint, Flask

from app.services.container import ServiceContainer
from app.utils import spectree_config


def create_container() -> ServiceContainer:
    """Create and configure the application's service container."""
    return ServiceContainer()


def register_blueprints(api_bp: Blueprint, app: Flask) -> None:
    """Register all app-specific blueprints on api_bp (under /api prefix)."""
    # Imported here: app.api's modules need Spectree configured when they
    # load, which create_app() has done for this app before it calls this.
    from app.api.artwork import artwork_bp
    from app.api.openapi_docs import complete_openapi_document
    from app.api.playback import playback_bp
    from app.api.speakers import speakers_bp

    # api_bp is one object for every app a process creates, and Flask refuses
    # a change to a blueprint that an app has registered.
    if not api_bp._got_registered_once:
        api_bp.register_blueprint(artwork_bp)
        api_bp.register_blueprint(speakers_bp)
        api_bp.register_blueprint(playback_bp)
    complete_openapi_document(app, spectree_config.api)


def register_error_handlers(app: Flask) -> None:
    """Register app-specific error handlers."""
    pass


def register_root_blueprints(app: Flask) -> None:
    """Register app-specific blueprints directly on the app (not under /api prefix).

    Use this for internal endpoints, WebSocket handlers, or other routes
    that should not be nested under the /api URL prefix.
    """
    pass


def register_cli_commands(cli: click.Group) -> None:
    """Register app-specific CLI commands."""
    pass


def post_migration_hook(app: Flask) -> None:
    """Run after database migrations (e.g., sync master data)."""
    pass


def load_test_data_hook(app: Flask) -> None:
    """Load test fixtures after database recreation."""
    pass
