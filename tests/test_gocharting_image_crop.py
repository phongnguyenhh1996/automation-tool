from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from automation_tool.gocharting_image_crop import (
    GOCHARTING_IMAGE_WIDTH_THIRDS,
    crop_png_width_thirds,
    footprint_crop_part_path,
    gocharting_detail_openai_image_paths,
    is_gocharting_detail_png,
)


def _write_rgb_png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (255, 0, 0)).save(path, format="PNG")


def test_is_gocharting_detail_png() -> None:
    src = Path("a_gocharting_GC_5m_detail_zoom.png")
    crop = Path("a_gocharting_GC_5m_detail_zoom_part2.png")
    ov = Path("a_gocharting_GC_5m.png")
    assert is_gocharting_detail_png(src)
    assert not is_gocharting_detail_png(crop)
    assert not is_gocharting_detail_png(ov)


def test_footprint_crop_part_path() -> None:
    source = Path("/tmp/charts/20260623_120000_gocharting_GC_5m_detail_zoom.png")
    assert footprint_crop_part_path(source, 1) == Path(
        "/tmp/charts/20260623_120000_gocharting_GC_5m_detail_zoom_part1.png"
    )


def test_crop_png_width_thirds(tmp_path: Path) -> None:
    source = tmp_path / "sample_detail_zoom.png"
    _write_rgb_png(source, 300, 100)

    crops = crop_png_width_thirds(source)
    assert len(crops) == GOCHARTING_IMAGE_WIDTH_THIRDS
    widths = []
    for p in crops:
        with Image.open(p) as img:
            widths.append(img.size[0])
    assert widths == [100, 100, 100]


def test_gocharting_detail_openai_image_paths(tmp_path: Path) -> None:
    detail = tmp_path / "x_gocharting_GC_5m_detail_zoom.png"
    overview = tmp_path / "x_gocharting_GC_5m.png"
    _write_rgb_png(detail, 300, 100)
    _write_rgb_png(overview, 300, 100)

    panels = gocharting_detail_openai_image_paths(detail)
    assert len(panels) == 3
    assert gocharting_detail_openai_image_paths(overview) == [overview]


def test_crop_png_width_thirds_invalid_part() -> None:
    with pytest.raises(ValueError):
        footprint_crop_part_path(Path("a.png"), 0)
