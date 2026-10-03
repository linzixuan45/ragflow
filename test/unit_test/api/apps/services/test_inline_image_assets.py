import base64
import hashlib
import json

from api.apps.services.inline_image_assets import (
    DEFAULT_BUDGET_BYTES,
    _safe_object_name,
    load_manifest,
    materialize_inline_images,
)


class FakeStorage:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str]] = []

    def put(self, dataset_id, object_name, data):
        self.puts.append((dataset_id, object_name))
        self.objects[(dataset_id, object_name)] = data

    def get(self, dataset_id, object_name):
        self.gets.append((dataset_id, object_name))
        return self.objects.get((dataset_id, object_name))

    def exists(self, dataset_id, object_name):
        return (dataset_id, object_name) in self.objects


def _ref(image_id: str) -> str:
    return hashlib.sha256(image_id.encode()).hexdigest()


def _run(text: str, storage: FakeStorage, **overrides):
    kwargs = {
        "dataset_id": "dataset-1",
        "document_id": "doc-1",
        "fs_path": "/notes.md",
        "image_ref_of": _ref,
        "put": storage.put,
        "exists": storage.exists,
        "get": storage.get,
    }
    kwargs.update(overrides)
    return materialize_inline_images(text, **kwargs)


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_OBJECT = f"inline/{hashlib.sha256(PNG_BYTES).hexdigest()}.png"
PNG_IMAGE_ID = f"dataset-1-{PNG_OBJECT}"
MANIFEST_NAME = "inline/doc-1.index.json"


def test_default_budget_matches_the_documented_magnitude():
    assert DEFAULT_BUDGET_BYTES == 32 * 1024 * 1024


def test_r1_inline_data_url_is_replaced_and_registered():
    storage = FakeStorage()
    text = f'<img src="data:image/png;base64,{PNG_B64}" alt="图">'

    result, manifest = _run(text, storage)

    assert result == f"[[image:dataset/dataset-1/notes.md|image_ref={_ref(PNG_IMAGE_ID)}]]"
    assert storage.objects[("dataset-1", PNG_OBJECT)] == PNG_BYTES
    assert storage.puts.count(("dataset-1", PNG_OBJECT)) == 1
    assert manifest["image_ids"] == [PNG_IMAGE_ID]
    stored = json.loads(storage.objects[("dataset-1", MANIFEST_NAME)].decode("utf-8"))
    assert stored == {"image_ids": [PNG_IMAGE_ID]}


def test_markdown_image_is_replaced_by_the_placeholder():
    storage = FakeStorage()
    text = f"![示意图](data:image/png;base64,{PNG_B64})"

    result, _ = _run(text, storage)

    assert result == f"[[image:dataset/dataset-1/notes.md|image_ref={_ref(PNG_IMAGE_ID)}]]"


def test_marker_path_is_normalized_to_a_single_leading_slash():
    storage = FakeStorage()
    text = f'<img src="data:image/png;base64,{PNG_B64}">'

    result, _ = _run(text, storage, fs_path="//notes.md")

    assert result == f"[[image:dataset/dataset-1/notes.md|image_ref={_ref(PNG_IMAGE_ID)}]]"


def test_r2_repeat_materialization_does_not_write_again():
    storage = FakeStorage()
    text = f'<img src="data:image/png;base64,{PNG_B64}">'
    first, _ = _run(text, storage)
    puts_after_first = list(storage.puts)

    second, _ = _run(text, storage)
    third, _ = _run(first, storage)

    assert second == first
    assert third == first
    assert storage.puts == puts_after_first


def test_existing_manifest_is_merged_without_rewriting_unchanged_sets():
    storage = FakeStorage()
    existing_id = "dataset-1-inline/existing.png"
    storage.objects[("dataset-1", MANIFEST_NAME)] = json.dumps({"image_ids": [existing_id]}).encode("utf-8")
    text = f'<img src="data:image/png;base64,{PNG_B64}">'

    _, manifest = _run(text, storage)

    assert manifest["image_ids"] == [existing_id, PNG_IMAGE_ID]
    assert storage.puts == [("dataset-1", PNG_OBJECT), ("dataset-1", MANIFEST_NAME)]


