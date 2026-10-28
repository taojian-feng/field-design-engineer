"""Tool definitions exposed to Claude and their dispatch to deterministic Python."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from . import calcs, field
from .pods_store import PodsStore

NUM = {"type": "number"}
INT_CLASS = {"type": "integer", "enum": [1, 2, 3, 4], "description": "49 CFR 192.5 class location"}
PIPE_PROPS = {
    "wall_in": {"type": "number", "description": "Nominal wall thickness, inches"},
    "grade": {"type": "string", "description": "API 5L grade, e.g. B, X42, X52, X65"},
    "smys_psi": {"type": "number", "description": "SMYS in psi (use instead of grade)"},
    "nps": {"type": "number", "description": "Nominal pipe size"},
    "od_in": {"type": "number", "description": "Outside diameter, inches (use instead of nps)"},
}

TOOLS: list[dict] = [
    {
        "name": "design_pressure",
        "description": "Steel pipe design pressure per 49 CFR 192.105 (P = 2St/D x F x E x T). Use for any new or replacement pipe spec.",
        "input_schema": {"type": "object", "properties": {
            **PIPE_PROPS, "class_location": INT_CLASS,
            "seam_type": {"type": "string", "description": "seamless, erw, efw, saw, dsaw, efw_low, spiral_a211, lap_welded, furnace_butt, unknown"},
            "temp_f": {"type": "number", "description": "Design temperature, deg F (default 60)"},
        }, "required": ["wall_in", "class_location"]},
    },
    {
        "name": "hoop_stress",
        "description": "Barlow hoop stress and percent SMYS at a given pressure.",
        "input_schema": {"type": "object", "properties": {"pressure_psig": NUM, **PIPE_PROPS},
                         "required": ["pressure_psig", "wall_in"]},
    },
    {
        "name": "maop",
        "description": "Combine limits into MAOP per 49 CFR 192.619(a): lowest of design pressure, weakest component, test pressure / class factor, and historical operating pressure.",
        "input_schema": {"type": "object", "properties": {
            "design_pressure_psig": NUM, "class_location": INT_CLASS, "test_pressure_psig": NUM,
            "weakest_component_rating_psig": NUM, "historical_max_psig": NUM,
            "records_tvc": {"type": "boolean", "description": "Are the supporting records traceable, verifiable and complete?"},
        }, "required": ["design_pressure_psig", "class_location"]},
    },
    {
        "name": "required_test_pressure",
        "description": "Minimum hydrostatic test pressure needed to support a target MAOP in a class location.",
        "input_schema": {"type": "object", "properties": {"maop_psig": NUM, "class_location": INT_CLASS},
                         "required": ["maop_psig", "class_location"]},
    },
    {
        "name": "class_location",
        "description": "Determine class location (49 CFR 192.5) from a 1-mile sliding segment building count.",
        "input_schema": {"type": "object", "properties": {
            "building_count": {"type": "integer"}, "four_story_prevalent": {"type": "boolean"},
            "identified_site_within_100yd": {"type": "boolean"},
        }, "required": ["building_count"]},
    },
    {
        "name": "check_cover",
        "description": "Check a depth of cover measurement against 49 CFR 192.327 minimums.",
        "input_schema": {"type": "object", "properties": {
            "measured_in": NUM, "class_location": {"description": "1-4, or 'road_ditch'", "type": ["integer", "string"]},
            "rock": {"type": "boolean", "description": "Consolidated rock"},
        }, "required": ["measured_in", "class_location"]},
    },
    {
        "name": "list_pipelines",
        "description": "List pipelines in the asset (PODS) database.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_assets",
        "description": "Get pipe segments, class location ranges, pressure tests and point features (valves, test stations, crossings) for a pipeline station range.",
        "input_schema": {"type": "object", "properties": {
            "pipeline_id": {"type": "string"}, "begin_station_ft": NUM, "end_station_ft": NUM,
        }, "required": ["pipeline_id", "begin_station_ft"]},
    },
    {
        "name": "current_maop",
        "description": "Compute MAOP for a station range straight from asset records (segments, class locations, tests, component ratings). Flags missing tests and non-TVC records.",
        "input_schema": {"type": "object", "properties": {
            "pipeline_id": {"type": "string"}, "begin_station_ft": NUM, "end_station_ft": NUM,
            "historical_max_psig": NUM,
        }, "required": ["pipeline_id", "begin_station_ft", "end_station_ft"]},
    },
    {
        "name": "class_location_study",
        "description": "Run a sliding-mile 49 CFR 192.5 class location study from the structure inventory along a pipeline and report any class increases versus the recorded class location ranges.",
        "input_schema": {"type": "object", "properties": {"pipeline_id": {"type": "string"}},
                         "required": ["pipeline_id"]},
    },
    {
        "name": "check_valve_spacing",
        "description": "Check mainline block valve spacing along a pipeline against 49 CFR 192.179(a) for the class locations crossed.",
        "input_schema": {"type": "object", "properties": {"pipeline_id": {"type": "string"}},
                         "required": ["pipeline_id"]},
    },
    {
        "name": "review_survey",
        "description": "Review a ROW walkdown CSV (station_ft, observation_type, value, notes) for depth of cover, CP potential, encroachment, exposed pipe and coating findings.",
        "input_schema": {"type": "object", "properties": {
            "pipeline_id": {"type": "string"}, "csv_path": {"type": "string"},
        }, "required": ["pipeline_id", "csv_path"]},
    },
    {
        "name": "reconcile_bom",
        "description": "Compare a design BOM CSV to an as-built materials CSV: missing items, quantity variances, spec substitutions, unplanned items, missing MTRs.",
        "input_schema": {"type": "object", "properties": {
            "design_csv": {"type": "string"}, "asbuilt_csv": {"type": "string"},
        }, "required": ["design_csv", "asbuilt_csv"]},
    },
    {
        "name": "assess_field_change",
        "description": "Impact assessment for a field change request: checks proposed pipe or component against current MAOP and cover requirements, returns verdict and required document updates.",
        "input_schema": {"type": "object", "properties": {
            "pipeline_id": {"type": "string"}, "begin_station_ft": NUM, "end_station_ft": NUM,
            "description": {"type": "string"},
            "proposed": {"type": "object", "description": "Any of nps, wall_in, grade, seam_type, component_rating_psig, depth_of_cover_in, rock"},
        }, "required": ["pipeline_id", "begin_station_ft", "end_station_ft", "description", "proposed"]},
    },
    {
        "name": "update_asset_record",
        "description": "Write an as-built change to the asset database (pipe_segment, point_feature or pressure_test). Requires engineer approval; every write is change-logged against a reference such as an FCR number.",
        "input_schema": {"type": "object", "properties": {
            "table": {"type": "string", "enum": ["pipe_segment", "point_feature", "pressure_test"]},
            "record": {"type": "object", "description": "Fields to insert or update; must include the table's id field"},
            "reference": {"type": "string", "description": "FCR, work order or drawing number justifying the change"},
        }, "required": ["table", "record", "reference"]},
    },
    {
        "name": "save_document",
        "description": "Save a markdown deliverable (report, FCR response, coordination memo, redline notes) to the output folder. Nothing is sent to anyone.",
        "input_schema": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "e.g. fcr-0042-response.md"},
            "content": {"type": "string"},
        }, "required": ["filename", "content"]},
    },
]

WRITE_TOOLS = {"update_asset_record"}


class ToolBox:
    def __init__(self, store: PodsStore, output_dir: str | Path = "output",
                 approve: Callable[[str, dict], bool] | None = None):
        self.store = store
        self.output_dir = Path(output_dir)
        self.approve = approve or (lambda name, args: False)

    def run(self, name: str, args: dict) -> str:
        """Execute one tool call and return a JSON string for the tool_result."""
        if name in WRITE_TOOLS and not self.approve(name, args):
            return json.dumps({"status": "declined", "message": "Engineer declined the write. Do not retry; report the proposed change instead."})
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ValueError(f"Unknown tool {name}")
        return json.dumps(handler(**args), default=str)

    # pure calculations
    _t_design_pressure = staticmethod(calcs.design_pressure)
    _t_hoop_stress = staticmethod(calcs.hoop_stress)
    _t_maop = staticmethod(calcs.maop)
    _t_required_test_pressure = staticmethod(calcs.required_test_pressure)
    _t_class_location = staticmethod(calcs.class_location)
    _t_check_cover = staticmethod(calcs.check_cover)
    _t_reconcile_bom = staticmethod(field.reconcile_bom)

    # asset store
    def _t_list_pipelines(self):
        return self.store.list_pipelines()

    def _t_get_assets(self, pipeline_id, begin_station_ft, end_station_ft=None):
        return self.store.segments_at(pipeline_id, begin_station_ft, end_station_ft)

    def _t_current_maop(self, **kw):
        return field.current_maop(self.store, **kw)

    def _t_class_location_study(self, **kw):
        return field.class_location_study(self.store, **kw)

    def _t_check_valve_spacing(self, **kw):
        return field.check_valve_spacing(self.store, **kw)

    def _t_review_survey(self, **kw):
        return field.review_survey(self.store, **kw)

    def _t_assess_field_change(self, **kw):
        return field.assess_field_change(self.store, **kw)

    def _t_update_asset_record(self, table, record, reference):
        return self.store.upsert(table, record, reference)

    def _t_save_document(self, filename, content):
        safe = Path(filename).name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / safe
        path.write_text(content)
        return {"saved": str(path), "bytes": len(content)}
