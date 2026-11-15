"""Tests for what is put into, and taken out of, the OpenAPI document."""

import copy
from collections.abc import Callable
from typing import Any

import pytest
from flask import Flask


@pytest.fixture
def drop(app: Flask) -> Callable[[dict[str, Any]], None]:
    # Imported here: app.api's modules need Spectree configured when they
    # load, which the app's creation has done.
    from app.api.openapi_docs import drop_unanswered_validation_errors

    return drop_unanswered_validation_errors


def statuses(document: dict, path: str, method: str) -> list[str]:
    return sorted(document["paths"][path][method]["responses"])


class TestDropUnansweredValidationErrors:
    def test_a_route_that_takes_no_query_and_no_body_has_no_400(
        self, openapi_document
    ):
        assert statuses(openapi_document, "/api/speakers", "get") == ["200"]
        assert statuses(openapi_document, "/api/speakers/{speaker_id}", "get") == [
            "200",
            "404",
        ]
        assert statuses(openapi_document, "/api/tasks/{task_id}/status", "get") == [
            "200",
            "404",
        ]

    def test_a_route_that_takes_a_query_or_a_body_keeps_its_400(
        self, openapi_document
    ):
        assert "400" in statuses(openapi_document, "/api/artwork/{artwork_id}", "get")
        assert "400" in statuses(
            openapi_document, "/api/speakers/{speaker_id}/volume", "post"
        )
        # The template's routes, which declare no 400 of their own.
        assert "400" in statuses(
            openapi_document, "/api/testing/sse/force-callback-error", "post"
        )
        assert "400" in statuses(openapi_document, "/api/testing/tasks/start", "post")

    def test_spectrees_400_is_taken_out(self, openapi_document, drop):
        document = copy.deepcopy(openapi_document)
        paths = document["paths"]
        spectrees = paths["/api/testing/tasks/start"]["post"]["responses"]["400"]
        assert "ValidationError" in (
            spectrees["content"]["application/json"]["schema"]["$ref"]
        )
        paths["/api/speakers"]["get"]["responses"]["400"] = copy.deepcopy(spectrees)

        drop(document)

        assert document == openapi_document

    def test_a_400_a_route_declares_itself_stays(self, openapi_document, drop):
        document = copy.deepcopy(openapi_document)
        operation = document["paths"]["/api/speakers"]["get"]
        operation["responses"]["400"] = copy.deepcopy(
            document["paths"]["/api/artwork/{artwork_id}"]["get"]["responses"]["400"]
        )

        drop(document)

        assert statuses(document, "/api/speakers", "get") == ["200", "400"]

    def test_it_leaves_a_complete_document_as_it_is(self, openapi_document, drop):
        document = copy.deepcopy(openapi_document)

        drop(document)

        assert document == openapi_document
