import ast
from pathlib import Path
from types import SimpleNamespace

from rag.app.parser_router import uses_table_row_tasks

_REPO = Path(__file__).resolve().parents[5]
_TASK_SERVICE_PATH = _REPO / "api" / "db" / "services" / "task_service.py"


def test_legacy_table_xlsx_queues_one_whole_file_mineru_task():
    parsed = ast.parse(_TASK_SERVICE_PATH.read_text(encoding="utf-8"))
    function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "queue_tasks")
    inserted = []

    class Hash:
        def update(self, _value):
            return None

        @staticmethod
        def hexdigest():
            return "digest"

    class DocumentService:
        @staticmethod
        def get_chunking_config(_doc_id):
            return {"parser_config": {}, "tenant_id": "tenant", "kb_id": "kb"}

        @staticmethod
        def update_by_id(*_args):
            return None

        @staticmethod
        def begin2parse(*_args):
            return None

    class TaskService:
        @staticmethod
        def get_tasks(_doc_id):
            return []

    namespace = {
        "FileType": SimpleNamespace(PDF=SimpleNamespace(value="pdf")),
        "MAXIMUM_PAGE_NUMBER": 100000,
        "MAXIMUM_TASK_PAGE_NUMBER": 100000000,
        "get_uuid": lambda: "task-id",
        "datetime": SimpleNamespace(now=lambda: SimpleNamespace(strftime=lambda _fmt: "now")),
        "uses_table_row_tasks": uses_table_row_tasks,
        "PdfParser": SimpleNamespace(total_page_number=lambda *_args: 1),
        "RAGFlowExcelParser": SimpleNamespace(row_number=lambda *_args: (_ for _ in ()).throw(AssertionError("XLSX must not use row tasks"))),
        "settings": SimpleNamespace(
            STORAGE_IMPL=SimpleNamespace(get=lambda *_args: b"xlsx"),
            docStoreConn=SimpleNamespace(delete=lambda *_args: None),
            get_svr_queue_name=lambda *_args: "queue",
        ),
        "xxhash": SimpleNamespace(xxh64=lambda: Hash()),
        "DocumentService": DocumentService,
        "TaskService": TaskService,
        "Task": object(),
        "bulk_insert_into_db": lambda _model, tasks, _replace: inserted.extend(tasks),
        "seed_doc_chunking_counter": lambda *_args: True,
        "REDIS_CONN": SimpleNamespace(queue_product=lambda *_args, **_kwargs: True),
        "search": SimpleNamespace(index_name=lambda _tenant: "index"),
        "reuse_prev_task_chunks": lambda *_args: 0,
        "abort_doc_chunking_counter": lambda *_args: None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(_TASK_SERVICE_PATH), "exec"), namespace)  # noqa: S102

    namespace["queue_tasks"](
        {
            "id": "doc",
            "type": "doc",
            "name": "legacy.xlsx",
            "parser_id": "table",
            "parser_config": {},
        },
        "bucket",
        "object",
        0,
    )

    assert len(inserted) == 1
    assert inserted[0]["from_page"] == 0
    assert inserted[0]["to_page"] == 100000000
