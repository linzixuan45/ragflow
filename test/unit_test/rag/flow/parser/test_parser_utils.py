import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_parser_utils(monkeypatch):
    repo_root = Path(__file__).resolve().parents[5]

    llm_service = ModuleType("api.db.services.llm_service")
    llm_service.LLMBundle = object
    monkeypatch.setitem(sys.modules, llm_service.__name__, llm_service)

    tenant_models = ModuleType("api.db.joint_services.tenant_model_service")
    tenant_models.get_model_config_from_provider_instance = lambda *_args: None
    monkeypatch.setitem(sys.modules, tenant_models.__name__, tenant_models)

    constants = ModuleType("common.constants")
    constants.LLMType = SimpleNamespace(IMAGE2TEXT="image2text")
    monkeypatch.setitem(sys.modules, constants.__name__, constants)

    figure_parser = ModuleType("deepdoc.parser.figure_parser")
    figure_parser.VisionFigureParser = object
    monkeypatch.setitem(sys.modules, figure_parser.__name__, figure_parser)

    rag_nlp = ModuleType("rag.nlp")
    rag_nlp.is_english = lambda *_args: False
    rag_nlp.random_choices = lambda values, **_kwargs: values
    rag_nlp.remove_contents_table = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, rag_nlp.__name__, rag_nlp)

    module_name = "test_parser_utils_unit_module"
    module_path = repo_root / "rag" / "flow" / "parser" / "utils.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("vlm_conf", [None, {}, {"llm_id": ""}])
def test_media_vision_enhancement_requires_explicit_model(monkeypatch, vlm_conf):
    utils = _load_parser_utils(monkeypatch)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("model lookup must not run without an explicit VLM")

    monkeypatch.setattr(utils, "get_model_config_from_provider_instance", fail_if_called)
    sections = [{"doc_type_kwd": "image", "image": object(), "text": "caption"}]

    result = utils.enhance_media_sections_with_vision(sections, "tenant", vlm_conf)

    assert result is sections
    assert sections[0]["text"] == "caption"


def test_media_vision_enhancement_uses_selected_model(monkeypatch):
    utils = _load_parser_utils(monkeypatch)
    selected = {}

    def get_model_config(tenant_id, model_type, llm_id):
        selected.update(tenant_id=tenant_id, model_type=model_type, llm_id=llm_id)
        return {"model": "vision-model"}

    class FakeBundle:
        def __init__(self, tenant_id, model_config):
            selected.update(bundle_tenant_id=tenant_id, model_config=model_config)

    class FakeVisionFigureParser:
        def __init__(self, vision_model, figures_data, context_size):
            selected.update(vision_model=vision_model, figures_data=figures_data, context_size=context_size)

        def __call__(self, callback=None):
            selected["callback"] = callback
            return [((object(), "generated description"), [(0, 0, 0, 0, 0)])]

    monkeypatch.setattr(utils, "get_model_config_from_provider_instance", get_model_config)
    monkeypatch.setattr(utils, "LLMBundle", FakeBundle)
    monkeypatch.setattr(utils, "VisionFigureParser", FakeVisionFigureParser)
    callback = object()
    sections = [{"doc_type_kwd": "image", "image": object(), "text": "caption"}]

    result = utils.enhance_media_sections_with_vision(
        sections,
        "tenant",
        {"llm_id": "vision@provider"},
        callback=callback,
    )

    assert result is sections
    assert sections[0]["text"] == "caption\ngenerated description"
    assert selected["tenant_id"] == "tenant"
    assert selected["llm_id"] == "vision@provider"
    assert selected["model_config"] == {"model": "vision-model"}
    assert selected["callback"] is callback
