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

import sys
from types import SimpleNamespace

from common.onnx_utils import get_onnx_cuda_device_count


def _set_onnxruntime(monkeypatch, providers):
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(get_available_providers=lambda: providers))


def test_cpu_mode_disables_onnx_cuda(monkeypatch):
    monkeypatch.setenv("DEVICE", "cpu")
    _set_onnxruntime(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])

    assert get_onnx_cuda_device_count() == 0


def test_missing_cuda_provider_disables_onnx_cuda(monkeypatch):
    monkeypatch.setenv("DEVICE", "gpu")
    _set_onnxruntime(monkeypatch, ["CPUExecutionProvider"])

    assert get_onnx_cuda_device_count() == 0


def test_gpu_mode_defaults_to_one_cuda_device(monkeypatch):
    monkeypatch.setenv("DEVICE", "gpu")
    monkeypatch.delenv("PARALLEL_DEVICES", raising=False)
    _set_onnxruntime(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])

    assert get_onnx_cuda_device_count() == 1


def test_explicit_parallel_devices_controls_cuda_device_count(monkeypatch):
    monkeypatch.setenv("DEVICE", "gpu")
    monkeypatch.setenv("PARALLEL_DEVICES", "3")
    _set_onnxruntime(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])

    assert get_onnx_cuda_device_count() == 3


def test_invalid_parallel_devices_falls_back_to_one(monkeypatch):
    monkeypatch.setenv("DEVICE", "gpu")
    monkeypatch.setenv("PARALLEL_DEVICES", "invalid")
    _set_onnxruntime(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])

    assert get_onnx_cuda_device_count() == 1
