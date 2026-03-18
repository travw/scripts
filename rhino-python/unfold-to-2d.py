#! python3
"""unfold-to-2d: unfold a 3D sheet metal part to a 2D flat pattern.

takes a closed polysurface (sheet metal part with material thickness),
constructs a neutral axis surface, unrolls it, and produces classified
2D curves on fabrication sublayers ready for DXF export & CNC routing.

output layers:
11 - 2D geo::Outside cut  (blue)     — perimeter boundary
11 - 2D geo::Inside cut   (magenta)  — holes / internal cutouts
11 - 2D geo::Mark          (dk green) — bend lines, placement marks, bend angle text

bend angle convention:
  "90 UP" = bent 90° toward the picked face side.
  "90 DN" = bent 90° away from the picked face side.

alias: unfold-to-2d -> _-RunPythonScript "path/to/unfold-to-2d.py"
"""

import System
import System.Drawing
import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
import math
from Rhino.Geometry import (
    AreaMassProperties,
    Brep,
    BrepLoopType,
    Curve,
    LineCurve,
    Line,
    Plane,
    Point3d,
    PointFaceRelation,
    PolylineCurve,
    TextEntity,
    Transform,
    Unroller,
    Vector3d,
)
from Rhino.Geometry.Intersect import Intersection
from Rhino.Input.Custom import GetObject
from Rhino.DocObjects import ObjectType


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
STANDARD_GAUGES = [0.100, 0.125, 0.160, 0.190]
TEXT_HEIGHT = 0.25  # inches


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _doc_translate(curve, dx, dy, dz):
    """translate a curve using doc round-trip (CPython 3 workaround).
    in-memory Curve.Translate() doesn't work reliably in CPython 3."""
    xf = Transform.Translation(dx, dy, dz)
    temp_id = sc.doc.Objects.AddCurve(curve)
    new_id = sc.doc.Objects.Transform(temp_id, xf, True)
    obj = sc.doc.Objects.FindId(new_id)
    if obj is not None:
        curve = obj.Geometry.DuplicateCurve()
    sc.doc.Objects.Delete(new_id, True)
    return curve


def pick_part_and_face():
    """select a closed polysurface and pick a sheet face. returns (brep, face_index, obj_id) or None."""
    go = GetObject()
    go.SetCommandPrompt("select part — pick the UP face (ctrl+shift+click)")
    go.GeometryFilter = ObjectType.Surface
    go.SubObjectSelect = True
    go.EnablePreSelect(True, True)
    go.DeselectAllBeforePostSelect = False
    go.GroupSelect = False
    go.Get()

    if go.CommandResult() != Rhino.Commands.Result.Success:
        return None
    if go.ObjectCount == 0:
        return None

    objref = go.Object(0)
    brep = objref.Brep()
    if brep is None:
        print("error: not a brep/polysurface")
        return None

    if not brep.IsSolid:
        print("error: part must be a closed polysurface (solid)")
        return None

    face = objref.Face()
    if face is None:
        if brep.Faces.Count == 1:
            face = brep.Faces[0]
        else:
            print("error: click a face (ctrl+shift+click for sub-face)")
            return None

    return brep, face.FaceIndex, objref.ObjectId


def get_face_outward_normal(brep, face_index):
    """get outward-pointing normal at the centroid of a brep face.
    uses DuplicateFace to ensure AreaMassProperties works on a Brep (not BrepFace)."""
    face = brep.Faces[face_index]
    face_brep = face.DuplicateFace(False)
    if face_brep is None:
        return None, None
    amp = AreaMassProperties.Compute(face_brep)
    if amp is None:
        return None, None
    centroid = amp.Centroid
    rc, u, v = face.ClosestPoint(centroid)
    if not rc:
        return None, None
    normal = face.NormalAt(u, v)
    if face.OrientationIsReversed:
        normal = -normal
    return centroid, normal


def _untrim_face(face):
    """remove inner trim loops (holes) from a brep face.
    returns a single-face brep with only the outer boundary, or the
    original DuplicateFace if untrimming fails."""
    face_brep = face.DuplicateFace(False)
    if face_brep is None:
        return None
    # if there's only one loop (outer), nothing to untrim
    if face.Loops.Count <= 1:
        return face_brep
    # get the outer loop 3D curve
    outer_crv = face.OuterLoop.To3dCurve()
    if outer_crv is None:
        return face_brep
    # try to create a planar brep from just the outer boundary
    tol = sc.doc.ModelAbsoluteTolerance
    planar = Brep.CreatePlanarBreps([outer_crv], tol)
    if planar and len(planar) > 0:
        return planar[0]
    # imperfect edges: retry with relaxed tolerance for slightly non-planar curves
    planar = Brep.CreatePlanarBreps([outer_crv], tol * 100)
    if planar and len(planar) > 0:
        return planar[0]
    return face_brep


def _shoot_thickness_ray(brep, face_index, origin, normal, tol):
    """shoot rays BOTH directions from origin and collect hit distances.
    uses _untrim_face for targets so rays don't pass through window holes.
    both directions needed because face normals can be inconsistent on imperfect geometry."""
    hits = []
    for direction in [normal, -normal]:
        start = origin + direction * 0.001
        end = origin + direction * 2.0
        ray = LineCurve(Line(start, end))
        for fi in range(brep.Faces.Count):
            if fi == face_index:
                continue
            face_brep = _untrim_face(brep.Faces[fi])
            if face_brep is None:
                continue
            rc, _, intersection_points = Intersection.CurveBrep(ray, face_brep, tol)
            if not rc or intersection_points is None:
                continue
            for pt in intersection_points:
                dist = origin.DistanceTo(pt)
                if dist > 0.01:
                    hits.append(dist)
    return hits


def _snap_hits_to_gauge(hits):
    """filter hits to sensible gauge range and snap to nearest standard gauge."""
    sensible = [d for d in hits if 0.0625 < d < 0.250]
    if not sensible:
        return None
    raw = min(sensible)
    return min(STANDARD_GAUGES, key=lambda g: abs(g - raw))


