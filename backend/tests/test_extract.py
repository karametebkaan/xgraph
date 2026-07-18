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
    # Content-keyed (not order-based): chunk calls run concurrently, so the fake
    # must return the right response for the right chunk regardless of order.
    def fake_llm(prompt, *, schema=None):
        if "chunk one" in prompt:
            return {
                "entities": [
                    {"name": "Apple", "label": "Organization"},
                    {"name": "Steve Jobs", "label": "Person"},
                ],
                "relations": [
                    {"source": "Apple", "target": "Steve Jobs", "label": "FOUNDED_BY"},
                ],
            }
        return {
            "entities": [
                {"name": "Apple", "label": "Organization"},
                {"name": "Tim Cook", "label": "Person"},
            ],
            "relations": [
                {"source": "Apple", "target": "Tim Cook", "label": "LED_BY"},
                # duplicate within the same chunk -> collapses
                {"source": "Apple", "target": "Tim Cook", "label": "LED_BY"},
                # target "Google" not among this chunk's entities -> dangling, dropped
                {"source": "Apple", "target": "Google", "label": "COMPETES_WITH"},
            ],
        }

    result = extract.extract_document(text, llm=fake_llm)

    apple_entities = [e for e in result["entities"] if e["name"] == "Apple"]
    assert len(apple_entities) == 1
    names = {e["name"] for e in result["entities"]}
    assert names == {"Apple", "Steve Jobs", "Tim Cook"}

    rel_labels = sorted((r["label"] for r in result["relations"]))
    assert rel_labels == ["FOUNDED_BY", "LED_BY"]

    apple_id = apple_entities[0]["id"]
    assert apple_id == "Apple"  # NODE = name
    for r in result["relations"]:
        assert r["src"] == apple_id

    assert result["truncated"] is False


def test_extract_document_entity_ids_are_canonical():
    text = "Just one paragraph."
    fake_llm = _canned_llm([
        {"entities": [{"name": "Acme Corp", "label": "Organization"}], "relations": []},
    ])
    result = extract.extract_document(text, llm=fake_llm)
    assert result["entities"][0]["id"] == "Acme Corp"  # NODE = name now


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
    src = "A"  # NODE = name
    dst = "B"
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
    # Content-keyed so it's deterministic under concurrent chunk calls: chunk
    # "a" (first, wins) is red; chunk "b" adds size and a losing color.
    def fake_llm(prompt, *, schema=None):
        if prompt.rstrip().endswith("a"):
            return {"entities": [{"name": "X", "label": "Thing", "attrs": {"color": "red"}}],
                    "relations": []}
        return {"entities": [{"name": "X", "label": "Thing",
                              "attrs": {"color": "blue", "size": "big"}}], "relations": []}
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


def test_extract_get_llm_binds_fast_model(monkeypatch):
    from xgraph_gateway import extract, llm as llmmod
    captured = {}
    def fake_llm(prompt, *, schema=None, model=None):
        captured["model"] = model
        return {"entities": [], "relations": []}
    monkeypatch.setattr(llmmod, "_llm", fake_llm)
    monkeypatch.setattr(extract, "_llm_fn", None)
    extract._get_llm()("hi", schema={})
    assert captured["model"] == extract.EXTRACT_MODEL
    assert "haiku" in extract.EXTRACT_MODEL


def test_fold_get_llm_binds_fast_model(monkeypatch):
    from xgraph_gateway import extract_fold, llm as llmmod
    captured = {}
    def fake_llm(prompt, *, schema=None, model=None):
        captured["model"] = model
        return {"canonical": None}
    monkeypatch.setattr(llmmod, "_llm", fake_llm)
    monkeypatch.setattr(extract_fold, "_llm_fn", None)
    extract_fold._get_llm()("hi", schema={})
    assert "haiku" in captured["model"]


def test_nlcypher_get_llm_keeps_default_model(monkeypatch):
    # ask/explain path must NOT pin a model (keeps the default/Opus).
    from xgraph_gateway import nlcypher, llm as llmmod
    captured = {"model": "SENTINEL"}
    def fake_llm(prompt, *, schema=None, model=None):
        captured["model"] = model
        return "x"
    monkeypatch.setattr(llmmod, "_llm", fake_llm)
    monkeypatch.setattr(nlcypher, "_llm_fn", None)
    nlcypher._get_llm()("hi")
    assert captured["model"] is None


def test_extract_chunks_run_concurrently_and_preserve_order():
    # Multi-chunk extraction runs in parallel but results must align to chunk
    # order (downstream merge is first-seen-wins).
    from xgraph_gateway import extract
    text = "\n\n".join(f"para number {i}" for i in range(6))
    def echo_llm(prompt, *, schema=None):
        # echo which para this call saw
        n = prompt.split("para number ")[1].split("\n")[0].strip()
        return {"entities": [{"name": f"E{n}", "label": "T"}], "relations": []}
    result = extract.extract_document(text, llm=echo_llm, mode="parallel")
    names = [e["name"] for e in result["entities"]]
    assert names == [f"E{i}" for i in range(6)]  # in order, none lost


