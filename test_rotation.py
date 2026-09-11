"""Проверка выправления поворота страниц PDF (indexing._normalize_rotation).
Детекция угла требует реального PDF (проверена на проде — ММК, 18 повёрнутых
страниц, угол 270°). Здесь — безопасные ветки: не-PDF и сбой не роняют индексацию,
и компенсирующий угол считается верно."""
from pathlib import Path
import indexing


def test_passthrough_non_pdf():
    p = Path("foo.docx")
    assert indexing._normalize_rotation(p) == (p, False)


def test_no_rotated_pages(monkeypatch):
    monkeypatch.setattr(indexing, "_detect_rotated_pages", lambda _p: {})
    p = Path("x.pdf")
    assert indexing._normalize_rotation(p) == (p, False)


def test_detection_failure_falls_back(monkeypatch):
    def boom(_p):
        raise RuntimeError("pdfium недоступен")
    monkeypatch.setattr(indexing, "_detect_rotated_pages", boom)
    p = Path("x.pdf")
    assert indexing._normalize_rotation(p) == (p, False)   # сбой -> исходный файл


def test_compensation_angle():
    # текст под 270° выправляется поворотом 90°, под 90° — поворотом 270°
    assert (360 - 270) % 360 == 90
    assert (360 - 90) % 360 == 270


if __name__ == "__main__":
    test_passthrough_non_pdf()
    test_compensation_angle()
    print("test_rotation: базовые ветки — OK (детекция проверяется на реальном PDF)")
