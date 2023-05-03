from unittest.mock import Mock

import pytest
from h_matchers import Any

from h.services.url_migration import URLMigrationService


class TestURLMigrationService:
    def test_move_annotations_does_nothing_if_annotation_was_deleted(
        self, svc, annotation_write_service
    ):
        svc.move_annotations(
            ["id-that-does-not-exist"],
            "https://somesite.com",
            {"url": "https://example.org"},
        )
        annotation_write_service.update_annotation.assert_not_called()

    def test_move_annotations_does_nothing_if_url_no_longer_matches(
        self, svc, db_session, factories, annotation_write_service
    ):
        ann = factories.Annotation(target_uri="https://example.com")
        db_session.flush()

        svc.move_annotations(
            [ann.id],
            # Use a different URL to simulate the case where the annotation's
            # URL is changed in between a move being scheduled, and the move
            # being executed.
            "https://wrongsite.com",
            {"url": "https://example.org"},
        )

        assert ann.target_uri == "https://example.com"
        annotation_write_service.update_annotation.assert_not_called()

    def test_move_annotations_updates_urls(
        self, svc, db_session, factories, annotation_write_service
    ):
        anns = [
            factories.Annotation(target_uri="https://example.com"),
            factories.Annotation(target_uri="https://example.com"),
        ]
        db_session.flush()

        svc.move_annotations(
            [anns[0].id, anns[1].id],
            "https://example.com",
            {"url": "https://example.org"},
        )

        assert annotation_write_service.update_annotation.call_count == 2
        for ann in anns[0:2]:
            annotation_write_service.update_annotation.assert_any_call(
                annotation=ann,
                data={"target_uri": "https://example.org"},
                update_timestamp=False,
                reindex_tag="URLMigrationService.move_annotations",
            )

    def test_move_annotations_updates_selectors(
        self, svc, db_session, factories, annotation_write_service
    ):
        ann = factories.Annotation(target_uri="https://example.com")
        ann.target_selectors = [
            {"type": "TextQuoteSelector", "exact": "foobar"},
            {"type": "EPUBContentSelector", "cfi": "/2/4"},
        ]
        db_session.flush()

        svc.move_annotations(
            [ann.id],
            "https://example.com",
            {
                "url": "https://example.org",
                "selectors": [
                    # New selector that is not in existing selectors. This should be added.
                    {"type": "PageSelector", "label": "3"},
                    # Selector that matches an existing selector. This should not be duplicated.
                    {"type": "EPUBContentSelector", "cfi": "/2/4"},
                ],
            },
        )

        annotation_write_service.update_annotation.assert_called_once_with(
            annotation=ann,
            data={
                "target_uri": "https://example.org",
                "target_selectors": [
                    {"type": "TextQuoteSelector", "exact": "foobar"},
                    {"type": "EPUBContentSelector", "cfi": "/2/4"},
                    {"type": "PageSelector", "label": "3"},
                ],
            },
            update_timestamp=False,
            reindex_tag="URLMigrationService.move_annotations",
        )

    def test_move_annotations_updates_documents(
        self, svc, db_session, factories, annotation_write_service, transform_document
    ):
        ann = factories.Annotation(target_uri="https://example.com")
        db_session.flush()

        svc.move_annotations(
            [ann.id],
            "https://example.com",
            {
                "url": "https://example.org",
                "document": {"title": "The new example.com"},
            },
        )

        transform_document.assert_called_with(
            {"title": "The new example.com"}, "https://example.org"
        )
        annotation_write_service.update_annotation.assert_called_once_with(
            annotation=ann,
            data={
                "target_uri": "https://example.org",
                "document": transform_document.return_value,
            },
            update_timestamp=False,
            reindex_tag="URLMigrationService.move_annotations",
        )

    def test_move_annotations_by_url_moves_matching_annotations(
        self,
        svc,
        db_session,
        factories,
        pyramid_request,
        annotation_write_service,
        move_annotations_task,
    ):
        anns = [
            factories.Annotation(target_uri="https://example.com"),
            factories.Annotation(target_uri="https://example.com"),
            factories.Annotation(target_uri="https://othersite.com"),
            factories.Annotation(target_uri="https://example.com"),
        ]
        db_session.flush()

        svc.move_annotations_by_url(
            "https://example.com",
            {"url": "https://example.org"},
        )

        # First annotation should be moved synchronously.
        annotation_write_service.update_annotation.assert_called_once_with(
            anns[0],
            {"target_uri": "https://example.org"},
            update_timestamp=False,
            reindex_tag="URLMigrationService.move_annotations",
        )
        pyramid_request.tm.commit.assert_called_once()

        remaining_ann_ids = [
            a.id for a in anns[1:] if a.target_uri == "https://example.com"
        ]

        # Remaining matching annotations should be moved in separate tasks.
        assert move_annotations_task.delay.call_count == 1
        move_annotations_task.delay.assert_called_once_with(
            Any.list.containing(remaining_ann_ids).only(),
            "https://example.com",
            {"url": "https://example.org"},
        )

    def test_move_annotations_by_url_handles_no_matches(
        self,
        svc,
        db_session,
        factories,
        annotation_write_service,
        move_annotations_task,
    ):
        # Make sure there are some non-matching annotations in the DB.
        factories.Annotation(target_uri="https://foo.com")
        factories.Annotation(target_uri="https://bar.com")
        factories.Annotation(target_uri="https://baz.com")
        db_session.flush()

        svc.move_annotations_by_url(
            "https://example.com",
            {"url": "https://example.org"},
        )

        annotation_write_service.update_annotation.assert_not_called()
        move_annotations_task.delay.assert_not_called()

    @pytest.fixture(autouse=True)
    def transform_document(self, patch):
        return patch("h.services.url_migration.transform_document")

    @pytest.fixture(autouse=True)
    def move_annotations_task(self, patch):
        return patch("h.services.url_migration.move_annotations")

    @pytest.fixture
    def pyramid_request(self, pyramid_request):
        pyramid_request.tm = Mock(spec_set=["commit"])
        return pyramid_request

    @pytest.fixture
    def svc(self, pyramid_request, annotation_write_service):
        return URLMigrationService(
            request=pyramid_request, annotation_write_service=annotation_write_service
        )