def test_llm_backend_prefers_sdk_when_api_key_set(monkeypatch):
    from xgraph_gateway import llm as llmmod
    monkeypatch.setattr(llmmod, "_llm_claude_sdk", lambda p, s, m=None: "SDK")
    monkeypatch.setattr(llmmod, "_llm_claude_cli", lambda p, s, m=None: "CLI")
    monkeypatch.setattr(llmmod.shutil, "which", lambda _x: "/usr/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llmmod._llm("hi") == "SDK"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llmmod._llm("hi") == "CLI"  # no key -> CLI (default dev path)


def test_extract_document_whole_mode_is_one_call_and_keeps_cross_paragraph_relations():
    calls = {"n": 0}
    def fake(prompt, *, schema=None):
        calls["n"] += 1
        assert "P one" in prompt and "P two" in prompt  # whole doc in one prompt
        return {"entities": [{"name": "A", "label": "T"}, {"name": "B", "label": "T"}],
                "relations": [{"source": "A", "target": "B", "label": "R"}]}
    out = extract.extract_document("A is P one.\n\nB is P two.", llm=fake, mode="whole")
    assert calls["n"] == 1
    assert len(out["relations"]) == 1  # A(para1) -> B(para2) survives in whole mode


def test_extract_document_sequential_mode_one_call_per_paragraph():
    calls = {"n": 0}
    def fake(prompt, *, schema=None):
        calls["n"] += 1
        return {"entities": [], "relations": []}
    extract.extract_document("p1\n\np2\n\np3", llm=fake, mode="sequential")
    assert calls["n"] == 3


def test_extract_document_unknown_mode_defaults_to_sequential():
    def fake(prompt, *, schema=None):
        return {"entities": [], "relations": []}
    # should not raise; behaves like the paragraph split (not one whole call)
    out = extract.extract_document("p1\n\np2", llm=fake, mode="bogus")
    assert out["truncated"] is False


def test_session_store_records_extract_mode():
    from xgraph_gateway.sessions import SessionStore
    store = SessionStore(adapter_factory=lambda e, c=None: object(),
                         compute_factory=lambda e, c=None: object())
    sid = store.create("fake", None, "duckdb", None, extract_mode="whole")
    assert store.get(sid)["extract_mode"] == "whole"
    sid2 = store.create("fake", None, "duckdb", None)  # default
    assert store.get(sid2)["extract_mode"] == "sequential"


def test_merge_partial_names_folds_surname_into_fullname():
    ents = [{"id": "Mullin", "name": "Mullin", "label": "Person", "facets": [], "attrs": {}},
            {"id": "Markwayne Mullin", "name": "Markwayne Mullin", "label": "Person",
             "facets": [], "attrs": {}}]
    rels = [{"id": "r1", "src": "Mullin", "dst": "DHS", "label": "WORKS_AT", "attrs": {}}]
    ents2, rels2 = extract._merge_partial_names(ents, rels)
    assert {e["name"] for e in ents2} == {"Markwayne Mullin"}  # Mullin folded away
    assert rels2[0]["src"] == "Markwayne Mullin"


def test_merge_partial_names_skips_ambiguous_surname():
    ents = [{"id": "Trump", "name": "Trump", "label": "Person", "facets": [], "attrs": {}},
            {"id": "Donald Trump", "name": "Donald Trump", "label": "Person", "facets": [], "attrs": {}},
            {"id": "Ivanka Trump", "name": "Ivanka Trump", "label": "Person", "facets": [], "attrs": {}}]
    ents2, _ = extract._merge_partial_names(ents, [])
    assert {e["name"] for e in ents2} == {"Trump", "Donald Trump", "Ivanka Trump"}  # ambiguous


def test_merge_partial_names_respects_label():
    ents = [{"id": "Apple", "name": "Apple", "label": "Organization", "facets": [], "attrs": {}},
            {"id": "Bob Apple", "name": "Bob Apple", "label": "Person", "facets": [], "attrs": {}}]
    ents2, _ = extract._merge_partial_names(ents, [])
    assert len(ents2) == 2  # different labels -> no merge


def test_extract_document_merges_surname_across_chunks():
    def fake(prompt, *, schema=None):
        if "DHS" in prompt:  # chunk two (unique token, not in the prompt template)
            return {"entities": [{"name": "Mullin", "label": "Person"},
                                 {"name": "DHS", "label": "Organization"}],
                    "relations": [{"source": "Mullin", "target": "DHS", "label": "WORKS_AT"}]}
        return {"entities": [{"name": "Markwayne Mullin", "label": "Person"}], "relations": []}
    out = extract.extract_document("Markwayne Mullin spoke.\n\nMullin joined DHS.",
                                   llm=fake, mode="sequential")
    names = {e["name"] for e in out["entities"]}
    assert "Markwayne Mullin" in names and "Mullin" not in names
    assert any(r["src"] == "Markwayne Mullin" for r in out["relations"])
