from __future__ import annotations

import re
from pathlib import Path

GOCHARTING_IMAGE_WIDTH_THIRDS = 3
_GOCHARTING_DETAIL_PART_RE = re.compile(r"_part(\d+)$")


def is_gocharting_detail_png(path: Path) -> bool:
    """True for source detail footprint PNGs (not overview, not crop panels)."""
    if path.suffix.lower() != ".png" or "_gocharting_" not in path.name:
        return False
    if "_detail_" not in path.name:
        return False
    return _GOCHARTING_DETAIL_PART_RE.search(path.stem) is None


def footprint_crop_part_path(source: Path, part: int) -> Path:
    """``{stem}_part{N}.png`` sibling of the source detail PNG."""
    if part < 1 or part > GOCHARTING_IMAGE_WIDTH_THIRDS:
        raise ValueError(
            f"crop part must be 1..{GOCHARTING_IMAGE_WIDTH_THIRDS}, got {part}"
        )
    return source.with_name(f"{source.stem}_part{part}{source.suffix}")


def gocharting_detail_crop_part_index(path: Path) -> int | None:
    """Return 1..3 when ``path`` is a GoCharting detail crop panel."""
    if not path.name.endswith(".png") or "_gocharting_" not in path.name:
        return None
    m = _GOCHARTING_DETAIL_PART_RE.search(path.stem)
    if not m:
        return None
    try:
        part = int(m.group(1))
    except ValueError:
        return None
    if 1 <= part <= GOCHARTING_IMAGE_WIDTH_THIRDS:
        return part
    return None


def _width_third_crop_boxes(width: int, height: int) -> list[tuple[int, int, int, int]]:
    third = width // GOCHARTING_IMAGE_WIDTH_THIRDS
    return [
        (0, 0, third, height),
        (third, 0, 2 * third, height),
        (2 * third, 0, width, height),
    ]


def _crops_are_fresh(source: Path, crop_paths: list[Path]) -> bool:
    if not all(p.is_file() for p in crop_paths):
        return False
    try:
        src_mtime = source.stat().st_mtime
    except OSError:
        return False
    return all(p.stat().st_mtime >= src_mtime for p in crop_paths)


def crop_png_width_thirds(source: Path) -> list[Path]:
    """
    Crop a detail PNG into 3 horizontal panels (left, center, right).

    Writes ``{stem}_part1..3.png`` next to the source unless crops are already fresh.
    """
    from PIL import Image

    if not source.is_file():
        raise FileNotFoundError(f"Detail PNG not found: {source}")

    crop_paths = [
        footprint_crop_part_path(source, part)
        for part in range(1, GOCHARTING_IMAGE_WIDTH_THIRDS + 1)
    ]
    if _crops_are_fresh(source, crop_paths):
        return crop_paths

    with Image.open(source) as img:
        rgb = img.convert("RGB")
        boxes = _width_third_crop_boxes(*rgb.size)
        for part, box in enumerate(boxes, start=1):
            panel = rgb.crop(box)
            out = crop_paths[part - 1]
            out.parent.mkdir(parents=True, exist_ok=True)
            panel.save(out, format="PNG")
    return crop_paths


def gocharting_detail_openai_image_paths(
    path: Path, *, crop_width_thirds: bool = True
) -> list[Path]:
    """Detail footprint PNGs → 3 crop panels when enabled; other images unchanged."""
    if crop_width_thirds and is_gocharting_detail_png(path):
        return crop_png_width_thirds(path)
    return [path]


def resolve_gocharting_detail_crop_width_thirds(
    gocharting_cfg: dict | None = None,
) -> bool:
    """Read ``detail_chart.crop_width_thirds`` from config (default True)."""
    if gocharting_cfg is None:
        from automation_tool.config import default_gocharting_config_path

        path = default_gocharting_config_path()
        if not path.is_file():
            return True
        from automation_tool.gocharting_capture import load_gocharting_yaml

        gocharting_cfg = load_gocharting_yaml(path)
    from automation_tool.gocharting_capture import gocharting_detail_crop_width_thirds

    return gocharting_detail_crop_width_thirds(gocharting_cfg)
