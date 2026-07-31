"""Convert MinerU's pipeline return values into parser JSON blocks."""

from rag.utils.source_projection import source_projection_blocks


def mineru_pipeline_boxes(pdf_parser, lines, media):
    boxes = []
    for line in lines or []:
        if not isinstance(line, tuple) or len(line) < 3:
            continue

        text, layout_type, position_tag = line[0], line[1], line[2]
        box = {"text": text, "layout_type": layout_type or "text"}
        metadata = line[3] if len(line) > 3 and isinstance(line[3], dict) else {}
        if "level" in metadata:
            box["level"] = metadata["level"]
        positions = [[position[0][-1] + 1, *position[1:]] for position in pdf_parser.extract_positions(position_tag)]
        if positions:
            box["positions"] = positions
        boxes.append(box)

    boxes.extend(source_projection_blocks([], media or []))
    return boxes
