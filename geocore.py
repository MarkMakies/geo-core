# geocore.py

# MIT License
# Copyright (c) 2025 Mark Makies

# requirements: pip install cadquery

import math
from pathlib import Path
from typing import Callable, Dict, Iterable, Sequence, Tuple, Union

import cadquery as cq
from cadquery import exporters, selectors

VectorLike = Union[Tuple[float, float, float], cq.Vector]
Workplane = cq.Workplane

EPSILON = 1e-9
PHI = (1 + math.sqrt(5)) / 2.0
INV_PHI = 1.0 / PHI

# ────────────────────────────────────────────────────────────────────────────────
# User-configurable constants (mm)
# These are the only knobs most users should touch.
HUB_FIT_CLEARANCE = -0.3      # Negative oversize for grip -0.3, -0.1 too small, -0.5 too tight
HUB_INSERTION_DEPTH = 22.0

# Rod sizes (mm)
ROD_OD = 30.0
ROD_ID = 25.0
ROD_LENGTH = 102.0
ROD_END_CHAMFER = 0.5
# ────────────────────────────────────────────────────────────────────────────────

ROD_LENGTH_FACTORS: Tuple[Tuple[str, float], ...] = (
    ("rod_L", 1.0),
    ("rod_sqrt2L", math.sqrt(2.0)),
    ("rod_phiL", PHI),
    ("rod_2L", 2.0),
)
ROD_SPECS = [
    (name, int(round(ROD_LENGTH * factor)))
    for name, factor in ROD_LENGTH_FACTORS
]

class EdgeRadiusRangeSelector(selectors.Selector):
    """Select edges whose radius magnitude matches target within tolerance"""
    def __init__(self, target_radius: float, tol: float = 1e-4) -> None:
        self.target_radius = target_radius
        self.tol = tol

    def filter(self, object_list):
        matches = []
        for obj in object_list:
            try:
                radius = obj.radius()
            except Exception:
                continue
            if radius is None:
                continue
            if abs(abs(radius) - self.target_radius) <= self.tol:
                matches.append(obj)
        return matches

# Derived dimensions
plug_d = ROD_ID - HUB_FIT_CLEARANCE
plug_r = plug_d * 0.5
wall = 2.5
safety = 1.5

# Peg/core (hub) interaction tuning
EMBED = max(3.0, 0.25 * plug_r)
FUSE_EPS = 0.2
FILLET_R = 1.0
MIN_OVERLAP_FRAC = 0.65
BALL_R_OVERRIDE = None
#BALL_R_OVERRIDE = 2.0 * plug_r + safety  # constant ball radius for every part

def _ensure_vector(vec: VectorLike) -> cq.Vector:
    """Ensure cadquery vector."""
    return vec if isinstance(vec, cq.Vector) else cq.Vector(vec)

def _unit(vec: VectorLike) -> cq.Vector:
    """Return normalized vector."""
    v = _ensure_vector(vec)
    return v if v.Length < EPSILON else v.multiply(1.0 / v.Length)

def _angle_between(a: VectorLike, b: VectorLike) -> float:
    """Angle between directions."""
    ua, ub = _unit(a), _unit(b)
    dot = max(-1.0, min(1.0, ua.dot(ub)))
    return math.acos(dot)

def _min_pairwise_angle(directions: Sequence[VectorLike]) -> float:
    """Smallest angle among directions."""
    vectors = [_unit(d) for d in directions]
    min_angle = math.inf
    for i, vec_a in enumerate(vectors):
        for vec_b in vectors[i + 1 :]:
            min_angle = min(min_angle, _angle_between(vec_a, vec_b))
    return max(min_angle, 1e-3)

def _core_radius_for_dirs(directions: Sequence[VectorLike]) -> float:
    """Sphere radius that clears sockets."""
    if BALL_R_OVERRIDE is not None:
        return BALL_R_OVERRIDE
    if len(directions) < 2:
        return plug_r + wall + safety
    min_angle = _min_pairwise_angle(directions)
    r_geo = plug_r / math.sin(min_angle * 0.5) + safety
    return max(plug_r + wall + safety, r_geo)

