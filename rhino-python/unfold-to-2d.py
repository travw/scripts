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
PLACEMENT_GAP_FACTOR = 1.5  # multiplier on part width for offset


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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
    returns (sheet_face_indices, edge_face_indices)."""
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
    return sheet_faces, edge_faces


def join_sheet_faces(brep, sheet_faces):
    """join all sheet faces into two polysurfaces representing both sides
    of the aluminum sheet. returns (side_a, side_b) or (None, None) on failure."""
    tol = sc.doc.ModelAbsoluteTolerance
    face_breps = []
    for fi in sheet_faces:
        dup = brep.Faces[fi].DuplicateFace(False)
        if dup is not None:
            face_breps.append(dup)

    if len(face_breps) < 2:
        print("error: not enough sheet faces to join ({})".format(len(face_breps)))
        return None, None

    joined = Brep.JoinBreps(face_breps, tol)
    if joined is None or len(joined) != 2:
        count = len(joined) if joined else 0
        print("error: expected 2 joined surfaces (both sides of sheet), got {}".format(count))
        return None, None

    return joined[0], joined[1]


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


def construct_neutral_axis(ref_side, thickness):
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

        # offset plane inward (opposite to outward normal)
        offset_origin = plane.Origin - normal * offset_dist
        face_planes[fi] = Plane(offset_origin, plane.XAxis, plane.YAxis)
        face_normals[fi] = normal

    if len(face_planes) < 1:
        print("error: no planar faces found in ref_side")
        return None

    # step 2: build boundary vertices for each neutral axis face
    # compute vertices by intersecting adjacent neutral-axis edge lines
    neutral_faces = []
    for fi in face_planes:
        normal = face_normals[fi]
        face = ref_side.Faces[fi]

        # get edges in boundary order from the outer loop
        loop = face.OuterLoop
        if loop is None:
            continue

        edge_lines = []
        for trim in loop.Trims:
            if trim.Edge is None:
                continue
            edge = trim.Edge
            adj = edge.AdjacentFaces()

            offset_vec = Vector3d(-normal.X * offset_dist,
                                   -normal.Y * offset_dist,
                                   -normal.Z * offset_dist)

            if len(adj) == 2:
                other = adj[0] if adj[1] == fi else adj[1]
                if other in face_planes:
                    # bend edge: plane-plane intersection (infinite line)
                    rc, int_line = Intersection.PlanePlane(face_planes[fi], face_planes[other])
                    if rc:
                        edge_lines.append(int_line)
                else:
                    # edge adjacent to non-sheet face (bend radius): translate like perimeter
                    p0 = edge.PointAtStart + offset_vec
                    p1 = edge.PointAtEnd + offset_vec
                    edge_lines.append(Line(p0, p1))
            else:
                # perimeter edge: translate to offset plane
                p0 = edge.PointAtStart + offset_vec
                p1 = edge.PointAtEnd + offset_vec
                edge_lines.append(Line(p0, p1))

        if len(edge_lines) < 3:
            print("warning: face {} has only {} edge lines".format(fi, len(edge_lines)))
            continue

        # compute vertices by intersecting adjacent edge lines
        n = len(edge_lines)
        vertices = []
        for i in range(n):
            line_a = edge_lines[i]
            line_b = edge_lines[(i + 1) % n]
            rc, ta, tb = Intersection.LineLine(line_a, line_b)
            if rc:
                vertices.append(Point3d(line_a.PointAt(ta)))

        if len(vertices) < 3:
            print("warning: face {} has only {} vertices".format(fi, len(vertices)))
            continue

        # close the polyline and create planar face
        vertices.append(vertices[0])
        boundary = PolylineCurve([Point3d(v.X, v.Y, v.Z) for v in vertices])

        # collect inner loops (window openings) translated to offset plane
        all_curves = [boundary]
        for li in range(face.Loops.Count):
            lp = face.Loops[li]
            if lp.LoopType == BrepLoopType.Outer:
                continue
            inner_3d = lp.To3dCurve()
            if inner_3d is None:
                continue
            inner_3d.Translate(offset_vec)
            all_curves.append(inner_3d)

        face_breps = Brep.CreatePlanarBreps(all_curves, tol)
        if face_breps and len(face_breps) > 0:
            neutral_faces.append(face_breps[0])
        else:
            # retry without inner loops if holes cause failure
            face_breps = Brep.CreatePlanarBreps([boundary], tol)
            if face_breps and len(face_breps) > 0:
                print("warning: face {} inner loops failed, using solid face".format(fi))
                neutral_faces.append(face_breps[0])
            else:
                print("warning: CreatePlanarBreps failed for face {}".format(fi))

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
                pieces = joined2 if joined2 else joined
                result = pieces[0]
                for extra in pieces[1:]:
                    result.Join(extra, tol * 10, True)
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
    uses the bisector of the two face normals: it points toward the convex
    (outside) of the bend. if convex side faces picked_normal → UP."""
    for info in bend_infos:
        na = info["normal_a"]
        nb = info["normal_b"]

        bisector = na + nb
        bisector.Unitize()

        dot = Vector3d.Multiply(bisector, picked_normal)
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
        naked = brp.DuplicateNakedEdgeCurves(True, False)
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

        curves = te.CreateCurves(ds, False)
        if curves and len(curves) > 0:
            all_text_curves.extend(curves)

    return all_text_curves


