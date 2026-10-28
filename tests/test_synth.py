"""The synthetic generator's answer key must agree with what the agent's tools find."""

import json

import pytest

from fde_agent import field
from fde_agent.pods_store import PodsStore
from fde_agent.synth import generate


@pytest.fixture(scope="module", params=[7, 42])
def dataset(request, tmp_path_factory):
    out = tmp_path_factory.mktemp(f"syn{request.param}")
    generate(out, seed=request.param)
    key = json.loads((out / "answer_key.json").read_text())["seeded_issues"]
    return out, PodsStore().load_csv_dir(out), key


def test_reproducible(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a, seed=3)
    generate(b, seed=3)
    for f in ("pipe_segment.csv", "structure.csv", "answer_key.json"):
        assert (a / f).read_text() == (b / f).read_text()


def test_every_issue_type_is_seeded(dataset):
    _, _, key = dataset
    for kind in ("non_tvc_records", "underrated_valve", "valve_spacing_exceeded", "class_location_increase",
                 "shallow_cover", "exposed_pipe", "encroachment", "bom_discrepancy"):
        assert key[kind], kind


def test_class_location_study_matches_key(dataset):
    _, store, key = dataset
    for p in store.list_pipelines():
        pid = p["pipeline_id"]
        found = [(c["begin_station_ft"], c["end_station_ft"])
                 for c in field.class_location_study(store, pid, step_ft=200)["class_increases"]]
        seeded = [(c["begin_station_ft"], c["end_station_ft"]) for c in key["class_location_increase"] if c["pipeline_id"] == pid]
        assert found == seeded, pid


def test_valve_spacing_matches_key(dataset):
    _, store, key = dataset
    for p in store.list_pipelines():
        pid = p["pipeline_id"]
        found = {v["from"] for v in field.check_valve_spacing(store, pid)["violations"]}
        seeded = [v for v in key["valve_spacing_exceeded"] if v["pipeline_id"] == pid]
        assert len(found) == len(seeded), pid


def test_underrated_valves_limit_maop(dataset):
    _, store, key = dataset
    for v in key["underrated_valve"]:
        line = next(p for p in store.list_pipelines() if p["pipeline_id"] == v["pipeline_id"])
        m = field.current_maop(store, v["pipeline_id"], 0, line["end_station_ft"])
        assert m["maop_psig"] <= v["rating_psig"]


def test_survey_findings_match_key(dataset):
    out, store, key = dataset
    for p in store.list_pipelines():
        pid = p["pipeline_id"]
        r = field.review_survey(store, pid, str(out / "walkdowns" / f"{pid}_walkdown.csv"))
        found = {f["station_ft"] for f in r["findings"]}
        for kind in ("low_cp", "shallow_cover", "exposed_pipe", "encroachment"):
            for x in key[kind]:
                if x["pipeline_id"] == pid:
                    assert x["station_ft"] in found, (pid, kind, x)


def test_bom_issues_match_key(dataset):
    out, _, key = dataset
    for proj in key["bom_discrepancy"]:
        r = field.reconcile_bom(str(out / "projects" / f"{proj['project']}_bom_design.csv"),
                                str(out / "projects" / f"{proj['project']}_asbuilt.csv"))
        found = {(i["item_id"], i["type"]) for i in r["issues"]}
        assert {(i["item_id"], i["type"]) for i in proj["issues"]} <= found
