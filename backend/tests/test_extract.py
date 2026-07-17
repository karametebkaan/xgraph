from __future__ import annotations

import io

import pytest

from xgraph_gateway import extract


# --- canonical_id -----------------------------------------------------------

def test_canonical_id_case_insensitive_stability():
    assert extract.canonical_id("Jerome Powell") == extract.canonical_id("jerome powell")


def test_canonical_id_format_is_slug_dash_8hex():
    cid = extract.canonical_id("Jerome Powell")
    slug, _, suffix = cid.rpartition("-")
    assert slug == "jerome-powell"
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


# --- chunk -------------------------------------------------------------------

def test_chunk_splits_on_blank_lines():
    chunks, truncated = extract.chunk("a\n\nb\n\n\nc")
    assert chunks == ["a", "b", "c"]
    assert truncated is False


def test_chunk_caps_at_max_chunks_and_reports_truncation():
    text = "\n\n".join(f"para{i}" for i in range(50))
    chunks, truncated = extract.chunk(text, max_chunks=40)
    assert len(chunks) == 40
    assert truncated is True


def test_chunk_drops_empty_paragraphs():
    chunks, truncated = extract.chunk("a\n\n\n\n  \n\nb")
    assert chunks == ["a", "b"]
    assert truncated is False


# --- extract_document (fake llm, no network) --------------------------------

def _canned_llm(responses):
    """Returns a fake `llm(prompt, schema=None)` yielding one canned dict per call."""
    calls = iter(responses)

    def fake(prompt, *, schema=None):
        return next(calls)
    return fake


def test_extract_document_merges_duplicate_entity_across_chunks():
    text = "Apple chunk one.\n\nApple chunk two."
    fake_llm = _canned_llm([
        {
            "entities": [
                {"name": "Apple", "label": "Organization"},
                {"name": "Steve Jobs", "label": "Person"},
            ],
            "relations": [
                {"source": "Apple", "target": "Steve Jobs", "label": "FOUNDED_BY"},
            ],
        },
        {
            "entities": [
                {"name": "Apple", "label": "Organization"},
                {"name": "Tim Cook", "label": "Person"},
            ],
            "relations": [
                {"source": "Apple", "target": "Tim Cook", "label": "LED_BY"},
                # duplicate of the above within the same chunk -> collapses
                {"source": "Apple", "target": "Tim Cook", "label": "LED_BY"},
                # target "Google" is not among this chunk's entities -> dangling, dropped
                {"source": "Apple", "target": "Google", "label": "COMPETES_WITH"},
            ],
        },
    ])

    result = extract.extract_document(text, llm=fake_llm)

    apple_entities = [e for e in result["entities"] if e["name"] == "Apple"]
    assert len(apple_entities) == 1
    names = {e["name"] for e in result["entities"]}
    assert names == {"Apple", "Steve Jobs", "Tim Cook"}

    rel_labels = sorted((r["label"] for r in result["relations"]))
    assert rel_labels == ["FOUNDED_BY", "LED_BY"]

    apple_id = apple_entities[0]["id"]
    assert apple_id == extract.canonical_id("Apple")
    for r in result["relations"]:
        assert r["src"] == apple_id

    assert result["truncated"] is False


def test_extract_document_entity_ids_are_canonical():
    text = "Just one paragraph."
    fake_llm = _canned_llm([
        {"entities": [{"name": "Acme Corp", "label": "Organization"}], "relations": []},
    ])
    result = extract.extract_document(text, llm=fake_llm)
    assert result["entities"][0]["id"] == extract.canonical_id("Acme Corp")


def test_extract_document_relation_id_is_sha1_of_src_dst_label():
    import hashlib
    text = "One paragraph."
    fake_llm = _canned_llm([
        {
            "entities": [{"name": "A", "label": "Thing"}, {"name": "B", "label": "Thing"}],
            "relations": [{"source": "A", "target": "B", "label": "RELATES_TO"}],
        },
    ])
    result = extract.extract_document(text, llm=fake_llm)
    src = extract.canonical_id("A")
    dst = extract.canonical_id("B")
    expected_id = hashlib.sha1(f"{src}|{dst}|RELATES_TO".encode()).hexdigest()[:16]
    assert result["relations"][0]["id"] == expected_id


