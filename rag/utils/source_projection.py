"""Lightweight construction of parser-owned PDF source blocks."""

from __future__ import annotations

import re


def _offset_position_tag(value, page_offset):
    if not isinstance(value, str) or page_offset <= 0:
        return value

    def replace(match):
        pages = "-".join(str(int(page) + page_offset) for page in match.group(1).split("-"))
        return f"@@{pages}\t"

    return re.sub(r"@@([0-9-]+)\t", replace, value)


def _offset_positions(positions, page_offset):
    if page_offset <= 0 or not isinstance(positions, (list, tuple)):
        return positions
    return [
        [int(position[0]) + page_offset, *position[1:]]
        if isinstance(position, (list, tuple)) and position and isinstance(position[0], (int, float))
        else position
        for position in positions
    ]


def source_projection_blocks(sections, tables, *, section_page_offset=0):
    blocks = []
    for source_index, section in enumerate(sections or []):
        if isinstance(section, dict):
            text = section.get("text", "")
            layout_type = section.get("layout_type", "text")
            positions = section.get("positions", section.get("position_tag", []))
            level = section.get("level")
        elif isinstance(section, (tuple, list)):
            text = section[0] if section else ""
            layout_type = section[1] if len(section) > 2 and isinstance(section[1], str) else "text"
            positions = section[-1] if len(section) > 1 else []
            level = None
        else:
            text, layout_type, positions = section, "text", []
            level = None
        if isinstance(text, str) and text.strip():
            block = {"text": text, "layout_type": layout_type}
            if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 6:
                block["level"] = level
            if isinstance(positions, str):
                block["position_tag"] = _offset_position_tag(positions, section_page_offset)
            else:
                block["positions"] = _offset_positions(positions or [], section_page_offset)
            block["source_ref"] = f"section:{source_index}"
            blocks.append(block)
    for source_index, table in enumerate(tables or []):
        if not isinstance(table, (tuple, list)) or len(table) != 2:
            continue
        data, positions = table
        if not isinstance(data, (tuple, list)) or len(data) != 2:
            continue
        image, rows = data
        text = rows if isinstance(rows, str) else "\n".join(rows or [])
        if not text.strip() and image is None:
            continue
        layout_type = "figure" if image is not None and "<tr>" not in text else "table"
        block = {
            "text": text,
            "layout_type": layout_type,
            "positions": _offset_positions(positions or [], 1),
            "source_ref": f"table:{source_index}",
        }
        if image is not None:
            block["image"] = image
        blocks.append(block)

    def order(item):
        positions = item.get("positions") or []
        if positions and isinstance(positions[0], (tuple, list)) and len(positions[0]) >= 4:
            position = positions[0]
            return (float(position[0]), float(position[3]), float(position[1]))
        tag = item.get("position_tag")
        if isinstance(tag, str):
            match = re.search(r"@@(\d+)\t([\d.]+)\t[\d.]+\t([\d.]+)", tag)
            if match:
                return (float(match.group(1)), float(match.group(3)), float(match.group(2)))
        return (float("inf"), float("inf"), float("inf"))

    blocks.sort(key=order)
    return blocks