def _rotation_to_direction(direction: VectorLike) -> Tuple[cq.Vector, float]:
    """Axis-angle rotating +Z to direction."""
    z_axis = cq.Vector(0, 0, 1)
    target = _unit(direction)
    dot = max(-1.0, min(1.0, z_axis.dot(target)))
    angle = math.degrees(math.acos(dot))
    axis = z_axis.cross(target)
    if axis.Length < EPSILON:
        return (cq.Vector(1, 0, 0), 180.0) if angle > 1e-6 else (cq.Vector(0, 0, 1), 0.0)
    return axis, angle

def _cyl_along_dir(length: float, radius: float, direction: VectorLike, base_center: VectorLike) -> Workplane:
    """Extrude cylinder along direction."""
    axis, angle = _rotation_to_direction(_ensure_vector(direction))
    solid = cq.Workplane("XY").circle(radius).extrude(length)
    if angle:
        solid = solid.rotate((0, 0, 0), (axis.x, axis.y, axis.z), angle)
    return solid.translate(_ensure_vector(base_center).toTuple())

def _peg(direction: VectorLike, core_radius: float) -> Workplane:
    """Peg with tip sphere."""
    v_dir = _unit(direction)
    embed = min(
        core_radius - safety,
        max(EMBED, FILLET_R + FUSE_EPS + 0.25, MIN_OVERLAP_FRAC * plug_r),
    )
    start_offset = core_radius - embed - FUSE_EPS
    base = v_dir.multiply(start_offset)
    rod_radius = 0.5 * ROD_OD
    radial_clear = (
        core_radius - math.sqrt(max(core_radius**2 - rod_radius**2, 0.0))
        if core_radius > rod_radius
        else 0.0
    )
    outside = max(0.0, HUB_INSERTION_DEPTH - radial_clear)
    length = embed + FUSE_EPS + outside
    peg = _cyl_along_dir(length, plug_r, v_dir, base)
    tip_center = base.add(v_dir.multiply(length))
    tip = cq.Workplane("XY").sphere(plug_r).translate(tip_center.toTuple())
    return peg.union(tip)

def _rod(length_mm: float) -> Workplane:
    """Create hollow rod with chamfered sockets for hub pegs"""
    outer_radius = 0.5 * ROD_OD
    inner_radius = 0.5 * ROD_ID
    rod = (
        cq.Workplane("XY")
        .circle(outer_radius)
        .circle(inner_radius)
        .extrude(length_mm * 0.5, both=True) 
    )
    if ROD_END_CHAMFER > 0:
        radius_selector = EdgeRadiusRangeSelector(inner_radius)
        try:
            rod = rod.faces(">Z").edges(radius_selector).chamfer(ROD_END_CHAMFER)
            rod = rod.faces("<Z").edges(radius_selector).chamfer(ROD_END_CHAMFER)
        except Exception:
            pass
    return rod

def _hub_from_dirs(directions: Sequence[VectorLike]) -> Workplane:
    """Hub sphere with unioned pegs."""
    radius = _core_radius_for_dirs(directions)
    core = cq.Workplane("XY").sphere(radius)
    for direction in directions:
        core = core.union(_peg(direction, radius))
    try:
        core = core.edges().fillet(FILLET_R)
    except Exception:
        pass
    return core

def _nearest_directions(origin: VectorLike, candidates: Iterable[VectorLike], count: int) -> Sequence[cq.Vector]:
    """Closest direction deltas from origin."""
    origin_vec = _ensure_vector(origin)
    deltas: list[tuple[float, cq.Vector]] = []
    for candidate in candidates:
        delta = _ensure_vector(candidate) - origin_vec
        if delta.Length > EPSILON:
            deltas.append((delta.Length, delta))
    deltas.sort(key=lambda item: item[0])
    return [delta for _, delta in deltas[:count]]

# ── Hub factories ──────────────────────────────────
def hub_straight_2() -> Workplane:
    """Two-opposed sockets."""
    return _hub_from_dirs([(0, 0, 1), (0, 0, -1)])

def hub_elbow_90_2() -> Workplane:
    """Right-angle pair."""
    return _hub_from_dirs([(0, 0, 1), (1, 0, 0)])

def hub_corner_cube_3() -> Workplane:
    """Axis-aligned triad."""
    return _hub_from_dirs([(1, 0, 0), (0, 1, 0), (0, 0, 1)])

