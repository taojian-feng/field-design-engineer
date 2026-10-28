"""Drawing search and verification: normalization, sources, and agreement with the synthetic answer key."""

import json

import pytest

from fde_agent.drawings import (DrawingIndex, FolderSource, compare, doc_key, field_package, norm_rev, rev_order,
                                verify)
from fde_agent.drawings.model import parse_drawing_text, render_drawing, rev_gaps
from fde_agent.drawings.sources import parse_filename
from fde_agent.pods_store import PodsStore, seed_sample
from fde_agent.synth import generate
from fde_agent.tools import ToolBox


@pytest.fixture(scope="module", params=[7, 42])
def dataset(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"dwg{request.param}")
    generate(out, seed=request.param)
    key = json.loads((out / "answer_key.json").read_text())["seeded_issues"]["drawing_discrepancies"]
    store = PodsStore().load_csv_dir(out)
    return out, DrawingIndex.from_folder(out / "drawings", store), key


def test_document_numbers_match_however_typed():
    assert doc_key("SYN-101-AL-001") == doc_key("syn101_al_1") == doc_key("SYN 101-AL-01")
    assert doc_key("SYN-101-AL-001") != doc_key("SYN-101-AL-010")


def test_revisions_normalize_and_order():
    assert [norm_rev(r) for r in ("Rev B", "R1", "rev.0", " b ", "02")] == ["B", "1", "0", "B", "2"]
    assert sorted(["1", "A", "0", "B", "10", "2"], key=rev_order) == ["A", "B", "0", "1", "2", "10"]
    assert rev_gaps(["A", "0", "1", "3"]) == ["2"]
    assert rev_gaps(["A", "C", "0"]) == ["B"]


@pytest.mark.parametrize("name,doc,rev", [
    ("SYN-101-AL-001 Rev 1.txt", "SYN-101-AL-001", "1"),
    ("SYN101AL001_R0.txt", "SYN-101-AL-001", "0"),
    ("Copy of SYN-105-AL-009_RevA FINAL.txt", "SYN-105-AL-009", "A"),
    ("syn-102-vd-03 rev 2 (field set).txt", "SYN-102-VD-03", "2"),
    ("SYN-101-XD-02 markup.txt", "SYN-101-XD-02", None),
])
def test_filename_parsing(name, doc, rev):
    d, r = parse_filename(name)
    assert doc_key(d) == doc_key(doc) and r == rev


def test_title_block_round_trip():
    tb = {"title": "Test", "doc_number": "X-1", "rev": "2", "status": "IFC", "stations": "0-100",
          "references": [{"doc_number": "VND-1", "rev": "B"}]}
    text = render_drawing("Co", tb, {"S1": {"wall_in": 0.25, "grade": "X52"}}, [{"rev": "2", "date": "d", "description": "x"}])
    p = parse_drawing_text(text)
    assert p["title_block"]["rev"] == "2" and p["attributes"]["S1"] == {"wall_in": 0.25, "grade": "X52"}
    assert p["references"] == [{"doc_number": "VND-1", "rev": "B"}]


def test_findings_match_answer_key_exactly(dataset):
    """Every planted discrepancy is found, and nothing else is flagged."""
    _, idx, key = dataset
    found = {(f["type"], doc_key(f["doc_number"])) for f in verify(idx)["findings"]}
    assert found == {(k["type"], doc_key(k["doc_number"])) for k in key}


def test_every_drawing_issue_type_is_planted(dataset):
    _, _, key = dataset
    assert {k["type"] for k in key} == {
        "superseded_reference", "broken_reference", "uncontrolled_revision", "conflicting_copy",
        "title_block_mismatch", "revision_gap", "vendor_rating_mismatch", "part_number_mismatch",
        "vendor_not_approved", "record_mismatch", "stale_copy"}


def test_search_by_messy_number_feature_and_station(dataset):
    _, idx, _ = dataset
    some = next(f for f in idx.families.values() if f.current.doc_type == "valve_detail")
    hit = idx.search(text=some.doc_number.lower().replace("-", " "))
    assert [h["doc_number"] for h in hit] == [some.doc_number]
    tag = some.current.attributes["valve"]["tag"]
    by_tag = {h["doc_number"] for h in idx.search(feature_id=tag)}
    assert some.doc_number in by_tag and any(h["doc_type"] == "vendor_ga" for h in idx.search(feature_id=tag))
    sta = some.current.attributes["valve"]["station_ft"]
    near = idx.search(pipeline_id=some.current.pipeline_id, begin_station_ft=sta - 1, end_station_ft=sta + 1)
    assert some.doc_number in {h["doc_number"] for h in near}
    assert any(h["doc_type"] == "alignment_sheet" for h in near)


def test_uncontrolled_and_conflicting_copies_show_in_search(dataset):
    _, idx, key = dataset
    for kind, state in (("uncontrolled_revision", "newer_than_controlled"), ("conflicting_copy", "conflicts_with_current")):
        doc = next(k["doc_number"] for k in key if k["type"] == kind)
        assert state in {c["state"] for c in idx.get(doc)["copies"]}


def test_compare_shows_what_changed_in_a_conflicting_copy(dataset):
    _, idx, key = dataset
    doc = next(k["doc_number"] for k in key if k["type"] == "conflicting_copy")
    diff = compare(idx, doc, doc_b=doc, source_b="shared_drive")
    assert not diff["identical"] and diff["differences"]


def test_package_blocks_on_planted_issue(dataset):
    _, idx, key = dataset
    k = next(k for k in key if k["type"] == "vendor_rating_mismatch")
    fam = idx.family(k["doc_number"])
    sta = fam.current.begin_station_ft
    pkg = field_package(idx, fam.current.pipeline_id, sta - 100, sta + 100)
    assert not pkg["ready_to_issue"]
    assert any(b["type"] == "vendor_rating_mismatch" for b in pkg["blocking_issues"])
    assert pkg["vendor_drawings"]


def test_folder_source_uses_extractors_and_skips_unknown(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ABC-100-AL-001 Rev 2.pdf").write_bytes(b"%PDF")
    (tmp_path / "a" / "notes.docx").write_bytes(b"x")
    src = FolderSource(tmp_path, extractors={".pdf": lambda p: "DRAWING NO: ABC-100-AL-001\nREV: 2\n"})
    recs = list(src.records())
    assert [(r.doc_number, r.rev) for r in recs] == [("ABC-100-AL-001", "2")]
    assert src.skipped and src.skipped[0].endswith("notes.docx")


def test_toolbox_drawing_tools(dataset):
    out, idx, key = dataset
    tb = ToolBox(idx.store, drawings=idx)
    doc = key[0]["doc_number"]
    assert json.loads(tb.run("search_drawings", {"text": "valve"}))["count"] > 0
    assert "finding_counts" in json.loads(tb.run("verify_drawings", {"pipeline_id": key[0]["pipeline_id"]}))
    assert "did_you_mean" in json.loads(tb.run("get_drawing", {"doc_number": "NOPE-999-AL-999"}))
    assert json.loads(tb.run("verify_drawings", {"doc_number": doc}))["drawings_checked"] <= 1
    with pytest.raises(ValueError, match="No drawing sources"):
        ToolBox(seed_sample(PodsStore())).run("search_drawings", {"text": "x"})
