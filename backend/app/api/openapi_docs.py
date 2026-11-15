"""What the generated OpenAPI document cannot say of itself, or says wrongly.

Spectree builds the document from the routes and their schemas. An answer
that is not JSON has no schema: the event stream, whose route is the
template's, and a picture. And Spectree gives every route a `400`, also one
that takes no query and no body. The document is the contract the app's
clients are built from, so the modules of those answers describe them, and
this module puts their descriptions into the document and takes out the
`400`s nothing answers.
"""

from typing import Any

from flask import Flask, request
from spectree import SpecTree

from app.api.artwork import describe_artwork
from app.api.sse_docs import describe_event_stream


def complete_openapi_document(app: Flask, api: SpecTree) -> None:
    """Have the app's OpenAPI document describe the answers without a schema,
    and no `400` that no request gets.

    Spectree builds the document when it is first asked for and keeps it;
    this completes the kept document each time it is asked for.
    """

    @app.before_request
    def complete_document() -> None:
        if request.path == api.config.spec_url:
            describe_event_stream(api.spec)
            describe_artwork(api.spec)
            drop_unanswered_validation_errors(api.spec)


def drop_unanswered_validation_errors(document: dict[str, Any]) -> None:
    """Take out of an OpenAPI document the `400` of each route that cannot
    answer one.

    Spectree documents its own validation error for every route under
    ``@api.validate`` that declares no `400` itself. It answers that error
    for a query or a body it refuses, so a route that takes neither never
    does.
    """
    for operations in document["paths"].values():
        for operation in operations.values():
            answers = operation["responses"]
            if "400" not in answers or "requestBody" in operation:
                continue
            if any(parameter["in"] != "path" for parameter in operation["parameters"]):
                continue
            schema = answers["400"]["content"]["application/json"]["schema"]
            if schema["$ref"].startswith("#/components/schemas/ValidationError."):
                del answers["400"]