def hub_tetra_3() -> Workplane:
    """Tetra face normals."""
    return _hub_from_dirs([(0, -1, -1), (-1, 0, -1), (-1, -1, 0)])

def hub_octa_4() -> Workplane:
    """Octa face center."""
    return _hub_from_dirs([(-1, 1, 0), (-1, -1, 0), (-1, 0, 1), (-1, 0, -1)])

def hub_icosa_5() -> Workplane:
    """Icosa vertex."""
    signs = (-1, 1)
    vertices = []
    for s1 in signs:
        for s2 in signs:
            vertices.extend(
                [
                    cq.Vector(0, s1, s2 * PHI),
                    cq.Vector(s1, s2 * PHI, 0),
                    cq.Vector(s2 * PHI, 0, s1),
                ]
            )
    v0 = cq.Vector(0, 1, PHI)
    directions = _nearest_directions(v0, vertices, 5)
    return _hub_from_dirs(directions)

def hub_dodeca_3() -> Workplane:
    """Dodeca vertex."""
    vertices = []
    signs = (-1, 1)
    for sx in signs:
        for sy in signs:
            for sz in signs:
                vertices.append(cq.Vector(sx, sy, sz))
    for sy in signs:
        for sz in signs:
            vertices.append(cq.Vector(0, sy * INV_PHI, sz * PHI))
    for sx in signs:
        for sy in signs:
            vertices.append(cq.Vector(sx * INV_PHI, sy * PHI, 0))
    for sx in signs:
        for sz in signs:
            vertices.append(cq.Vector(sx * PHI, 0, sz * INV_PHI))
    v0 = cq.Vector(1, 1, 1)
    directions = _nearest_directions(v0, vertices, 3)
    return _hub_from_dirs(directions)

def hub_cubic_6() -> Workplane:
    """Six axial sockets."""
    return _hub_from_dirs([
        (1, 0, 0), (-1, 0, 0),
        (0, 1, 0), (0, -1, 0),
        (0, 0, 1), (0, 0, -1),
    ])

def hub_trigonal_planar_3() -> Workplane:
    """Planar 120° triad."""
    angle = 2.0 * math.pi / 3.0
    return _hub_from_dirs([
        (1.0, 0.0, 0.0),
        (math.cos(angle), math.sin(angle), 0.0),
        (math.cos(-angle), math.sin(-angle), 0.0),
    ])

def hub_hex_planar_6() -> Workplane:
    """Planar 60° hex."""
    directions = [
        (math.cos(k * math.pi / 3.0), math.sin(k * math.pi / 3.0), 0.0)
        for k in range(6)
    ]
    return _hub_from_dirs(directions)

def hub_tetrahedral_4() -> Workplane:
    """Tetrahedral set."""
    return _hub_from_dirs([
        (1, 1, 1),
        (1, -1, -1),
        (-1, 1, -1),
        (-1, -1, 1),
    ])

# Factories dictionaries
HUB_FACTORIES: Dict[str, Callable[[], Workplane]] = {
    "straight_2": hub_straight_2,
    "elbow_90_2": hub_elbow_90_2,
    "corner_cube_3": hub_corner_cube_3,
    "tetra_3": hub_tetra_3,
    "octa_4": hub_octa_4,
    "icosa_5": hub_icosa_5,
    "dodeca_3": hub_dodeca_3,
    "cubic_6": hub_cubic_6,
    "trigonal_planar_3": hub_trigonal_planar_3,
    "hex_planar_6": hub_hex_planar_6,
    "tetrahedral_4": hub_tetrahedral_4,
}

ROD_FACTORIES: Dict[str, Callable[[], Workplane]] = {
    name: (lambda length_mm=length_mm: _rod(float(length_mm))) 
    for name, length_mm in ROD_SPECS
}

def _export_all() -> None:
    export_dir = Path("./stl")
    export_dir.mkdir(exist_ok=True)
    produced = []
    for name, factory in HUB_FACTORIES.items():
        exporters.export(factory(), str(export_dir / f"hub_{name}.stl"))
        produced.append(f"hub_{name}")
    for name, factory in ROD_FACTORIES.items():
        exporters.export(factory(), str(export_dir / f"{name}.stl"))
        produced.append(name)

if __name__ == "__main__":
    _export_all()