def place_2d_output(brep_3d, unrolled_breps, outside_curves, inside_curves,
                     unrolled_ink, unrolled_bend, text_curves, sublayers):
    """place all 2D output on the correct layers, offset from the 3D part."""
    bb_3d = brep_3d.GetBoundingBox(True)
    bb_width = bb_3d.Max.X - bb_3d.Min.X

    all_2d_geo = []
    all_2d_geo.extend(unrolled_breps)
    for c in outside_curves:
        all_2d_geo.append(c)

    if not all_2d_geo:
        print("error: no 2D geometry to place")
        return 0

    bb_2d = all_2d_geo[0].GetBoundingBox(True)
    for geo in all_2d_geo[1:]:
        bb_2d.Union(geo.GetBoundingBox(True))

    offset_x = bb_3d.Max.X + bb_width * PLACEMENT_GAP_FACTOR - bb_2d.Min.X
    offset_y = (bb_3d.Min.Y + bb_3d.Max.Y) / 2 - (bb_2d.Min.Y + bb_2d.Max.Y) / 2
    offset_z = -bb_2d.Min.Z

    xform = Transform.Translation(offset_x, offset_y, offset_z)

    count = 0
    outside_layer = sc.doc.Layers.FindByFullPath(sublayers["outside"], -1)
    inside_layer = sc.doc.Layers.FindByFullPath(sublayers["inside"], -1)
    mark_layer = sc.doc.Layers.FindByFullPath(sublayers["mark"], -1)

    attr_outside = Rhino.DocObjects.ObjectAttributes()
    attr_outside.LayerIndex = outside_layer

    attr_inside = Rhino.DocObjects.ObjectAttributes()
    attr_inside.LayerIndex = inside_layer

    attr_mark = Rhino.DocObjects.ObjectAttributes()
    attr_mark.LayerIndex = mark_layer

    for crv in outside_curves:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_outside) != System.Guid.Empty:
            count += 1

    for crv in inside_curves:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_inside) != System.Guid.Empty:
            count += 1

    for crv in unrolled_bend:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_mark) != System.Guid.Empty:
            count += 1

    for crv in unrolled_ink:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_mark) != System.Guid.Empty:
            count += 1

    for crv in text_curves:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_mark) != System.Guid.Empty:
            count += 1

    return count


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def unfold_to_2d():
    # step 1-2: select part and pick face
    result = pick_part_and_face()
    if result is None:
        return
    brep, face_index, obj_id = result

    _, picked_normal = get_face_outward_normal(brep, face_index)
    if picked_normal is None:
        print("error: could not compute face normal")
        return

    print("picked face: index {}".format(face_index))

    # step 3: detect thickness
    auto_thickness = detect_thickness(brep, face_index)
    thickness = prompt_thickness(auto_thickness)
    if thickness is None:
        return
    print("thickness: {}".format(thickness))

    # step 4: classify faces
    sheet_faces, edge_faces = classify_faces(brep, thickness)
    print("faces: {} sheet, {} edge".format(len(sheet_faces), len(edge_faces)))

    if len(sheet_faces) < 2:
        print("error: need at least 2 sheet faces")
        return

    # step 5: join sheet faces → 2 polysurfaces
    side_a, side_b = join_sheet_faces(brep, sheet_faces)
    if side_a is None:
        return
    print("joined sheet faces: side A ({} faces), side B ({} faces)".format(
        side_a.Faces.Count, side_b.Faces.Count))

    # step 6: identify reference side (contains picked face)
    ref_side, other_side = identify_reference_side(side_a, side_b, brep, face_index)

    # step 7: construct neutral axis (plane geometry for sharp corners)
    neutral_axis = construct_neutral_axis(ref_side, thickness)
    if neutral_axis is None:
        return
    print("neutral axis: {} faces".format(neutral_axis.Faces.Count))

    # step 8: identify bends from reference polysurface
    bend_infos = identify_bends(ref_side)
    print("bends: {}".format(len(bend_infos)))

    # step 9: project bend lines to neutral axis
    project_bends_to_neutral_axis(bend_infos, neutral_axis)

    # step 10: compute bend directions
    determine_bend_directions(bend_infos, picked_normal)
    for info in bend_infos:
        print("  bend: {:.1f} {}".format(info["angle"], info["direction"]))

    # step 11: find ink curves
    ink_curves = find_ink_curves(brep)
    print("ink curves: {}".format(len(ink_curves)))

    # step 12: unroll
    unroll_result = unroll_neutral_axis(neutral_axis, ink_curves, bend_infos)
    if unroll_result is None:
        return
    unrolled_breps, unrolled_ink, unrolled_bend = unroll_result
    print("unrolled: {} brep(s), {} ink, {} bend lines".format(
        len(unrolled_breps), len(unrolled_ink), len(unrolled_bend)))

    # step 13: classify unrolled boundary curves
    outside_curves, inside_curves = classify_unrolled_curves(unrolled_breps)
    print("cuts: {} outside, {} inside".format(len(outside_curves), len(inside_curves)))

    # step 14: create bend angle text
    text_curves = create_bend_text_curves(bend_infos, unrolled_bend)

    # ensure sublayers exist
    sublayers = ensure_sublayers()

    # step 15: place 2D output
    count = place_2d_output(
        brep, unrolled_breps, outside_curves, inside_curves,
        unrolled_ink, unrolled_bend, text_curves, sublayers,
    )

    sc.doc.Views.Redraw()
    print("unfold complete: {} curves placed".format(count))


if __name__ == "__main__":
    unfold_to_2d()
