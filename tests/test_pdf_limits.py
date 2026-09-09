"""Resource-limit tests for attachment extraction."""
import os
import sys
import types

import pytest

from internpearls import ai_logic


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_extract_rejects_preloaded_incompatible_pypdf_before_parsing(tmp_path, monkeypatch):
    parser_called = False

    def unexpected_reader(_path):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("incompatible parser was used")

    incompatible = types.SimpleNamespace(
        __version__="6.16.2", PdfReader=unexpected_reader)
    monkeypatch.setitem(sys.modules, "pypdf", incompatible)

    with pytest.raises(ValueError, match=r"sample\.pdf.*pypdf 6\.16\.2.*6\.18\.0"):
        ai_logic.extract_attachment(
            os.path.join(FIXTURES, "sample.pdf"), os.fspath(tmp_path))

    assert parser_called is False


def test_extract_cancellation_stops_before_pdf_parser(tmp_path, monkeypatch):
    parser_called = False

    def unexpected_pypdf():
        nonlocal parser_called
        parser_called = True
        raise AssertionError("cancelled input reached the PDF parser")

    monkeypatch.setattr(ai_logic, "_pypdf", unexpected_pypdf)

    with pytest.raises(ValueError, match=r"^Attachment extraction cancelled$"):
        ai_logic.extract_attachment(
            os.path.join(FIXTURES, "sample.pdf"), os.fspath(tmp_path),
            cancel=lambda: True)

    assert parser_called is False


def test_extract_checks_cancellation_before_each_pdf_page(tmp_path, monkeypatch):
    page_visited = False

    def unexpected_text():
        nonlocal page_visited
        page_visited = True
        return ""

    page = types.SimpleNamespace(extract_text=unexpected_text, images=[])
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=[page]))
    cancel_calls = 0

    def cancel_on_page():
        nonlocal cancel_calls
        cancel_calls += 1
        return cancel_calls == 2

    with pytest.raises(ValueError, match=r"^Attachment extraction cancelled$"):
        ai_logic.extract_attachment(
            os.path.join(FIXTURES, "sample.pdf"), os.fspath(tmp_path),
            cancel=cancel_on_page)

    assert page_visited is False


def test_extract_checks_cancellation_before_each_pdf_image(tmp_path, monkeypatch):
    image_visited = False

    class OneImage:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            nonlocal image_visited
            image_visited = True
            return types.SimpleNamespace(name="figure.png", data=b"image")

    page = types.SimpleNamespace(extract_text=lambda: "", images=OneImage())
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=[page]))
    cancel_calls = 0

    def cancel_on_image():
        nonlocal cancel_calls
        cancel_calls += 1
        return cancel_calls == 3

    with pytest.raises(ValueError, match=r"^Attachment extraction cancelled$"):
        ai_logic.extract_attachment(
            os.path.join(FIXTURES, "sample.pdf"), os.fspath(tmp_path),
            cancel=cancel_on_image)

    assert image_visited is False


def test_extract_rejects_oversized_file_before_pdf_parser(tmp_path, monkeypatch):
    source = tmp_path / "oversized.pdf"
    with source.open("wb") as fh:
        fh.seek(ai_logic.MAX_ATTACHMENT_BYTES)
        fh.write(b"x")

    parser_called = False

    def unexpected_pypdf():
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input reached the PDF parser")

    monkeypatch.setattr(ai_logic, "_pypdf", unexpected_pypdf)

    with pytest.raises(ValueError, match=r"oversized\.pdf.*25 MiB"):
        ai_logic.extract_attachment(os.fspath(source), os.fspath(tmp_path))

    assert parser_called is False


def test_extract_rejects_pdf_over_page_limit_before_visiting_pages(tmp_path, monkeypatch):
    source = tmp_path / "too-many-pages.pdf"
    source.write_bytes(b"%PDF-synthetic")

    class TooManyPages:
        def __len__(self):
            return ai_logic.MAX_PDF_PAGES + 1

        def __iter__(self):
            raise AssertionError("over-limit pages were visited")

    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=TooManyPages()))

    with pytest.raises(ValueError, match=r"too-many-pages\.pdf.*100 page"):
        ai_logic.extract_attachment(os.fspath(source), os.fspath(tmp_path))


def test_extract_rejects_pdf_when_text_output_exceeds_limit(tmp_path, monkeypatch):
    source = tmp_path / "text-heavy.pdf"
    source.write_bytes(b"%PDF-synthetic")

    page = types.SimpleNamespace(
        extract_text=lambda: "x" * 500_001,
        images=[])
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=[page]))

    with pytest.raises(ValueError, match=r"text-heavy\.pdf.*500,000 character"):
        ai_logic.extract_attachment(os.fspath(source), os.fspath(tmp_path))


def test_extract_rejects_pdf_over_image_count_before_decoding(tmp_path, monkeypatch):
    source = tmp_path / "image-heavy.pdf"
    source.write_bytes(b"%PDF-synthetic")

    class TooManyImages:
        def __len__(self):
            return 51

        def __getitem__(self, index):
            raise AssertionError("over-limit image was decoded")

    page = types.SimpleNamespace(extract_text=lambda: "", images=TooManyImages())
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=[page]))

    with pytest.raises(ValueError, match=r"image-heavy\.pdf.*50 embedded image"):
        ai_logic.extract_attachment(os.fspath(source), os.fspath(tmp_path))


def test_extract_rejects_pdf_over_aggregate_image_bytes_before_write(tmp_path, monkeypatch):
    source = tmp_path / "image-bytes.pdf"
    source.write_bytes(b"%PDF-synthetic")

    class OversizedData:
        def __len__(self):
            return 25 * 1024 * 1024 + 1

    image = types.SimpleNamespace(name="figure.png", data=OversizedData())
    page = types.SimpleNamespace(extract_text=lambda: "", images=[image])
    pypdf = ai_logic._pypdf()
    monkeypatch.setattr(
        pypdf, "PdfReader", lambda _path: types.SimpleNamespace(pages=[page]))

    with pytest.raises(ValueError, match=r"image-bytes\.pdf.*25 MiB.*image"):
        ai_logic.extract_attachment(os.fspath(source), os.fspath(tmp_path))

    assert not (tmp_path / "image-bytes-p1-img0.png").exists()


def test_extract_applies_and_restores_pypdf_memory_bounds(tmp_path, monkeypatch):
    pypdf = ai_logic._pypdf()
    original_extract_text = pypdf._page.PageObject.extract_text
    observed = []

    def record_configuration(page, *args, **kwargs):
        config = pypdf.get_configuration()
        observed.append((config.maximum_declared_stream_length,
                         config.zlib_maximum_output_length,
                         config.image_maximum_buffer_size))
        return original_extract_text(page, *args, **kwargs)

    monkeypatch.setattr(pypdf._page.PageObject, "extract_text", record_configuration)
    before = pypdf.get_configuration()

    ai_logic.extract_attachment(
        os.path.join(FIXTURES, "sample.pdf"), os.fspath(tmp_path))

    assert observed == [(25 * 1024 * 1024,) * 3]
    assert pypdf.get_configuration() is before
