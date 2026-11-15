"""What the generated OpenAPI document cannot say of itself.

Spectree builds the document from the routes and their schemas. An answer
that is not JSON has no schema: the event stream, whose route is the
template's, and a picture. The document is the contract the app's clients are
built from, so the modules of those answers describe them, and this module
puts their descriptions into the document.
"""

from flask import Flask, request
from spectree import SpecTree

from app.api.artwork import describe_artwork
from app.api.sse_docs import describe_event_stream


def complete_openapi_document(app: Flask, api: SpecTree) -> None:
    """Have the app's OpenAPI document describe the answers without a schema.

    Spectree builds the document when it is first asked for and keeps it;
    this completes the kept document each time it is asked for.
    """

    @app.before_request
    def complete_document() -> None:
        if request.path == api.config.spec_url:
            describe_event_stream(api.spec)
            describe_artwork(api.spec)
