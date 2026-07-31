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

import ast
from pathlib import Path

from deepdoc.vision import ocr


class _Session:
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return self._providers


def _model_dir(tmp_path):
    (tmp_path / "det.onnx").write_bytes(b"model")
    ocr.loaded_models.clear()
    return str(tmp_path)


def test_load_model_uses_onnx_cuda_provider(monkeypatch, tmp_path):
    calls = []
    session = _Session(["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(ocr, "get_onnx_cuda_device_count", lambda: 1)
    monkeypatch.setattr(ocr.ort, "InferenceSession", lambda *args, **kwargs: calls.append((args, kwargs)) or session)

    loaded_session, _ = ocr.load_model(_model_dir(tmp_path), "det")

    assert loaded_session is session
    assert calls[0][1]["providers"] == ["CUDAExecutionProvider"]
    assert calls[0][1]["provider_options"][0]["device_id"] == 0


def test_load_model_falls_back_to_cpu_when_cuda_initialization_fails(monkeypatch, tmp_path):
    providers = []
    cpu_session = _Session(["CPUExecutionProvider"])
    monkeypatch.setattr(ocr, "get_onnx_cuda_device_count", lambda: 1)

    def create_session(*_args, **kwargs):
        providers.append(kwargs["providers"])
        if kwargs["providers"] == ["CUDAExecutionProvider"]:
            raise RuntimeError("CUDA unavailable")
        return cpu_session

    monkeypatch.setattr(ocr.ort, "InferenceSession", create_session)

    loaded_session, _ = ocr.load_model(_model_dir(tmp_path), "det")

    assert loaded_session is cpu_session
    assert providers == [["CUDAExecutionProvider"], ["CPUExecutionProvider"]]


def test_load_model_falls_back_when_onnx_silently_rejects_cuda(monkeypatch, tmp_path):
    providers = []
    cuda_fallback_session = _Session(["CPUExecutionProvider"])
    cpu_session = _Session(["CPUExecutionProvider"])
    monkeypatch.setattr(ocr, "get_onnx_cuda_device_count", lambda: 1)

    def create_session(*_args, **kwargs):
        providers.append(kwargs["providers"])
        return cuda_fallback_session if len(providers) == 1 else cpu_session

    monkeypatch.setattr(ocr.ort, "InferenceSession", create_session)

    loaded_session, _ = ocr.load_model(_model_dir(tmp_path), "det")

    assert loaded_session is cpu_session
    assert providers == [["CUDAExecutionProvider"], ["CPUExecutionProvider"]]


def test_deepdoc_ocr_entrypoint_does_not_import_torch():
    source = Path(__file__).parents[3] / "deepdoc" / "vision" / "t_ocr.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert "torch" not in imported_roots