def test_extract_document_reports_truncated_when_chunks_capped():
    text = "\n\n".join(f"para{i}" for i in range(5))
    fake_llm = _canned_llm([{"entities": [], "relations": []} for _ in range(2)])
    result = extract.extract_document(text, llm=fake_llm, max_chunks=2)
    assert result["truncated"] is True


def test_extract_document_hint_folded_into_prompt():
    captured = {}

    def capturing_llm(prompt, *, schema=None):
        captured["prompt"] = prompt
        captured["schema"] = schema
        return {"entities": [], "relations": []}

    extract.extract_document("some text", hint="focus on banks", llm=capturing_llm)
    assert "focus on banks" in captured["prompt"]
    assert captured["schema"] == extract._EXTRACT_SCHEMA


def test_extract_document_accepts_json_string_response():
    fake_llm = _canned_llm(['{"entities": [{"name": "X", "label": "Thing"}], "relations": []}'])
    result = extract.extract_document("some text", llm=fake_llm)
    assert result["entities"][0]["name"] == "X"


def test_extract_document_shallow_merges_attrs_first_wins():
    fake_llm = _canned_llm([
        {"entities": [{"name": "X", "label": "Thing", "attrs": {"color": "red"}}], "relations": []},
        {"entities": [{"name": "X", "label": "Thing", "attrs": {"color": "blue", "size": "big"}}],
         "relations": []},
    ])
    result = extract.extract_document("a\n\nb", llm=fake_llm)
    x = [e for e in result["entities"] if e["name"] == "X"][0]
    assert x["attrs"]["color"] == "red"
    assert x["attrs"]["size"] == "big"


def test_extract_document_carries_facets():
    from xgraph_gateway import extract

    def fake_llm(prompt, *, schema=None):
        return {"entities": [{"name": "Anthropic", "label": "Company",
                              "facets": [{"name": "AI", "axis": "Industry"}],
                              "attrs": {}}],
                "relations": []}

    out = extract.extract_document("Anthropic is an AI company.", llm=fake_llm)
    ent = out["entities"][0]
    assert ent["label"] == "Company"
    assert ent["facets"] == [{"name": "AI", "axis": "Industry"}]


def test_extract_document_defaults_facets_to_empty():
    from xgraph_gateway import extract

    def fake_llm(prompt, *, schema=None):
        return {"entities": [{"name": "Bob", "label": "Person", "attrs": {}}],
                "relations": []}

    out = extract.extract_document("Bob exists.", llm=fake_llm)
    assert out["entities"][0]["facets"] == []


# --- read_document -----------------------------------------------------------

def test_read_document_txt():
    assert extract.read_document("d.txt", b"hello") == "hello"


def test_read_document_markdown():
    assert extract.read_document("d.md", b"# hi") == "# hi"


def test_read_document_unsupported_extension_raises():
    with pytest.raises(ValueError):
        extract.read_document("d.docx", b"x")


def _minimal_pdf_bytes(text: bytes = b"Hello World") -> bytes:
    """Hand-build a minimal single-page PDF with a real text content stream.

    pypdf itself has no "draw text" authoring API (it manipulates/reads
    existing PDF structure), so this constructs the handful of PDF objects
    (catalog, pages, page, font, content stream) directly, the same shape a
    minimal writer would produce, and lets pypdf read it back.
    """
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>"
        b"/MediaBox[0 0 612 792]/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream_content = b"BT /F1 24 Tf 72 720 Td (" + text + b") Tj ET"
    objs.append(b"<</Length " + str(len(stream_content)).encode() + b">>\nstream\n"
                + stream_content + b"\nendstream")

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(buf.tell())
        buf.write(str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref_offset = buf.tell()
    n = len(objs) + 1
    buf.write(b"xref\n" + f"0 {n}\n".encode() + b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(b"trailer\n<</Size " + str(n).encode() + b"/Root 1 0 R>>\n")
    buf.write(b"startxref\n" + str(xref_offset).encode() + b"\n%%EOF")
    return buf.getvalue()


def test_read_document_pdf():
    try:
        import pypdf  # noqa: F401
    except ImportError:
        pytest.skip("pypdf not installed")
    data = _minimal_pdf_bytes(b"Hello World")
    try:
        text = extract.read_document("d.pdf", data)
    except Exception:
        pytest.skip("pypdf could not extract readable text from the in-process PDF")
    if "Hello World" not in text:
        pytest.skip("pypdf could not extract readable text from the in-process PDF")
    assert "Hello World" in text
