#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""Unit tests for the built-in metadata pass in chunk_post_processor."""

from unittest.mock import MagicMock, patch

from rag.svr.task_executor_refactor.chunk_post_processor import apply_built_in_metadata

_SERVICE = "rag.svr.task_executor_refactor.chunk_post_processor.DocMetadataService"


def _ctx(parser_config):
    ctx = MagicMock()
    ctx.parser_config = parser_config
    ctx.doc_id = "doc-1"
    ctx.name = "report.pdf"
    ctx.write_interceptor = None
    return ctx


def test_skips_without_built_in_config():
    ctx = _ctx({})
    with patch(_SERVICE) as service:
        apply_built_in_metadata(ctx)
    service.get_document_metadata.assert_not_called()
    service.update_document_metadata.assert_not_called()


def test_skips_when_no_built_in_key_matches():
    ctx = _ctx({"built_in_metadata": [{"key": "unknown_field"}]})
    with patch(_SERVICE) as service:
        apply_built_in_metadata(ctx)
    service.update_document_metadata.assert_not_called()


def test_writes_file_name_and_update_time_into_existing_metadata():
    ctx = _ctx({"built_in_metadata": [{"key": "update_time"}, {"key": "file_name"}]})
    with patch(_SERVICE) as service:
        service.get_document_metadata.return_value = {"author": "alice"}
        apply_built_in_metadata(ctx)

    service.get_document_metadata.assert_called_once_with("doc-1")
    doc_id, written = service.update_document_metadata.call_args.args
    assert doc_id == "doc-1"
    assert written["author"] == "alice"
    assert written["file_name"] == "report.pdf"
    assert len(written["update_time"]) == 19  # "YYYY-MM-DD HH:MM:SS"


def test_built_in_value_overrides_stale_existing_scalar():
    ctx = _ctx({"built_in_metadata": [{"key": "file_name"}]})
    with patch(_SERVICE) as service:
        service.get_document_metadata.return_value = {"file_name": "stale.pdf"}
        apply_built_in_metadata(ctx)

    _doc_id, written = service.update_document_metadata.call_args.args
    assert written["file_name"] == "report.pdf"


def test_routes_through_write_interceptor_when_present():
    ctx = _ctx({"built_in_metadata": [{"key": "file_name"}]})
    ctx.write_interceptor = MagicMock()
    with patch(_SERVICE) as service:
        service.get_document_metadata.return_value = {}
        apply_built_in_metadata(ctx)

    ctx.write_interceptor.intercept.assert_called_once_with("DocMetadataService.update_document_metadata")
    service.update_document_metadata.assert_not_called()
