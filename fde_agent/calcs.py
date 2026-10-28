"""Deterministic pipeline engineering calculations. The agent calls these; it never does the math itself."""

from __future__ import annotations

from . import codes


def _smys(grade: str | None, smys_psi: float | None) -> float:
    if smys_psi:
        return float(smys_psi)
    if grade is None:
        raise ValueError("Provide either grade or smys_psi")
    key = grade.upper().replace("API 5L", "").replace("GRADE", "").strip()
    if key not in codes.SMYS_PSI:
        raise ValueError(f"Unknown pipe grade {grade!r}; known: {sorted(codes.SMYS_PSI)}")
    return float(codes.SMYS_PSI[key])


def _od(nps: float | None, od_in: float | None) -> float:
    if od_in:
        return float(od_in)
    if nps is None or int(nps) not in codes.NPS_OD_IN:
        raise ValueError(f"Provide od_in, or nps from {sorted(codes.NPS_OD_IN)}")
    return codes.NPS_OD_IN[int(nps)]


def joint_factor(seam_type: str, od_in: float) -> float:
    key = seam_type.lower().strip()
    if key not in codes.JOINT_FACTOR:
        raise ValueError(f"Unknown seam type {seam_type!r}; known: {sorted(codes.JOINT_FACTOR)}")
    e = codes.JOINT_FACTOR[key]
    if e is None:  # unknown seam
        e = 0.80 if od_in > 4.5 else 0.60
    return e


def temperature_factor(temp_f: float) -> float:
    table = codes.TEMP_DERATING
    if temp_f <= table[0][0]:
        return table[0][1]
    if temp_f > table[-1][0]:
        raise ValueError("192.115 does not cover temperatures above 450 F")
    for (t0, f0), (t1, f1) in zip(table, table[1:]):
        if t0 <= temp_f <= t1:
            return round(f0 + (f1 - f0) * (temp_f - t0) / (t1 - t0), 4)
    raise AssertionError("unreachable")


def design_pressure(
    wall_in: float,
    class_location: int,
    seam_type: str = "erw",
    temp_f: float = 60,
    grade: str | None = None,
    smys_psi: float | None = None,
    nps: float | None = None,
    od_in: float | None = None,
) -> dict:
    """49 CFR 192.105: P = (2 S t / D) x F x E x T."""
    s = _smys(grade, smys_psi)
    d = _od(nps, od_in)
    f = codes.DESIGN_FACTOR[int(class_location)]
    e = joint_factor(seam_type, d)
    t = temperature_factor(temp_f)
    p = 2 * s * wall_in / d * f * e * t
    return {
        "design_pressure_psig": round(p, 1),
        "inputs": {"S_psi": s, "t_in": wall_in, "D_in": d, "F": f, "E": e, "T": t},
        "formula": "P = (2*S*t/D) * F * E * T  [49 CFR 192.105]",
    }


def hoop_stress(pressure_psig: float, wall_in: float, grade: str | None = None,
                smys_psi: float | None = None, nps: float | None = None, od_in: float | None = None) -> dict:
    """Barlow hoop stress and percent of SMYS."""
    s = _smys(grade, smys_psi)
    d = _od(nps, od_in)
    sh = pressure_psig * d / (2 * wall_in)
    pct = 100 * sh / s
    if pct > 72:
        band = "above 72% SMYS - exceeds Class 1 design factor"
    elif pct >= 30:
        band = "30-72% SMYS - transmission-level stress (192.3 definition)"
    elif pct >= 20:
        band = "20-30% SMYS"
    else:
        band = "below 20% SMYS"
    return {"hoop_stress_psi": round(sh, 0), "percent_smys": round(pct, 1), "band": band}


def maop(
    design_pressure_psig: float,
    class_location: int,
    test_pressure_psig: float | None = None,
    weakest_component_rating_psig: float | None = None,
    historical_max_psig: float | None = None,
    records_tvc: bool | None = None,
) -> dict:
    """49 CFR 192.619(a): MAOP is the lowest of the applicable limits."""
    limits = {"192.619(a)(1) design pressure of pipe": design_pressure_psig}
    if weakest_component_rating_psig is not None:
        limits["192.619(a)(1) weakest component rating"] = weakest_component_rating_psig
    if test_pressure_psig is not None:
        factor = codes.TEST_FACTOR[int(class_location)]
        limits[f"192.619(a)(2) test pressure / {factor}"] = round(test_pressure_psig / factor, 1)
    if historical_max_psig is not None:
        limits["192.619(a)(3) highest actual operating pressure (lookback)"] = historical_max_psig
    governing = min(limits, key=limits.get)
    flags = []
    if test_pressure_psig is None:
        flags.append("No pressure test on record: 192.619(a)(2) limit cannot be confirmed.")
    if records_tvc is False:
        flags.append("Records not traceable, verifiable and complete: MAOP reconfirmation under 192.624 may be required.")
    return {
        "maop_psig": limits[governing],
        "governing_limit": governing,
        "all_limits_psig": limits,
        "flags": flags,
    }


def required_test_pressure(maop_psig: float, class_location: int) -> dict:
    """Minimum strength test pressure to support a target MAOP (inverse of 192.619(a)(2))."""
    factor = codes.TEST_FACTOR[int(class_location)]
    return {"min_test_pressure_psig": round(maop_psig * factor, 1), "factor": factor,
            "note": "Test duration and medium per Subpart J (192.505/192.507) still apply."}


def class_location(building_count: int, four_story_prevalent: bool = False,
                   identified_site_within_100yd: bool = False) -> dict:
    """49 CFR 192.5 class location from a 1-mile sliding segment building count."""
    if four_story_prevalent:
        cls, why = 4, "Buildings with 4 or more stories above ground are prevalent."
    elif building_count >= 46:
        cls, why = 3, f"{building_count} buildings (46 or more)."
    elif identified_site_within_100yd:
        cls, why = 3, "Identified site (20+ persons) within 100 yards."
    else:
        cls = next(c for limit, c in codes.CLASS_BUILDING_LIMITS + [(10**9, 3)] if building_count <= limit)
        why = f"{building_count} buildings."
    return {"class_location": cls, "basis": why, "design_factor": codes.DESIGN_FACTOR[cls]}


def check_cover(measured_in: float, class_location: int | str, rock: bool = False) -> dict:
    """49 CFR 192.327 minimum depth of cover check."""
    key = class_location if class_location == "road_ditch" else int(class_location)
    normal, consolidated_rock = codes.COVER_IN[key]
    required = consolidated_rock if rock else normal
    return {"measured_in": measured_in, "required_in": required,
            "compliant": measured_in >= required, "deficit_in": max(0, required - measured_in)}
