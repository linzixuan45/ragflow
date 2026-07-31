from PIL import Image

from rag.utils.mineru_adapter import mineru_pipeline_boxes


class _Parser:
    page_from = 0

    @staticmethod
    def extract_positions(_value):
        return [([0], 10.0, 20.0, 30.0, 40.0)]

    @staticmethod
    def crop(_value, _zoom):
        raise AssertionError("MinerU text lines must not be converted into images")


def test_mineru_pipeline_boxes_preserve_native_media_return_value():
    image = Image.new("RGB", (4, 3), "white")

    boxes = mineru_pipeline_boxes(
        _Parser(),
        [("Body", "text", "position-tag")],
        [
            ((image, "Figure 1"), [[0, 50.0, 150.0, 60.0, 160.0]]),
            ((None, "<table><tr><td>value</td></tr></table>"), [[1, 10.0, 100.0, 20.0, 80.0]]),
        ],
    )

    assert [box["layout_type"] for box in boxes] == ["text", "figure", "table"]
    assert "image" not in boxes[0]
    assert boxes[1]["image"] is image
    assert boxes[1]["positions"] == [[1, 50.0, 150.0, 60.0, 160.0]]
    assert boxes[2]["positions"] == [[2, 10.0, 100.0, 20.0, 80.0]]
