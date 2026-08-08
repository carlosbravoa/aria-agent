"""
PDF reading via file_access (offline — PDFs are generated inline, no fixtures).

Covers both backends: poppler's `pdftotext` when present, and the pypdf
fallback (forced by stubbing shutil.which), plus the failure paths that must
produce a clear message rather than binary mojibake or an empty string.
"""

import pytest

from aria.tools import file_access as fa


def _make_pdf(page_texts: list[str]) -> bytes:
    """Build a minimal but structurally valid PDF, one text line per page.

    Offsets in the xref table are computed as the file is assembled, so the
    result is a real PDF that both poppler and pypdf accept. Pass an empty
    string for a page to get a page with no text layer (the 'scanned' case).
    """
    n = len(page_texts)
    page_ids    = [3 + i for i in range(n)]
    content_ids = [3 + n + i for i in range(n)]
    font_id     = 3 + 2 * n

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % i for i in page_ids)
        + b"] /Count %d >>" % n,
    ]
    for i in range(n):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
            % (content_ids[i], font_id)
        )
    for text in page_texts:
        stream = (b"BT /F1 24 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
                  if text else b"")
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % idx + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


def _write_pdf(ws, name="doc.pdf", pages=("Hello Aria PDF",)):
    ws.mkdir(parents=True, exist_ok=True)
    p = ws / name
    p.write_bytes(_make_pdf(list(pages)))
    return p


# ── Extraction ────────────────────────────────────────────────────────────────

def test_read_pdf_extracts_text(minimal_env):
    p = _write_pdf(minimal_env)
    out = fa.execute({"action": "read", "path": str(p)})
    assert "Hello Aria PDF" in out
    assert out.startswith("[PDF:")


def test_pdf_page_count_reported(minimal_env):
    p = _write_pdf(minimal_env, pages=("Page one", "Page two", "Page three"))
    out = fa.execute({"action": "read", "path": str(p)})
    assert "3 pages" in out
    assert "Page one" in out and "Page three" in out


def test_single_page_is_singular(minimal_env):
    p = _write_pdf(minimal_env)
    assert "1 page]" in fa.execute({"action": "read", "path": str(p)})


def test_pdf_detected_by_magic_not_extension(minimal_env):
    """A PDF saved under a non-.pdf name still extracts — Telegram and email
    attachments routinely arrive mislabelled."""
    p = _write_pdf(minimal_env, name="report.dat")
    out = fa.execute({"action": "read", "path": str(p)})
    assert "Hello Aria PDF" in out


def test_pypdf_fallback_when_poppler_missing(minimal_env, monkeypatch):
    monkeypatch.setattr(fa.shutil, "which", lambda _: None)
    p = _write_pdf(minimal_env)
    out = fa.execute({"action": "read", "path": str(p)})
    assert "Hello Aria PDF" in out
    assert out.startswith("[PDF:")


# ── Failure paths ─────────────────────────────────────────────────────────────

def test_scanned_pdf_reports_no_text_layer(minimal_env):
    """A PDF with no text layer must say so, not return an empty string."""
    p = _write_pdf(minimal_env, name="scan.pdf", pages=("",))
    out = fa.execute({"action": "read", "path": str(p)})
    assert "no extractable text" in out
    assert "OCR is not supported" in out


def test_encrypted_pdf_reports_clearly(minimal_env):
    pypdf = pytest.importorskip("pypdf")
    src = _write_pdf(minimal_env, name="plain.pdf")
    enc = minimal_env / "locked.pdf"
    writer = pypdf.PdfWriter()
    for page in pypdf.PdfReader(str(src)).pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    with enc.open("wb") as fh:
        writer.write(fh)

    out = fa.execute({"action": "read", "path": str(enc)})
    assert "encrypted" in out.lower()


def test_corrupt_pdf_does_not_crash(minimal_env):
    minimal_env.mkdir(parents=True, exist_ok=True)
    p = minimal_env / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nnot really a pdf at all\n")
    out = fa.execute({"action": "read", "path": str(p)})
    assert out.startswith("[file_access]")


# ── Interaction with the existing read machinery ──────────────────────────────

def test_offset_limit_keeps_pdf_prefix(minimal_env):
    p = _write_pdf(minimal_env, pages=("Alpha", "Beta", "Gamma"))
    out = fa.execute({"action": "read", "path": str(p), "offset": 1, "limit": 1})
    assert out.startswith("[PDF:")
    assert "[lines 1" in out


def test_plain_text_read_is_unchanged(minimal_env):
    """Regression: non-PDF reads must not gain a prefix."""
    minimal_env.mkdir(parents=True, exist_ok=True)
    p = minimal_env / "notes.txt"
    p.write_text("just text\n")
    assert fa.execute({"action": "read", "path": str(p)}) == "just text\n"


def test_pdf_outside_allowlist_still_denied(minimal_env, monkeypatch, tmp_path):
    """PDF handling runs after the allow-list check, not around it."""
    outside = tmp_path / "elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    p = outside / "secret.pdf"
    p.write_bytes(_make_pdf(["classified"]))
    out = fa.execute({"action": "read", "path": str(p)})
    assert "classified" not in out