def detect_thickness(brep, face_index):
    """detect material thickness by shooting rays inward from the picked face.
    tries centroid first, then multi-point sampling across the face.
    snaps to nearest standard aluminum gauge (0.100, 0.125, 0.160, 0.190).
    falls back to minimum edge length if ray approaches fail."""
    tol = sc.doc.ModelAbsoluteTolerance
    face = brep.Faces[face_index]

    # phase 1: try centroid ray (fast path)
    face_brep = face.DuplicateFace(False)
    amp = AreaMassProperties.Compute(face_brep) if face_brep else None
    if amp is not None:
        centroid = amp.Centroid
        rc, u, v = face.ClosestPoint(centroid)
        if rc:
            normal = face.NormalAt(u, v)
            if face.OrientationIsReversed:
                normal = -normal
            hits = _shoot_thickness_ray(brep, face_index, centroid, normal, tol)
            print("  thickness phase 1: centroid hits={}".format(
                ["{:.4f}".format(h) for h in hits]))
            result = _snap_hits_to_gauge(hits)
            if result is not None:
                return result

    # phase 2: multi-point sampling (handles faces with large holes)
    u_dom = face.Domain(0)
    v_dom = face.Domain(1)
    interior_count = 0
    for ui in range(5):
        for vi in range(5):
            u = u_dom.T0 + (u_dom.T1 - u_dom.T0) * (ui + 0.5) / 5
            v = v_dom.T0 + (v_dom.T1 - v_dom.T0) * (vi + 0.5) / 5
            # check if UV point is on trimmed face (not in a hole)
            # use int comparison for CPython 3 enum safety
            pfr = face.IsPointOnFace(u, v)
            is_exterior = (pfr == PointFaceRelation.Exterior or int(pfr) == 2)
            if is_exterior:
                continue
            interior_count += 1
            pt = face.PointAt(u, v)
            normal = face.NormalAt(u, v)
            if face.OrientationIsReversed:
                normal = -normal
            hits = _shoot_thickness_ray(brep, face_index, pt, normal, tol)
            result = _snap_hits_to_gauge(hits)
            if result is not None:
                print("  thickness phase 2: found at sample ({},{})".format(ui, vi))
                return result
    print("  thickness phase 2: {} interior samples, no gauge hits".format(interior_count))

    return _detect_thickness_min_edge(brep, face_index)


def _detect_thickness_min_edge(brep, face_index):
    """fallback thickness detection: shortest edge of the picked face.
    for a sheet face, the shortest edges are the thickness edges."""
    face = brep.Faces[face_index]
    edge_lengths = []
    for ei in face.AdjacentEdges():
        edge_lengths.append(brep.Edges[ei].GetLength())
    if not edge_lengths:
        return None
    edge_lengths.sort()
    raw = edge_lengths[0]
    if raw < 0.01 or raw > 0.5:
        return None
    closest_gauge = min(STANDARD_GAUGES, key=lambda g: abs(g - raw))
    return closest_gauge


def prompt_thickness(auto_thickness):
    """prompt user to accept or override detected thickness. returns float."""
    if auto_thickness is not None:
        msg = "detected thickness: {:.4f}. enter to accept or type override".format(auto_thickness)
        return rs.GetReal(msg, auto_thickness, 0.01, 1.0)
    else:
        return rs.GetReal("could not auto-detect thickness. enter thickness",
                          number=0.125, minimum=0.01, maximum=1.0)


def classify_faces(brep, thickness):
    """classify brep faces into sheet faces and edge faces.
    a sheet face has a parallel partner: shoot rays BOTH directions from its centroid
    and check if either hits another face at ~thickness distance.
    returns (sheet_face_indices, edge_face_indices, partners_dict).
    partners_dict maps each sheet face to its first-found partner."""
    tol = sc.doc.ModelAbsoluteTolerance
    thick_tol = thickness * 0.2  # 20% tolerance for partner distance matching

    # precompute centroids, normals, and untrimmed face breps
    # centroid from trimmed face (lands on material, not in holes)
    # untrimmed faces used only as ray targets (no holes to pass through)
    face_data = []
    face_breps = []
    for i in range(brep.Faces.Count):
        centroid, normal = get_face_outward_normal(brep, i)
        face_data.append((centroid, normal))
        face_breps.append(_untrim_face(brep.Faces[i]))

    # track which faces have a partner (symmetric: if i→j hits, both are sheet)
    sheet_set = set()
    partners = {}  # face_i -> face_j (first-found partner)

    for i in range(brep.Faces.Count):
        if i in sheet_set:
            continue
        ci, ni = face_data[i]
        if ci is None or ni is None:
            print("  face {}: no centroid/normal, skipping".format(i))
            continue

        found = False
        best_dist = None  # track closest hit for diagnostics
        best_j = None
        best_dot = None
        # shoot rays in BOTH directions from centroid
        for direction in [ni, -ni]:
            if found:
                break
            start = ci + direction * 0.001
            end = ci + direction * 2.0  # generous ray length
            ray = LineCurve(Line(start, end))

            for j in range(brep.Faces.Count):
                if i == j:
                    continue
                fb = face_breps[j]
                if fb is None:
                    continue
                rc, _, intersection_points = Intersection.CurveBrep(ray, fb, tol)
                if not rc or intersection_points is None or len(intersection_points) == 0:
                    continue
                for pt in intersection_points:
                    dist = ci.DistanceTo(pt)
                    if abs(dist - thickness) < thick_tol:
                        # verify normals are parallel or antiparallel (sheet partners)
                        # edge faces have perpendicular normals (dot ≈ 0) → rejected
                        # use abs(dot) to handle breps with inconsistent face orientation
                        _, nj = face_data[j]
                        if nj is not None:
                            dot = Vector3d.Multiply(ni, nj)
                            if abs(dot) < 0.5:
                                # track best rejected candidate
                                if best_dist is None or abs(dist - thickness) < abs(best_dist - thickness):
                                    best_dist = dist
                                    best_j = j
                                    best_dot = dot
                                continue
                        sheet_set.add(i)
                        sheet_set.add(j)
                        if i not in partners:
                            partners[i] = j
                        if j not in partners:
                            partners[j] = i
                        print("  face {} <-> face {}: partner at {:.4f}\"".format(i, j, dist))
                        found = True
                        break
                    else:
                        # track nearest miss for diagnostics
                        if best_dist is None or abs(dist - thickness) < abs(best_dist - thickness):
                            best_dist = dist
                            best_j = j
                            _, nj = face_data[j]
                            best_dot = Vector3d.Multiply(ni, nj) if nj is not None else None
                if found:
                    break
        if not found and best_dist is not None:
            print("  face {}: no partner (best: face {} dist={:.4f}\" dot={})".format(
                i, best_j, best_dist,
                "{:.2f}".format(best_dot) if best_dot is not None else "?"))
        elif not found:
            print("  face {}: no partner (no ray hits)".format(i))

    sheet_faces = sorted(sheet_set)
    edge_faces = [i for i in range(brep.Faces.Count) if i not in sheet_set]
    return sheet_faces, edge_faces, partners


