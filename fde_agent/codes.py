"""Regulatory lookup tables (49 CFR Part 192, gas transmission/distribution steel pipe).

These are encoded from the published regulation for prototype use. A qualified
engineer must verify every result before it is used for design or operation.
"""

# 192.111 - Design factor (F) by class location
DESIGN_FACTOR = {1: 0.72, 2: 0.60, 3: 0.50, 4: 0.40}

# 192.113 - Longitudinal joint factor (E) for steel pipe
JOINT_FACTOR = {
    "seamless": 1.00,
    "erw": 1.00,  # electric resistance welded (192.113 lists ERW at 1.00)
    "efw": 1.00,  # electric flash welded
    "saw": 1.00,  # submerged arc welded (single or double)
    "dsaw": 1.00,
    "efw_low": 0.80,  # electric fusion welded, ASTM A134 / A139
    "spiral_a211": 0.80,  # ASTM A211 spiral welded
    "lap_welded": 0.80,
    "furnace_butt": 0.60,  # furnace butt welded (ASTM A53, API 5L)
    "unknown": None,  # 192.113: other or unknown -> 0.80 above NPS 4, 0.60 at NPS 4 and below (see calcs)
}

# 192.115 - Temperature derating factor (T) for steel pipe, deg F
TEMP_DERATING = [(250, 1.000), (300, 0.967), (350, 0.933), (400, 0.900), (450, 0.867)]

# 192.619(a)(2)(ii) - Test pressure divisor for steel pipe installed after 11/11/1970
TEST_FACTOR = {1: 1.10, 2: 1.25, 3: 1.50, 4: 1.50}

# 192.327 - Minimum depth of cover in inches: (normal soil, consolidated rock)
COVER_IN = {
    1: (30, 18),
    2: (36, 24),
    3: (36, 24),
    4: (36, 24),
    "road_ditch": (36, 24),  # drainage ditches of public roads and railroad crossings
}

# 192.5 - Class location by count of buildings intended for human occupancy within
# 220 yards of the centerline in any continuous 1-mile sliding segment.
CLASS_BUILDING_LIMITS = [(10, 1), (45, 2)]  # <=10 -> Class 1, 11-45 -> Class 2, >=46 -> Class 3

# API 5L line pipe - Specified Minimum Yield Strength, psi
SMYS_PSI = {
    "A25": 25_000,
    "A": 30_000,
    "B": 35_000,
    "X42": 42_000,
    "X46": 46_000,
    "X52": 52_000,
    "X56": 56_000,
    "X60": 60_000,
    "X65": 65_000,
    "X70": 70_000,
    "X80": 80_000,
}

# ASME B36.10 nominal pipe size -> outside diameter, inches
NPS_OD_IN = {
    2: 2.375, 3: 3.5, 4: 4.5, 6: 6.625, 8: 8.625, 10: 10.75, 12: 12.75,
    14: 14.0, 16: 16.0, 18: 18.0, 20: 20.0, 24: 24.0, 30: 30.0, 36: 36.0, 42: 42.0,
}

# Cathodic protection criterion (49 CFR 192 App. D / AMPP SP0169): pipe-to-soil
# potential of -850 mV or more negative vs. Cu/CuSO4 with current applied.
CP_CRITERION_MV = -850
