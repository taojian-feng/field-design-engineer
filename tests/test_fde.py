import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fde_agent import calcs, field
from fde_agent.agent import FDEAgent
from fde_agent.pods_store import PodsStore, seed_sample
from fde_agent.tools import TOOLS, ToolBox

DATA = Path(__file__).resolve().parent.parent / "data" / "sample"


@pytest.fixture
def store():
    return seed_sample(PodsStore())


def test_design_pressure_hand_calc():
    # 12.75 OD, 0.250 wall, X52, Class 1, ERW: 2*52000*0.25/12.75*0.72 = 1468.2 psig
    r = calcs.design_pressure(wall_in=0.25, class_location=1, grade="X52", nps=12)
    assert r["design_pressure_psig"] == pytest.approx(1468.2, abs=0.1)


def test_temperature_derating_interpolates():
    assert calcs.temperature_factor(275) == pytest.approx(0.9835, abs=1e-4)
    with pytest.raises(ValueError):
        calcs.temperature_factor(500)


def test_unknown_seam_depends_on_size():
    assert calcs.joint_factor("unknown", 12.75) == 0.80
    assert calcs.joint_factor("unknown", 4.5) == 0.60


def test_maop_governing_limit():
    r = calcs.maop(1468.2, 1, test_pressure_psig=1400, weakest_component_rating_psig=1480)
    assert r["maop_psig"] == pytest.approx(1272.7, abs=0.1)
    assert "192.619(a)(2)" in r["governing_limit"]


def test_class_location():
    assert calcs.class_location(10)["class_location"] == 1
    assert calcs.class_location(11)["class_location"] == 2
    assert calcs.class_location(46)["class_location"] == 3
    assert calcs.class_location(3, identified_site_within_100yd=True)["class_location"] == 3
    assert calcs.class_location(3, four_story_prevalent=True)["class_location"] == 4


def test_current_maop_from_records(store):
    r = field.current_maop(store, "L100", 0, 26400)
    seg = {s["segment_id"]: s for s in r["segments"]}
    # S03 is 1965 pipe with unknown seam and no test or MTR
    assert any("No pressure test" in f for f in seg["L100-S03"]["flags"])
    assert any("192.624" in f for f in seg["L100-S03"]["flags"])
    # The ANSI 300 valve at station 15500 caps the adjoining segments at 740 psig
    assert r["maop_psig"] <= 740


def test_survey_review(store):
    r = field.review_survey(store, "L100", str(DATA / "l100_walkdown.csv"))
    issues = " ".join(f["issue"] for f in r["findings"])
    assert "-812 mV" in issues
    assert "Exposed pipe" in issues
    stations = {f["station_ft"] for f in r["findings"]}
    assert 11800 in stations and 4800 in stations
    assert 16900 not in stations  # 26 in over rock meets the 24 in rock minimum in Class 2
    assert r["findings"][0]["severity"] == "high"


def test_bom_reconcile():
    r = field.reconcile_bom(str(DATA / "l100_bom_design.csv"), str(DATA / "l100_asbuilt.csv"))
    types = {(i["item_id"], i["type"]) for i in r["issues"]}
    assert ("P-01", "quantity_variance") in types
    assert ("F-02", "substitution") in types
    assert ("F-02", "traceability") in types
    assert ("V-01", "substitution") in types
    assert ("C-01", "not_installed") in types
    assert ("C-02", "unplanned_item") in types
    assert ("C-02", "traceability") not in types


def test_field_change_rejects_underrated_valve(store):
    r = field.assess_field_change(store, "L100", 0, 8000, {"component_rating_psig": 285},
                                  "Substitute ANSI 150 valve")
    assert r["verdict"] == "rejected"


def test_write_requires_approval(store, tmp_path):
    rec = {"table": "point_feature", "reference": "FCR-0042",
           "record": {"feature_id": "TP-08", "pipeline_id": "L100", "station_ft": 19600,
                      "feature_type": "cp_test_station"}}
    declined = json.loads(ToolBox(store, tmp_path).run("update_asset_record", rec))
    assert declined["status"] == "declined"
    ok = json.loads(ToolBox(store, tmp_path, approve=lambda n, a: True).run("update_asset_record", rec))
    assert ok["action"] == "insert"
    assert store.change_log()[0]["reference"] == "FCR-0042"


def test_every_tool_has_handler(store, tmp_path):
    box = ToolBox(store, tmp_path)
    for t in TOOLS:
        assert hasattr(box, f"_t_{t['name']}"), t["name"]


class FakeClient:
    """Scripted stand-in for the Anthropic client to exercise the tool loop offline."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _block(**kw):
    return SimpleNamespace(**kw)


def test_agent_loop_runs_tools(store, tmp_path):
    client = FakeClient([
        SimpleNamespace(stop_reason="tool_use", content=[
            _block(type="tool_use", id="t1", name="current_maop",
                   input={"pipeline_id": "L100", "begin_station_ft": 0, "end_station_ft": 8000}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="MAOP is set by the test.")]),
    ])
    agent = FDEAgent(ToolBox(store, tmp_path), client=client)
    assert agent.ask("MAOP for L100 0-8000?") == "MAOP is set by the test."
    result_msg = agent.messages[2]  # user turn carrying the tool results
    assert result_msg["role"] == "user"
    payload = json.loads(result_msg["content"][0]["content"])
    assert payload["governing_segment"] == "L100-S01"
    assert client.calls[0]["fallbacks"] == "default"