def join_sheet_faces(brep, sheet_faces, partners, picked_face_index):
    """join all sheet faces into two polysurfaces representing both sides
    of the aluminum sheet. uses partner pairs for graph-coloring to assign
    faces to sides. returns (ref_side, other_side) where ref_side contains
    the picked face, or (None, None) on failure."""
    tol = sc.doc.ModelAbsoluteTolerance

    # graph coloring: assign each face to side A (0) or side B (1)
    color = {}  # face_index -> 0 or 1
    for fi in sheet_faces:
        if fi in color:
            continue
        # BFS from this face
        color[fi] = 0
        queue = [fi]
        while queue:
            current = queue.pop(0)
            if current not in partners:
                continue
            partner = partners[current]
            if partner in color:
                if color[partner] == color[current]:
                    print("warning: conflict coloring face {} and {} (both side {})".format(
                        current, partner, color[current]))
                continue
            color[partner] = 1 - color[current]
            queue.append(partner)

    # determine which color the picked face got (ref side)
    ref_color = color.get(picked_face_index, 0)
    side_a_indices = [fi for fi in sheet_faces if color.get(fi, 0) == ref_color]
    side_b_indices = [fi for fi in sheet_faces if color.get(fi, 0) != ref_color]
    print("  side A (ref): {} ({} faces)".format(side_a_indices, len(side_a_indices)))
    print("  side B:       {} ({} faces)".format(side_b_indices, len(side_b_indices)))

    if not side_a_indices or not side_b_indices:
        print("error: could not split sheet faces into 2 sides (A={}, B={})".format(
            len(side_a_indices), len(side_b_indices)))
        return None, None

    # join each side separately
    def _join_side(indices):
        face_breps = []
        for fi in indices:
            dup = brep.Faces[fi].DuplicateFace(False)
            if dup is not None:
                face_breps.append(dup)
        if not face_breps:
            return None
        if len(face_breps) == 1:
            return face_breps[0]
        joined = Brep.JoinBreps(face_breps, tol)
        if joined and len(joined) == 1:
            return joined[0]
        elif joined and len(joined) > 1:
            # try looser tolerance
            joined2 = Brep.JoinBreps(list(joined), tol * 10)
            if joined2 and len(joined2) == 1:
                return joined2[0]
            # force merge
            pieces = list(joined2) if joined2 else list(joined)
            result = pieces[0]
            for pi in range(1, len(pieces)):
                result.Join(pieces[pi], tol * 10, True)
            return result
        return face_breps[0]

    side_a = _join_side(side_a_indices)
    side_b = _join_side(side_b_indices)

    if side_a is None or side_b is None:
        print("error: failed to join sheet faces into sides")
        return None, None

    return side_a, side_b


def identify_reference_side(side_a, side_b, brep, picked_face_index):
    """determine which joined polysurface contains the picked face.
    returns (reference_side, other_side)."""
    centroid, _ = get_face_outward_normal(brep, picked_face_index)
    if centroid is None:
        return side_a, side_b

    dist_a = side_a.ClosestPoint(centroid).DistanceTo(centroid)
    dist_b = side_b.ClosestPoint(centroid).DistanceTo(centroid)

    if dist_a <= dist_b:
        return side_a, side_b
    else:
        return side_b, side_a