def test_r4_non_image_and_invalid_base64_are_kept_verbatim():
    storage = FakeStorage()
    non_image = '<img src="data:text/html;base64,PGI+">'
    broken_base64 = '<img src="data:image/png;base64,AAAAA">'

    assert _run(non_image, storage)[0] == non_image
    assert _run(broken_base64, storage)[0] == broken_base64
    assert storage.puts == []


def test_unsupported_mime_is_kept_verbatim():
    storage = FakeStorage()
    text = '<img src="data:image/svg+xml;base64,PHN2Zz4=">'

    assert _run(text, storage)[0] == text
    assert storage.puts == []


def test_r5_storage_failure_keeps_original_without_raising():
    storage = FakeStorage()

    def boom(_dataset_id, _object_name, _data):
        raise OSError("storage down")

    text = f'<img src="data:image/png;base64,{PNG_B64}">'

    result, manifest = _run(text, storage, put=boom)

    assert result == text
    assert manifest == {}


def test_r5_manifest_write_failure_does_not_raise():
    storage = FakeStorage()
    real_put = storage.put

    def flaky(dataset_id, object_name, data):
        if object_name.endswith(".index.json"):
            raise OSError("manifest write failed")
        real_put(dataset_id, object_name, data)

    text = f'<img src="data:image/png;base64,{PNG_B64}">'

    result, manifest = _run(text, storage, put=flaky)

    assert result == f"[[image:dataset/dataset-1/notes.md|image_ref={_ref(PNG_IMAGE_ID)}]]"
    assert manifest == {}
    assert storage.objects[("dataset-1", PNG_OBJECT)] == PNG_BYTES


def test_r6_budget_keeps_images_beyond_the_limit():
    storage = FakeStorage()
    raw_a = b"aaa"
    raw_b = b"bbb"
    b64_a = base64.b64encode(raw_a).decode("ascii")
    b64_b = base64.b64encode(raw_b).decode("ascii")
    text = f'<img src="data:image/png;base64,{b64_a}"><img src="data:image/png;base64,{b64_b}">'
    object_a = f"inline/{hashlib.sha256(raw_a).hexdigest()}.png"

    result, manifest = _run(text, storage, budget_bytes=len(b64_a))

    assert result == f'[[image:dataset/dataset-1/notes.md|image_ref={_ref(f"dataset-1-{object_a}")}]]<img src="data:image/png;base64,{b64_b}">'
    assert storage.puts == [("dataset-1", object_a), ("dataset-1", MANIFEST_NAME)]
    assert manifest["image_ids"] == [f"dataset-1-{object_a}"]


def test_invalid_image_reference_is_kept_verbatim():
    storage = FakeStorage()
    text = f'<img src="data:image/png;base64,{PNG_B64}">'

    result, manifest = _run(text, storage, image_ref_of=lambda _image_id: "not-a-hex-ref")

    assert result == text
    assert manifest == {}
    # 图片已按内容哈希写入（幂等资产），但引用非法时文本不替换、清单不登记。
    assert storage.puts == [("dataset-1", PNG_OBJECT)]


def test_object_name_guard_rejects_traversal_and_absolute_paths():
    assert _safe_object_name("inline/abc.png")
    assert not _safe_object_name("other/abc.png")
    assert not _safe_object_name("../secret")
    assert not _safe_object_name("inline/../escape.png")
    assert not _safe_object_name("/absolute/inline.png")


def test_load_manifest_missing_or_corrupt_returns_empty_list():
    storage = FakeStorage()

    assert load_manifest(dataset_id="dataset-1", document_id="doc-1", get=storage.get) == []

    storage.objects[("dataset-1", MANIFEST_NAME)] = b"not json"
    assert load_manifest(dataset_id="dataset-1", document_id="doc-1", get=storage.get) == []


def test_load_manifest_returns_registered_image_ids():
    storage = FakeStorage()
    storage.objects[("dataset-1", MANIFEST_NAME)] = json.dumps({"image_ids": ["a", "b"]}).encode("utf-8")

    assert load_manifest(dataset_id="dataset-1", document_id="doc-1", get=storage.get) == ["a", "b"]