def _segments_cross_2d(a0, a1, b0, b1):
    """test if 2D line segments a0-a1 and b0-b1 cross each other.
    a0,a1,b0,b1 are (x,y) tuples. returns True if segments properly cross."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    d1 = cross(a0, a1, b0)
    d2 = cross(a0, a1, b1)
    d3 = cross(b0, b1, a0)
    d4 = cross(b0, b1, a1)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _compute_vertex(line_a, type_a, line_b, type_b, face_plane):
    """compute the vertex where two adjacent edge lines meet.
    uses a cascade of strategies for robustness.
    returns Point3d or None."""
    tol = sc.doc.ModelAbsoluteTolerance

    # strategy 1: direct LineLine intersection
    rc, ta, tb = Intersection.LineLine(line_a, line_b)
    if rc:
        pt_a = line_a.PointAt(ta)
        pt_b = line_b.PointAt(tb)
        gap = pt_a.DistanceTo(pt_b)
        if gap < tol * 100:  # generous for 3D skew
            return Point3d((pt_a.X + pt_b.X) / 2,
                           (pt_a.Y + pt_b.Y) / 2,
                           (pt_a.Z + pt_b.Z) / 2)

    # strategy 2: project to offset plane, intersect in 2D
    origin = face_plane.Origin
    x_axis = face_plane.XAxis
    y_axis = face_plane.YAxis

    def to_2d(pt):
        v = pt - origin
        return (Vector3d.Multiply(v, x_axis), Vector3d.Multiply(v, y_axis))

    def from_2d(u, v):
        return origin + x_axis * u + y_axis * v

    a0_2d = to_2d(line_a.From)
    a1_2d = to_2d(line_a.To)
    b0_2d = to_2d(line_b.From)
    b1_2d = to_2d(line_b.To)

    dax = a1_2d[0] - a0_2d[0]
    day = a1_2d[1] - a0_2d[1]
    dbx = b1_2d[0] - b0_2d[0]
    dby = b1_2d[1] - b0_2d[1]
    denom = dax * dby - day * dbx

    if abs(denom) > 1e-10:
        dx = b0_2d[0] - a0_2d[0]
        dy = b0_2d[1] - a0_2d[1]
        t = (dx * dby - dy * dbx) / denom
        u = a0_2d[0] + t * dax
        v = a0_2d[1] + t * day
        return from_2d(u, v)

    # strategy 3: parallel lines — use shared endpoint
    if type_b in ("perimeter", "perimeter_fallback"):
        return Point3d(line_b.From.X, line_b.From.Y, line_b.From.Z)
    if type_a in ("perimeter", "perimeter_fallback"):
        return Point3d(line_a.To.X, line_a.To.Y, line_a.To.Z)

    return None


def _validate_polygon(vertices, face_plane, tol):
    """validate and repair a polygon: remove duplicates, collinear points,
    project to plane, check self-intersection. returns cleaned list or None."""
    if len(vertices) < 3:
        return None

    # remove consecutive duplicates
    cleaned = [vertices[0]]
    for i in range(1, len(vertices)):
        if vertices[i].DistanceTo(cleaned[-1]) > tol:
            cleaned.append(vertices[i])
    if len(cleaned) > 1 and cleaned[-1].DistanceTo(cleaned[0]) < tol:
        cleaned.pop()
    if len(cleaned) < 3:
        return None

    # remove collinear interior points
    simplified = []
    n = len(cleaned)
    for i in range(n):
        prev_pt = cleaned[(i - 1) % n]
        curr = cleaned[i]
        next_pt = cleaned[(i + 1) % n]
        seg = Line(prev_pt, next_pt)
        t = seg.ClosestParameter(curr)
        closest = seg.PointAt(t)
        if curr.DistanceTo(closest) > tol * 2:
            simplified.append(curr)
    if len(simplified) < 3:
        return None

    # project to face plane for exact planarity
    projected = []
    for pt in simplified:
        projected.append(face_plane.ClosestPoint(pt))

    # skip self-intersection check — let CreatePlanarBreps be the judge
    # (the check had false positives on simple faces with near-collinear edges)
    return projected


def _find_through_face(ref_side, fi, edge, skipped_faces, face_planes):
    """trace through a chain of skipped transition faces to find the next
    real face. handles multi-level chains (e.g., 32 → 0 → 24 → 22)."""
    visited = {fi}
    current_fi = fi
    current_edge = edge

    for step in range(10):  # safety limit
        adj = list(current_edge.AdjacentFaces())
        if len(adj) != 2:
            print("    look-through step {}: edge has {} adj faces, dead end".format(step, len(adj)))
            return None
        next_fi = adj[0] if adj[1] == current_fi else adj[1]
        if next_fi in visited:
            print("    look-through step {}: face {} already visited, loop".format(step, next_fi))
            return None
        visited.add(next_fi)

        in_planes = next_fi in face_planes
        in_skipped = next_fi in skipped_faces
        print("    look-through step {}: face {} → face {} (planes={}, skipped={})".format(
            step, current_fi, next_fi, in_planes, in_skipped))

        # found a real (non-skipped) face with an offset plane
        if in_planes and not in_skipped:
            print("    look-through: found real face {}!".format(next_fi))
            return next_fi

        # hit a face that's not in face_planes at all (non-sheet edge face)
        if not in_skipped:
            print("    look-through: face {} not skipped and not in planes, dead end".format(next_fi))
            return None

        # it's a skipped face — try ALL other edges (not just the first)
        skipped_face = ref_side.Faces[next_fi]
        if skipped_face.OuterLoop is None:
            return None
        # first check: does any edge lead directly to a real face?
        for trim in skipped_face.OuterLoop.Trims:
            if trim.Edge is None:
                continue
            other_edge = trim.Edge
            if other_edge.EdgeIndex == current_edge.EdgeIndex:
                continue
            other_adj = list(other_edge.AdjacentFaces())
            if len(other_adj) != 2:
                continue
            far_fi = other_adj[0] if other_adj[1] == next_fi else other_adj[1]
            if far_fi in face_planes and far_fi not in skipped_faces and far_fi not in visited:
                return far_fi  # found real face directly
        # second check: try edges leading to more skipped faces (continue walking)
        found_next = False
        for trim in skipped_face.OuterLoop.Trims:
            if trim.Edge is None:
                continue
            other_edge = trim.Edge
            if other_edge.EdgeIndex == current_edge.EdgeIndex:
                continue
            other_adj = list(other_edge.AdjacentFaces())
            if len(other_adj) != 2:
                continue
            far_fi = other_adj[0] if other_adj[1] == next_fi else other_adj[1]
            if far_fi in visited:
                continue
            if far_fi in skipped_faces:
                current_fi = next_fi
                current_edge = other_edge
                found_next = True
                break
        if not found_next:
            return None
    return None


def _build_edge_lines(face, fi, face_planes, face_normals, offset_dist,
                      ref_side=None, skipped_faces=None):
    """build edge lines for a face's outer loop. every trim produces exactly
    one (type, Line, target_fi) entry. bend edges use PlanePlane with translated
    fallback. target_fi is the PlanePlane partner face index (or None for perimeter).
    respects trim.IsReversed() for consistent winding."""
    normal = face_normals[fi]
    offset_vec = Vector3d(-normal.X * offset_dist,
                           -normal.Y * offset_dist,
                           -normal.Z * offset_dist)
    edge_lines = []
    for trim in face.OuterLoop.Trims:
        edge = trim.Edge
        if edge is None:
            continue

        # edge direction respecting trim winding
        if trim.IsReversed():
            e_start = edge.PointAtEnd
            e_end = edge.PointAtStart
        else:
            e_start = edge.PointAtStart
            e_end = edge.PointAtEnd

        adj = list(edge.AdjacentFaces())
        if len(adj) == 2:
            other = adj[0] if adj[1] == fi else adj[1]
            target_fi = other  # face to use for PlanePlane

            # if adjacent face is skipped (transition face), find nearest real face
            # by geometric proximity (topological walk fails on complex corner topology)
            if skipped_faces and other in skipped_faces and ref_side is not None:
                edge_mid = Point3d((e_start.X + e_end.X) / 2,
                                   (e_start.Y + e_end.Y) / 2,
                                   (e_start.Z + e_end.Z) / 2)
                best_fi = None
                best_d = float("inf")
                for cfi in face_planes:
                    if cfi == fi or cfi in skipped_faces:
                        continue
                    cf_brep = ref_side.Faces[cfi].DuplicateFace(False)
                    cf_amp = AreaMassProperties.Compute(cf_brep)
                    if cf_amp is not None:
                        d = edge_mid.DistanceTo(cf_amp.Centroid)
                        if d < best_d:
                            best_d = d
                            best_fi = cfi
                if best_fi is not None:
                    target_fi = best_fi

            if target_fi in face_planes and target_fi not in (skipped_faces or set()):
                rc, int_line = Intersection.PlanePlane(face_planes[fi], face_planes[target_fi])
                if rc:
                    edge_lines.append(("bend", int_line, target_fi))
                    continue
            # PlanePlane failed or target not usable: fallback to translated
            p0 = Point3d(e_start.X + offset_vec.X, e_start.Y + offset_vec.Y, e_start.Z + offset_vec.Z)
            p1 = Point3d(e_end.X + offset_vec.X, e_end.Y + offset_vec.Y, e_end.Z + offset_vec.Z)
            edge_lines.append(("perimeter_fallback", Line(p0, p1), None))
        else:
            p0 = Point3d(e_start.X + offset_vec.X, e_start.Y + offset_vec.Y, e_start.Z + offset_vec.Z)
            p1 = Point3d(e_end.X + offset_vec.X, e_end.Y + offset_vec.Y, e_end.Z + offset_vec.Z)
            edge_lines.append(("perimeter", Line(p0, p1), None))

    return edge_lines


def _merge_same_target_runs(edge_lines):
    """merge consecutive bend edges that share the same PlanePlane target.
    when multiple edges adjacent to skipped transition faces all resolve to
    the same target, they produce identical infinite lines that cause
    degenerate vertices. collapsing them into a single bend entry fixes this."""
    if len(edge_lines) < 3:
        return edge_lines
    merged = []
    i = 0
    while i < len(edge_lines):
        typ, line, target = edge_lines[i]
        if target is not None:
            # start of a potential run — find how far it extends
            run_end = i + 1
            while run_end < len(edge_lines) and edge_lines[run_end][2] == target:
                run_end += 1
            if run_end - i > 1:
                # run of N entries with same target: keep just one bend entry
                merged.append(("bend", line, target))
                i = run_end
                continue
        merged.append((typ, line, target))
        i += 1
    return merged


def construct_neutral_axis(ref_side, thickness, original_brep=None):
    """construct the neutral axis surface from plane geometry.
    for each planar face in ref_side, computes the offset plane (t/2 inward),
    then builds each face's boundary from:
      - bend edges: plane-plane intersection of adjacent offset planes
      - perimeter edges: original edge translated to offset plane
    produces sharp corners at bends with exact edge connectivity."""
    tol = sc.doc.ModelAbsoluteTolerance
    offset_dist = thickness / 2.0

    # step 1: compute offset plane for each face
    face_planes = {}
    face_normals = {}
    for fi in range(ref_side.Faces.Count):
        face = ref_side.Faces[fi]
        plane_tol = max(tol * 10, 0.01)  # loosen for near-planar faces
        rc, plane = face.TryGetPlane(plane_tol)
        if not rc:
            print("warning: face {} is not planar, skipping".format(fi))
            continue
        # get outward normal for this face in the ref_side context
        face_brep = face.DuplicateFace(False)
        amp = AreaMassProperties.Compute(face_brep)
        if amp is None:
            print("warning: face {} AreaMassProperties failed, skipping".format(fi))
            continue
        centroid = amp.Centroid
        rc2, u, v = face.ClosestPoint(centroid)
        if not rc2:
            print("warning: face {} ClosestPoint failed, skipping".format(fi))
            continue
        normal = face.NormalAt(u, v)
        if face.OrientationIsReversed:
            normal = -normal

        # verify offset direction: NAS point should be inside the original solid
        if original_brep is not None:
            test_pt = centroid - normal * offset_dist
            if not original_brep.IsPointInside(test_pt, tol, False):
                normal = -normal  # offset landed outside — flip

        # offset plane inward (opposite to outward normal)
        offset_origin = plane.Origin - normal * offset_dist
        face_planes[fi] = Plane(offset_origin, plane.XAxis, plane.YAxis)
        face_normals[fi] = normal

    if len(face_planes) < 1:
        print("error: no planar faces found in ref_side")
        return None

    # step 2: build each neutral axis face using robust helpers
    print("=== neutral axis construction ===")
    print("  ref_side has {} faces, {} have offset planes".format(
        ref_side.Faces.Count, len(face_planes)))

    # map ref_side face indices to original brep face indices by centroid matching
    orig_map = {}
    if original_brep is not None:
        for rfi in range(ref_side.Faces.Count):
            rf_brep = ref_side.Faces[rfi].DuplicateFace(False)
            rf_amp = AreaMassProperties.Compute(rf_brep)
            if rf_amp is None:
                continue
            rc = rf_amp.Centroid
            best_oi = -1
            best_d = float("inf")
            for oi in range(original_brep.Faces.Count):
                oc, _ = get_face_outward_normal(original_brep, oi)
                if oc is None:
                    continue
                d = rc.DistanceTo(oc)
                if d < best_d:
                    best_d = d
                    best_oi = oi
            if best_oi >= 0 and best_d < 1.0:
                orig_map[rfi] = best_oi

    # pre-compute skipped faces (transition faces below area threshold)
    min_area = thickness * 2.0
    skipped_faces = set()
    for fi in face_planes:
        face_brep_check = ref_side.Faces[fi].DuplicateFace(False)
        amp_check = AreaMassProperties.Compute(face_brep_check)
        if amp_check is not None and amp_check.Area < min_area:
            skipped_faces.add(fi)

    neutral_faces = []
    nas_ok = 0
    nas_skip = 0
    for fi in face_planes:
        normal = face_normals[fi]
        face = ref_side.Faces[fi]
        fl = "face {}".format(fi)
        if fi in orig_map:
            fl = "face {} (orig {})".format(fi, orig_map[fi])
        if face.OuterLoop is None:
            print("  {}: no outer loop → SKIPPED".format(fl))
            nas_skip += 1
            continue

        # skip small transition faces (they're in the polysurface for connectivity
        # but shouldn't generate their own NAS face)
        if fi in skipped_faces:
            face_brep_area = face.DuplicateFace(False)
            amp_area = AreaMassProperties.Compute(face_brep_area)
            area_val = amp_area.Area if amp_area else 0
            print("  {}: area {:.4f} < {:.4f} min → SKIPPED (transition face)".format(
                fl, area_val, min_area))
            nas_skip += 1
            continue

        # build edge lines (look through skipped faces to find real neighbors)
        edge_lines = _build_edge_lines(face, fi, face_planes, face_normals, offset_dist,
                                        ref_side=ref_side, skipped_faces=skipped_faces)
        pre_merge = len(edge_lines)
        edge_lines = _merge_same_target_runs(edge_lines)
        n_bend = sum(1 for t, _, _tgt in edge_lines if t == "bend")
        n_perim = sum(1 for t, _, _tgt in edge_lines if t == "perimeter")
        n_fallback = sum(1 for t, _, _tgt in edge_lines if t == "perimeter_fallback")
        n_merged = pre_merge - len(edge_lines)
        n_trims = face.OuterLoop.Trims.Count

        if len(edge_lines) < 3:
            print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → SKIPPED (too few)".format(
                fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback))
            nas_skip += 1
            continue

        # compute vertices with multi-strategy intersection
        n = len(edge_lines)
        vertices = []
        for i in range(n):
            type_a, line_a, _tgt_a = edge_lines[i]
            type_b, line_b, _tgt_b = edge_lines[(i + 1) % n]
            pt = _compute_vertex(line_a, type_a, line_b, type_b, face_planes[fi])
            if pt is not None:
                vertices.append(pt)

        if len(vertices) < 3:
            # fallback: translate the face boundary curve to offset plane
            outer_crv = face.OuterLoop.To3dCurve()
            offset_vec = Vector3d(-normal.X * offset_dist,
                                   -normal.Y * offset_dist,
                                   -normal.Z * offset_dist)
            if outer_crv is not None:
                outer_translated = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
                fb = Brep.CreatePlanarBreps([outer_translated], tol)
                if not fb or len(fb) == 0:
                    fb = Brep.CreatePlanarBreps([outer_translated], tol * 100)
                if fb and len(fb) > 0:
                    neutral_faces.append(fb[0])
                    print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → FALLBACK (boundary translate) → OK".format(
                        fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
                    nas_ok += 1
                    continue
            print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → SKIPPED".format(
                fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
            nas_skip += 1
            continue

        # validate and repair polygon
        validated = _validate_polygon(vertices, face_planes[fi], tol)
        if validated is None:
            # fallback: translate the face boundary curve to offset plane
            outer_crv = face.OuterLoop.To3dCurve()
            offset_vec = Vector3d(-normal.X * offset_dist,
                                   -normal.Y * offset_dist,
                                   -normal.Z * offset_dist)
            if outer_crv is not None:
                outer_translated = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
                fb = Brep.CreatePlanarBreps([outer_translated], tol)
                if not fb or len(fb) == 0:
                    fb = Brep.CreatePlanarBreps([outer_translated], tol * 100)
                if fb and len(fb) > 0:
                    neutral_faces.append(fb[0])
                    print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → INVALID → FALLBACK → OK".format(
                        fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
                    nas_ok += 1
                    continue
            print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → INVALID → SKIPPED".format(
                fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
            nas_skip += 1
            continue

        # close polyline and create planar face
        validated.append(validated[0])
        boundary = PolylineCurve([Point3d(v.X, v.Y, v.Z) for v in validated])

        # collect inner loops (window openings) translated to offset plane
        offset_vec = Vector3d(-normal.X * offset_dist,
                               -normal.Y * offset_dist,
                               -normal.Z * offset_dist)
        all_curves = [boundary]
        inner_count = 0
        for li in range(face.Loops.Count):
            lp = face.Loops[li]
            if lp.LoopType == BrepLoopType.Outer:
                continue
            inner_3d = lp.To3dCurve()
            if inner_3d is None:
                continue
            inner_3d = _doc_translate(inner_3d, offset_vec.X, offset_vec.Y, offset_vec.Z)
            all_curves.append(inner_3d)
            inner_count += 1

        face_breps = Brep.CreatePlanarBreps(all_curves, tol)
        if not face_breps or len(face_breps) == 0:
            face_breps = Brep.CreatePlanarBreps(all_curves, tol * 100)
        if face_breps and len(face_breps) > 0:
            neutral_faces.append(face_breps[0])
            inner_str = " +{} holes".format(inner_count) if inner_count > 0 else ""
            print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → {} validated{} → OK".format(
                fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback,
                len(vertices), len(validated) - 1, inner_str))
            nas_ok += 1
        else:
            # last resort: try without inner loops
            face_breps = Brep.CreatePlanarBreps([boundary], tol * 100)
            if face_breps and len(face_breps) > 0:
                neutral_faces.append(face_breps[0])
                print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → OK (no holes)".format(
                    fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
                nas_ok += 1
            else:
                print("  {}: {} trims, {} edge_lines ({}b/{}p/{}f) → {} verts → CreatePlanarBreps FAILED".format(
                    fl, n_trims, len(edge_lines), n_bend, n_perim, n_fallback, len(vertices)))
                nas_skip += 1

    print("  result: {} of {} faces OK".format(nas_ok, nas_ok + nas_skip))

    if not neutral_faces:
        print("error: could not create any neutral axis faces")
        return None

    # step 4: join all faces into a polysurface
    if len(neutral_faces) == 1:
        result = neutral_faces[0]
    else:
        joined = Brep.JoinBreps(neutral_faces, tol)
        if joined and len(joined) == 1:
            result = joined[0]
        elif joined and len(joined) > 1:
            # faces didn't all join — try looser tolerance
            joined2 = Brep.JoinBreps(joined, tol * 10)
            if joined2 and len(joined2) == 1:
                result = joined2[0]
            else:
                # merge into one brep
                pieces = list(joined2) if joined2 else list(joined)
                result = pieces[0]
                for pi in range(1, len(pieces)):
                    result.Join(pieces[pi], tol * 10, True)
                print("warning: neutral axis joined with loose tolerance ({} pieces)".format(
                    len(pieces)))
        else:
            print("warning: could not join neutral axis faces")
            result = neutral_faces[0]

    return result


def identify_bends(ref_side):
    """find bends in the reference polysurface by looking for non-smooth
    internal edges where adjacent faces meet at a non-trivial angle.
    returns list of bend info dicts."""
    bends = []

    for ei in range(ref_side.Edges.Count):
        edge = ref_side.Edges[ei]
        adj = edge.AdjacentFaces()
        if len(adj) != 2:
            continue  # naked edge = perimeter, not a bend

        fa, fb = adj[0], adj[1]

        # get face normals
        face_a = ref_side.Faces[fa]
        face_b = ref_side.Faces[fb]

        amp_a = AreaMassProperties.Compute(face_a)
        amp_b = AreaMassProperties.Compute(face_b)
        if amp_a is None or amp_b is None:
            continue

        ca = amp_a.Centroid
        cb = amp_b.Centroid

        rc_a, ua, va = face_a.ClosestPoint(ca)
        rc_b, ub, vb = face_b.ClosestPoint(cb)
        if not rc_a or not rc_b:
            continue

        na = face_a.NormalAt(ua, va)
        nb = face_b.NormalAt(ub, vb)
        if face_a.OrientationIsReversed:
            na = -na
        if face_b.OrientationIsReversed:
            nb = -nb

        dot = Vector3d.Multiply(na, nb)
        dot = max(-1.0, min(1.0, dot))

        if dot > 0.99:
            continue  # faces are coplanar, not a bend

        angle_between = math.degrees(math.acos(dot))
        included_angle = round(180.0 - angle_between, 1)

        bend_curve = edge.DuplicateCurve()
        mid_t = bend_curve.Domain.Mid
        mid_pt = bend_curve.PointAt(mid_t)
        tangent = bend_curve.TangentAt(mid_t)

        bends.append({
            "curve_3d": bend_curve,
            "angle": included_angle,
            "direction": "UP",  # refined later
            "normal_a": na,
            "normal_b": nb,
            "centroid_a": ca,
            "centroid_b": cb,
            "mid_pt": mid_pt,
            "tangent": tangent,
        })

    return bends


def project_bends_to_neutral_axis(bend_infos, neutral_axis_brep):
    """find bend lines on the neutral axis surface by extracting internal edges
    (edges shared by 2 faces = bend seams). matches each bend to its nearest
    internal edge and simplifies to a straight LineCurve.
    updates each bend_info with 'curve_na' key."""
    # collect internal (non-naked) edges — these are the bend seams
    internal_edges = []
    for ei in range(neutral_axis_brep.Edges.Count):
        edge = neutral_axis_brep.Edges[ei]
        if len(edge.AdjacentFaces()) == 2:
            internal_edges.append(edge)

    for info in bend_infos:
        bend_mid = info["mid_pt"]
        best_edge = None
        best_dist = float("inf")
        for edge in internal_edges:
            cp = edge.PointAt(edge.Domain.Mid)
            d = bend_mid.DistanceTo(cp)
            if d < best_dist:
                best_dist = d
                best_edge = edge

        if best_edge is not None:
            # simplify to a straight line (endpoints only)
            info["curve_na"] = LineCurve(Line(best_edge.PointAtStart, best_edge.PointAtEnd))
        else:
            info["curve_na"] = info["curve_3d"]  # fallback


def determine_bend_directions(bend_infos, picked_normal):
    """compute UP/DN direction for each bend relative to the picked face normal.
    uses face centroid positions: vectors from bend edge midpoint to each
    adjacent face centroid sum to a vector pointing toward the concave/inside
    of the bend. if inside faces picked_normal → UP (folds toward picked face)."""
    for info in bend_infos:
        mid = info["mid_pt"]
        ca = info["centroid_a"]
        cb = info["centroid_b"]

        # sum of vectors from bend edge to face centroids → points to inside
        inside_vec = (ca - mid) + (cb - mid)
        inside_vec.Unitize()

        dot = Vector3d.Multiply(inside_vec, picked_normal)
        info["direction"] = "UP" if dot > 0 else "DN"


def find_ink_curves(brep):
    """find curves on '09 - Ink lines' layer that are associated with the brep.
    returns list of (curve_guid, curve_geometry)."""
    ink_layer = "09 - Ink lines"
    if not rs.IsLayer(ink_layer):
        return []

    tol = sc.doc.ModelAbsoluteTolerance * 10
    result = []

    all_objects = sc.doc.Objects.FindByLayer(ink_layer)
    if all_objects is None:
        return []

    for obj in all_objects:
        if obj.ObjectType != ObjectType.Curve:
            continue
        crv = obj.Geometry
        if crv is None:
            continue

        is_associated = False
        for t_param in [0.0, 0.25, 0.5, 0.75, 1.0]:
            domain = crv.Domain
            t = domain.T0 + t_param * (domain.T1 - domain.T0)
            pt = crv.PointAt(t)
            closest_pt = brep.ClosestPoint(pt)
            if pt.DistanceTo(closest_pt) < tol:
                is_associated = True
                break

        if is_associated:
            result.append((obj.Id, crv.DuplicateCurve()))

    return result


def ensure_sublayers():
    """ensure 11 - 2D geo sublayers exist. returns dict of layer names."""
    parent = "11 - 2D geo"
    sublayers = {
        "outside": "{}::Outside cut".format(parent),
        "inside": "{}::Inside cut".format(parent),
        "mark": "{}::Mark".format(parent),
    }
    colors = {
        "outside": System.Drawing.Color.Blue,
        "inside": System.Drawing.Color.FromArgb(255, 0, 255),
        "mark": System.Drawing.Color.FromArgb(0, 127, 0),
    }

    if not rs.IsLayer(parent):
        rs.AddLayer(parent)

    for key, name in sublayers.items():
        if not rs.IsLayer(name):
            rs.AddLayer(name, colors[key])

    return sublayers


def unroll_neutral_axis(neutral_axis_brep, ink_curves, bend_infos):
    """unroll the neutral axis surface with following geometry.
    returns (unrolled_breps, unrolled_ink_curves, unrolled_bend_curves) or None."""
    unroller = Unroller(neutral_axis_brep)

    num_ink = len(ink_curves)
    num_bend = len(bend_infos)

    for _, crv in ink_curves:
        unroller.AddFollowingGeometry(crv)

    for info in bend_infos:
        unroller.AddFollowingGeometry(info["curve_na"])

    unrolled_breps, out_curves, out_points, out_dots = unroller.PerformUnroll()

    if unrolled_breps is None or len(unrolled_breps) == 0:
        print("error: unroll failed")
        return None

    out_curve_list = list(out_curves) if out_curves else []
    unrolled_ink = out_curve_list[:num_ink]
    unrolled_bend = out_curve_list[num_ink:num_ink + num_bend]

    return unrolled_breps, unrolled_ink, unrolled_bend


def classify_unrolled_curves(unrolled_breps):
    """classify boundary curves from unrolled breps into outside cut and inside cut.
    returns (outside_curves, inside_curves)."""
    outside = []
    inside = []

    for brp in unrolled_breps:
        naked = brp.DuplicateNakedEdgeCurves(True, True)
        if naked is None or len(naked) == 0:
            continue

        tol = sc.doc.ModelAbsoluteTolerance
        joined = Curve.JoinCurves(naked, tol)
        if joined is None or len(joined) == 0:
            continue

        if len(joined) == 1:
            outside.extend(joined)
        else:
            areas = []
            for crv in joined:
                amp = AreaMassProperties.Compute(crv)
                area = amp.Area if amp else 0
                areas.append((area, crv))
            areas.sort(key=lambda x: x[0], reverse=True)

            outside.append(areas[0][1])
            for _, crv in areas[1:]:
                inside.append(crv)

    return outside, inside


def create_bend_text_curves(bend_infos, unrolled_bend_curves):
    """create text as curve geometry for bend angle annotations.
    returns list of curves."""
    all_text_curves = []

    if len(unrolled_bend_curves) != len(bend_infos):
        print("warning: bend curve count mismatch ({} vs {})".format(
            len(unrolled_bend_curves), len(bend_infos)))
        return all_text_curves

    for i, crv in enumerate(unrolled_bend_curves):
        info = bend_infos[i]
        angle = info["angle"]
        direction = info["direction"]

        angle_int = int(round(angle))
        text_content = "{} {}".format(angle_int, direction)

        mid_t = crv.Domain.Mid
        mid_pt = crv.PointAt(mid_t)
        tangent = crv.TangentAt(mid_t)
        tangent.Unitize()

        # perpendicular in XY plane (unrolled is flat)
        perp = Vector3d(-tangent.Y, tangent.X, 0)
        perp.Unitize()

        text_origin = mid_pt + perp * TEXT_HEIGHT * 2

        text_plane = Plane(text_origin, tangent, perp)

        ds = sc.doc.DimStyles.Current
        te = TextEntity.Create(text_content, text_plane, ds, False, 0, 0)
        if te is None:
            continue

        te.TextHeight = TEXT_HEIGHT
        # use Mecsoft_Font-1 (CNC single-stroke font)
        mecsoft = Rhino.DocObjects.Font.FromQuartetProperties(
            "Mecsoft_Font-1", False, False)
        if mecsoft is not None:
            te.Font = mecsoft

        curves = te.CreateCurves(ds, False)
        if curves and len(curves) > 0:
            all_text_curves.extend(curves)

    return all_text_curves


def add_output(neutral_axis, unrolled_breps, outside_curves, inside_curves,
               unrolled_bend, unrolled_ink, text_curves, sublayers,
               brep=None, picked_face_index=0):
    """add 2D curves to fabrication sublayers, aligned to the neutral axis
    face plane. the Unroller flattens to an arbitrary plane — we rotate
    the output normal onto the NA face normal (preserving in-plane layout),
    then translate to match centroids. applied via sc.doc.Objects.Transform
    (the only transform method that works reliably in CPython 3)."""
    # compute alignment: PlaneToPlane from unrolled face to matching NA face
    # the Unroller preserves face order, so NA face[i] → unrolled face[i]
    align_xform = None
    if unrolled_breps and len(unrolled_breps) > 0 and brep is not None:
        print("=== alignment ===")
        # find NA face closest to picked face
        picked_centroid, _ = get_face_outward_normal(brep, picked_face_index)
        best_na_idx = 0
        best_na_dist = float("inf")
        if picked_centroid is not None:
            for nfi in range(neutral_axis.Faces.Count):
                nf_brep = neutral_axis.Faces[nfi].DuplicateFace(False)
                nf_amp = AreaMassProperties.Compute(nf_brep)
                if nf_amp is not None:
                    d = picked_centroid.DistanceTo(nf_amp.Centroid)
                    if d < best_na_dist:
                        best_na_dist = d
                        best_na_idx = nfi

        # source: matching unrolled face
        uf_idx = min(best_na_idx, unrolled_breps[0].Faces.Count - 1)
        unrolled_face = unrolled_breps[0].Faces[uf_idx]
        uf_brep = unrolled_face.DuplicateFace(False)
        amp_uf = AreaMassProperties.Compute(uf_brep)
        # target: matching NA face
        na_face = neutral_axis.Faces[best_na_idx]
        na_brep = na_face.DuplicateFace(False)
        amp_na = AreaMassProperties.Compute(na_brep)

        if amp_uf and amp_na:
            uf_centroid = amp_uf.Centroid
            na_centroid = amp_na.Centroid
            plane_tol = max(sc.doc.ModelAbsoluteTolerance * 100, 0.1)
            rc_uf_plane, uf_plane = unrolled_face.TryGetPlane(plane_tol)
            rc_na_plane, na_plane = na_face.TryGetPlane(plane_tol)
            if rc_uf_plane and rc_na_plane:
                uf_plane.Origin = uf_centroid
                na_plane.Origin = na_centroid
                align_xform = Transform.PlaneToPlane(uf_plane, na_plane)
                print("  picked face {} → NA face {} (dist={:.4f}\") → PlaneToPlane".format(
                    picked_face_index, best_na_idx, best_na_dist))

    count = 0
    outside_idx = sc.doc.Layers.FindByFullPath(sublayers["outside"], -1)
    inside_idx = sc.doc.Layers.FindByFullPath(sublayers["inside"], -1)
    mark_idx = sc.doc.Layers.FindByFullPath(sublayers["mark"], -1)

    attr_outside = Rhino.DocObjects.ObjectAttributes()
    attr_outside.LayerIndex = outside_idx

    attr_inside = Rhino.DocObjects.ObjectAttributes()
    attr_inside.LayerIndex = inside_idx

    attr_mark = Rhino.DocObjects.ObjectAttributes()
    attr_mark.LayerIndex = mark_idx

    guids = []

    def _add(crv, attr):
        guid = sc.doc.Objects.AddCurve(crv, attr)
        if guid != System.Guid.Empty:
            if align_xform is not None:
                # Transform returns new guid (deletes old)
                new_guid = sc.doc.Objects.Transform(guid, align_xform, True)
                guids.append(new_guid)
            else:
                guids.append(guid)
            return 1
        return 0

    for crv in outside_curves:
        count += _add(crv, attr_outside)

    for crv in inside_curves:
        count += _add(crv, attr_inside)

    for crv in unrolled_bend:
        count += _add(crv, attr_mark)

    for crv in unrolled_ink:
        count += _add(crv, attr_mark)

    for crv in text_curves:
        count += _add(crv, attr_mark)

    # group all output
    if guids:
        group_idx = sc.doc.Groups.Add()
        for g in guids:
            sc.doc.Groups.AddToGroup(group_idx, g)

    return count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def unfold_to_2d():
    # step 1: select part and pick face
    result = pick_part_and_face()
    if result is None:
        return
    brep, face_index, obj_id = result
    print("picked face: index {}".format(face_index))

    _, picked_normal = get_face_outward_normal(brep, face_index)
    if picked_normal is None:
        print("error: could not compute face normal")
        return

    # step 2: detect thickness
    auto_thickness = detect_thickness(brep, face_index)
    thickness = prompt_thickness(auto_thickness)
    if thickness is None:
        return
    print("thickness: {}".format(thickness))

    # step 3: classify faces
    print("=== face classification ===")
    sheet_faces, edge_faces, partners = classify_faces(brep, thickness)
    print("  {} sheet faces, {} edge faces".format(len(sheet_faces), len(edge_faces)))

    # print compact partner list
    seen_pairs = set()
    pair_strs = []
    for fi in sorted(partners.keys()):
        pair = tuple(sorted([fi, partners[fi]]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            pair_strs.append("{}↔{}".format(pair[0], pair[1]))
    print("  partners: {}".format(", ".join(pair_strs)))

    # add face index text dots for visual identification
    sheet_set = set(sheet_faces)
    dot_ids = []
    for i in range(brep.Faces.Count):
        centroid, _ = get_face_outward_normal(brep, i)
        if centroid is None:
            continue
        dot = Rhino.Geometry.TextDot(str(i), centroid)
        dot.FontHeight = 14
        attr = Rhino.DocObjects.ObjectAttributes()
        if i in sheet_set:
            attr.ObjectColor = System.Drawing.Color.FromArgb(0, 180, 0)
        else:
            attr.ObjectColor = System.Drawing.Color.FromArgb(220, 0, 0)
        attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
        dot_ids.append(sc.doc.Objects.AddTextDot(dot, attr))
    if dot_ids:
        group_idx = sc.doc.Groups.Add("unfold_face_dots")
        for did in dot_ids:
            sc.doc.Groups.AddToGroup(group_idx, did)
    sc.doc.Views.Redraw()

    if len(sheet_faces) < 2:
        print("error: need at least 2 sheet faces")
        return

    # step 4: join sheet faces -> 2 polysurfaces (graph-colored by partner pairs)
    # ref_side contains the picked face, other_side is the opposite
    ref_side, other_side = join_sheet_faces(brep, sheet_faces, partners, face_index)
    if ref_side is None:
        return

    print("  ref side: {} faces, other side: {} faces".format(
        ref_side.Faces.Count, other_side.Faces.Count))

    # step 6: construct neutral axis (prints its own === header ===)
    neutral_axis = construct_neutral_axis(ref_side, thickness, original_brep=brep)
    if neutral_axis is None:
        return
    print("  neutral axis: {} faces".format(neutral_axis.Faces.Count))

    # debug: bake NAS individual faces for visual inspection
    nas_attr = Rhino.DocObjects.ObjectAttributes()
    nas_attr.ObjectColor = System.Drawing.Color.FromArgb(0, 200, 200)
    nas_attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
    nas_dot_ids = []
    for nfi in range(neutral_axis.Faces.Count):
        dup = neutral_axis.Faces[nfi].DuplicateFace(False)
        if dup is not None:
            nas_dot_ids.append(sc.doc.Objects.AddBrep(dup, nas_attr))
    if nas_dot_ids:
        grp = sc.doc.Groups.Add("unfold_nas_debug")
        for nid in nas_dot_ids:
            sc.doc.Groups.AddToGroup(grp, nid)
    print("  NAS debug: {} individual faces baked (cyan)".format(len(nas_dot_ids)))
    sc.doc.Views.Redraw()

    # step 7: identify bends
    print("=== bends ===")
    bend_infos = identify_bends(ref_side)
    print("  {} bends found".format(len(bend_infos)))

    # step 8: project bend lines to neutral axis
    project_bends_to_neutral_axis(bend_infos, neutral_axis)

    # step 9: compute bend directions
    determine_bend_directions(bend_infos, picked_normal)
    for info in bend_infos:
        print("  bend: {:.1f} {}".format(info["angle"], info["direction"]))

    # step 10: find ink curves
    ink_curves = find_ink_curves(brep)
    print("ink curves: {}".format(len(ink_curves)))

    # step 11: unroll
    print("=== unroll ===")
    unroll_result = unroll_neutral_axis(neutral_axis, ink_curves, bend_infos)
    if unroll_result is None:
        return
    unrolled_breps, unrolled_ink, unrolled_bend = unroll_result
    print("  {} brep(s), {} ink, {} bend lines".format(
        len(unrolled_breps), len(unrolled_ink), len(unrolled_bend)))

    # step 12: classify unrolled boundary curves
    outside_curves, inside_curves = classify_unrolled_curves(unrolled_breps)
    print("  cuts: {} outside, {} inside".format(len(outside_curves), len(inside_curves)))

    # step 13: create bend angle text
    text_curves = create_bend_text_curves(bend_infos, unrolled_bend)

    # step 14: add curves to sublayers
    sublayers = ensure_sublayers()
    count = add_output(neutral_axis, unrolled_breps, outside_curves,
                       inside_curves, unrolled_bend, unrolled_ink,
                       text_curves, sublayers,
                       brep=brep, picked_face_index=face_index)

    sc.doc.Views.Redraw()
    print("unfold complete: {} curves placed".format(count))


if __name__ == "__main__":
    unfold_to_2d()
