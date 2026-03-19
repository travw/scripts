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
    PointContainment,
    PointFaceRelation,
    PolylineCurve,
    TextEntity,
    Transform,
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


def _make_planar(curves, tol):
    """create planar brep from curves, trying normal then loose tolerance."""
    result = Brep.CreatePlanarBreps(curves, tol)
    if not result or len(result) == 0:
        result = Brep.CreatePlanarBreps(curves, tol * 100)
    if result and len(result) > 0:
        return result[0]
    return None


def _build_nas_boundary(face, fi, face_planes, face_normals, offset_dist,
                        ref_side, skipped_faces, tol, original_brep=None):
    """build the NAS face boundary by intersecting the offset plane with the
    original polysurface, then trimming at PP axes (bend lines).

    the NAP (neutral axis plane, offset t/2 from the face) is parallel to
    the sheet face and cuts through the edge/transition faces of the original
    polysurface. the intersection curves capture exact boundary geometry
    including notches and cutouts. trimming at PP axes (where adjacent NAPs
    meet) gives correct bend extents without overshoot.

    returns a closed Curve on the NAP, or None."""
    normal = face_normals[fi]
    offset_vec = Vector3d(-normal.X * offset_dist,
                           -normal.Y * offset_dist,
                           -normal.Z * offset_dist)
    nap_plane = face_planes[fi]

    # fallback: translated outer loop
    outer_crv = face.OuterLoop.To3dCurve()
    if outer_crv is None:
        return None
    fallback = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
    if fallback is not None and not fallback.IsClosed:
        fallback = None

    if original_brep is None:
        return fallback

    # step 1: intersect NAP with original polysurface
    rc, curves, points = Intersection.BrepPlane(original_brep, nap_plane, tol)
    if not rc or curves is None or len(curves) == 0:
        return fallback

    # step 1.5: compute face geometry for filtering and centroid
    face_brep = face.DuplicateFace(False)
    amp = AreaMassProperties.Compute(face_brep)
    if amp is None:
        return fallback
    centroid_nap = nap_plane.ClosestPoint(amp.Centroid)

    # filter intersection curves to those near this face (exclude distant faces
    # at the same elevation whose curves would create unwanted extensions)
    face_bb = face_brep.GetBoundingBox(True)
    face_bb.Inflate(offset_dist * 5)
    near_curves = [c for c in curves if face_bb.Contains(c.PointAt(c.Domain.Mid))]
    if near_curves:
        curves = near_curves

    # save raw curves for post-trim split (removes bridging artifacts)
    raw_curves_for_split = list(curves)

    # step 2: join intersection curves and find the closed loop for this face
    joined = Curve.JoinCurves(curves, tol * 10)
    if joined is None or len(joined) == 0:
        return fallback

    raw_loop = None
    for crv in joined:
        if not crv.IsClosed:
            continue
        contain = crv.Contains(centroid_nap, nap_plane, tol)
        if contain == PointContainment.Inside:
            if raw_loop is None:
                raw_loop = crv
            else:
                # pick largest loop containing centroid (outer boundary, not window holes)
                amp_new = AreaMassProperties.Compute(crv)
                amp_old = AreaMassProperties.Compute(raw_loop)
                if amp_new and amp_old and amp_new.Area > amp_old.Area:
                    raw_loop = crv
    if raw_loop is None:
        # fallback: closest closed loop
        best_d = float("inf")
        for crv in joined:
            if not crv.IsClosed:
                continue
            rc_cp, t_cp = crv.ClosestPoint(centroid_nap)
            if rc_cp:
                d = centroid_nap.DistanceTo(crv.PointAt(t_cp))
                if d < best_d:
                    best_d = d
                    raw_loop = crv
    if raw_loop is None:
        return fallback

    # step 3: compute PP axes for adjacent faces (bend lines)
    bend_map = {}  # target_fi -> Line (infinite PlanePlane line)
    for trim_obj in face.OuterLoop.Trims:
        edge = trim_obj.Edge
        if edge is None:
            continue
        adj = list(edge.AdjacentFaces())
        if len(adj) != 2:
            continue
        other = adj[0] if adj[1] == fi else adj[1]
        target = other
        if skipped_faces and other in skipped_faces:
            edge_mid = edge.PointAt(edge.Domain.Mid)
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
                target = best_fi
        if target == fi or target not in face_planes or target in (skipped_faces or set()):
            continue
        if target in bend_map:
            continue
        rc_pp, pp_line = Intersection.PlanePlane(face_planes[fi], face_planes[target])
        if rc_pp:
            bend_map[target] = pp_line

    if not bend_map:
        return raw_loop

    # step 4: trim raw loop at PP planes using Brep.Trim
    raw_breps = Brep.CreatePlanarBreps([raw_loop], tol)
    if not raw_breps or len(raw_breps) == 0:
        raw_breps = Brep.CreatePlanarBreps([raw_loop], tol * 100)
    if not raw_breps or len(raw_breps) == 0:
        return raw_loop

    trimmed_brep = raw_breps[0]
    for tgt, pp_line in bend_map.items():
        pp_dir = Vector3d(pp_line.Direction)
        pp_dir.Unitize()
        trim_normal = Vector3d.CrossProduct(pp_dir, nap_plane.Normal)
        trim_normal.Unitize()
        pp_mid = pp_line.PointAt(pp_line.ClosestParameter(centroid_nap))
        if Vector3d.Multiply(centroid_nap - pp_mid, trim_normal) < 0:
            trim_normal = -trim_normal
        trim_plane = Plane(pp_mid, trim_normal)

        # trim both orientations and pick the piece containing the centroid
        pieces_pos = trimmed_brep.Trim(trim_plane, tol)
        trim_plane_flip = Plane(pp_mid, -trim_normal)
        pieces_neg = trimmed_brep.Trim(trim_plane_flip, tol)
        all_pieces = list(pieces_pos or []) + list(pieces_neg or [])
        if all_pieces:
            best_piece = None
            best_d = float("inf")
            for piece in all_pieces:
                cp = piece.ClosestPoint(centroid_nap)
                d = centroid_nap.DistanceTo(cp)
                if d < best_d:
                    best_d = d
                    best_piece = piece
            if best_piece is not None:
                trimmed_brep = best_piece

    # step 4.5: split using raw intersection curves to remove bridging artifacts
    # pick largest piece by area (not closest to centroid — centroid may be in a hole)
    if raw_curves_for_split and trimmed_brep.Faces.Count > 0:
        split_brep = trimmed_brep.Faces[0].Split(raw_curves_for_split, tol)
        if split_brep is not None and split_brep.Faces.Count > 1:
            best_piece = None
            best_area = 0
            for fi_s in range(split_brep.Faces.Count):
                piece = split_brep.Faces[fi_s].DuplicateFace(False)
                amp_s = AreaMassProperties.Compute(piece)
                if amp_s and amp_s.Area > best_area:
                    best_area = amp_s.Area
                    best_piece = piece
            if best_piece is not None:
                trimmed_brep = best_piece

    # step 5: extract boundary, snap bend edges to PP lines, project to NAP
    if trimmed_brep.Faces.Count == 0:
        return raw_loop
    boundary = trimmed_brep.Faces[0].OuterLoop.To3dCurve()
    if boundary is None or not boundary.IsClosed:
        return raw_loop

    # convert boundary to polyline points for snapping
    polyline_result = boundary.TryGetPolyline()
    if polyline_result[0]:
        pts = list(polyline_result[1])
    else:
        # approximate as polyline
        poly = boundary.ToPolyline(tol, tol, 0.01, 10000)
        if poly and poly.TryGetPolyline()[0]:
            pts = list(poly.TryGetPolyline()[1])
        else:
            # can't convert — just project boundary to NAP and return
            return boundary

    if len(pts) < 3:
        return boundary

    # snap bend-edge segments to PP lines: only segments that are both
    # near AND parallel to a PP line. corner/notch segments at angles are
    # preserved even if they're close to a PP line.
    for i in range(len(pts) - 1):
        seg_dir = Vector3d(pts[i + 1].X - pts[i].X,
                           pts[i + 1].Y - pts[i].Y,
                           pts[i + 1].Z - pts[i].Z)
        seg_len = seg_dir.Length
        if seg_len < tol:
            continue
        seg_dir.Unitize()
        mid = Point3d((pts[i].X + pts[i + 1].X) / 2,
                      (pts[i].Y + pts[i + 1].Y) / 2,
                      (pts[i].Z + pts[i + 1].Z) / 2)
        for tgt, pp_line in bend_map.items():
            t = pp_line.ClosestParameter(mid)
            dist = mid.DistanceTo(pp_line.PointAt(t))
            if dist > tol * 10:
                continue
            # check parallelism: segment must be nearly parallel to PP line
            pp_dir = Vector3d(pp_line.Direction)
            pp_dir.Unitize()
            dot = abs(Vector3d.Multiply(seg_dir, pp_dir))
            if dot < 0.999:
                continue  # angled segment (corner/notch) — don't snap
            # material check: verify adjacent face has material at this location
            adj_face = ref_side.Faces[tgt]
            adj_loop = adj_face.OuterLoop.To3dCurve()
            if adj_loop is not None:
                adj_normal = face_normals[tgt]
                adj_ov = Vector3d(-adj_normal.X * offset_dist,
                                   -adj_normal.Y * offset_dist,
                                   -adj_normal.Z * offset_dist)
                adj_loop_nap = _doc_translate(adj_loop, adj_ov.X, adj_ov.Y, adj_ov.Z)
                adj_plane = face_planes[tgt]
                mid_on_adj = adj_plane.ClosestPoint(mid)
                if adj_loop_nap and adj_loop_nap.IsClosed:
                    contain = adj_loop_nap.Contains(mid_on_adj, adj_plane, tol)
                    if contain != PointContainment.Inside:
                        continue  # no material on adjacent face here — don't snap
            # snap both endpoints to PP line
            t0 = pp_line.ClosestParameter(pts[i])
            t1 = pp_line.ClosestParameter(pts[i + 1])
            pts[i] = pp_line.PointAt(t0)
            pts[i + 1] = pp_line.PointAt(t1)
            break

    # project ALL points to NAP for exact planarity
    for i in range(len(pts)):
        pts[i] = nap_plane.ClosestPoint(pts[i])

    # ensure closure
    pts[-1] = Point3d(pts[0].X, pts[0].Y, pts[0].Z)

    # remove consecutive duplicates and short segments (trim corner artifacts)
    cleaned = [pts[0]]
    for p in pts[1:]:
        if p.DistanceTo(cleaned[-1]) > offset_dist * 0.5:
            cleaned.append(p)
    if len(cleaned) > 1 and cleaned[-1].DistanceTo(cleaned[0]) < offset_dist * 0.5:
        cleaned.pop()
    if len(cleaned) < 3:
        return boundary
    cleaned.append(Point3d(cleaned[0].X, cleaned[0].Y, cleaned[0].Z))

    return PolylineCurve(cleaned)


def construct_neutral_axis(ref_side, thickness, original_brep=None, other_side=None,
                           partners=None):
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

        # verify offset direction: should point toward partner face
        direction_set = False
        if partners is not None and original_brep is not None:
            # use partner face centroid from original brep (most robust)
            orig_fi = None
            # find original face index for this ref_side face
            face_brep_c = face.DuplicateFace(False)
            amp_c = AreaMassProperties.Compute(face_brep_c)
            if amp_c is not None:
                best_oi = -1
                best_od = float("inf")
                for oi in range(original_brep.Faces.Count):
                    oc, _ = get_face_outward_normal(original_brep, oi)
                    if oc is None:
                        continue
                    d = amp_c.Centroid.DistanceTo(oc)
                    if d < best_od:
                        best_od = d
                        best_oi = oi
                if best_oi >= 0 and best_od < 1.0:
                    orig_fi = best_oi
            if orig_fi is not None and orig_fi in partners:
                partner_fi = partners[orig_fi]
                partner_centroid, _ = get_face_outward_normal(
                    original_brep, partner_fi)
                if partner_centroid is not None:
                    to_partner = partner_centroid - centroid
                    if Vector3d.Multiply(to_partner, normal) > 0:
                        normal = -normal  # normal was pointing toward partner (inward), flip to outward
                    direction_set = True
        if not direction_set:
            if other_side is not None:
                test_a = centroid - normal * offset_dist
                test_b = centroid + normal * offset_dist
                dist_a = test_a.DistanceTo(other_side.ClosestPoint(test_a))
                dist_b = test_b.DistanceTo(other_side.ClosestPoint(test_b))
                if dist_b < dist_a:
                    normal = -normal
            elif original_brep is not None:
                test_pt = centroid - normal * offset_dist
                if not original_brep.IsPointInside(test_pt, tol, False):
                    normal = -normal

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

        # build NAS face boundary via BrepPlane intersection + PP trimming
        boundary = _build_nas_boundary(face, fi, face_planes, face_normals,
                                        offset_dist, ref_side, skipped_faces, tol,
                                        original_brep=original_brep)

        if boundary is None:
            print("  {}: boundary construction failed → SKIPPED".format(fl))
            nas_skip += 1
            continue

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

        face_brep = _make_planar(all_curves, tol)
        if face_brep is None and inner_count > 0:
            # retry without inner loops
            face_brep = _make_planar([boundary], tol)
            if face_brep is not None:
                inner_count = 0

        if face_brep is not None:
            neutral_faces.append(face_brep)
            inner_str = " +{} holes".format(inner_count) if inner_count > 0 else ""
            print("  {}: {} trims{} → OK".format(
                fl, face.OuterLoop.Trims.Count, inner_str))
            nas_ok += 1
        else:
            print("  {}: {} trims → CreatePlanarBreps FAILED".format(
                fl, face.OuterLoop.Trims.Count))
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
    """match each bend to its two adjacent NAS faces using normal comparison,
    then compute the bend axis from plane-plane intersection.
    updates each bend_info with 'curve_na', 'na_face_a', 'na_face_b',
    and 'na_axis' keys."""
    nas = neutral_axis_brep

    # pre-compute NAS face normals and planes
    nas_normals = {}  # fi -> Vector3d (outward)
    nas_planes = {}   # fi -> Plane
    for fi in range(nas.Faces.Count):
        face = nas.Faces[fi]
        plane_tol = max(sc.doc.ModelAbsoluteTolerance * 100, 0.1)
        rc, plane = face.TryGetPlane(plane_tol)
        if not rc:
            continue
        mid_u = face.Domain(0).Mid
        mid_v = face.Domain(1).Mid
        n = face.NormalAt(mid_u, mid_v)
        if face.OrientationIsReversed:
            n.Reverse()
        nas_normals[fi] = n
        nas_planes[fi] = plane

    # collect internal edges for curve_na extent (optional, best-effort)
    internal_edges = []
    for ei in range(nas.Edges.Count):
        edge = nas.Edges[ei]
        if len(edge.AdjacentFaces()) == 2:
            internal_edges.append(edge)

    for i, info in enumerate(bend_infos):
        bend_na = info["normal_a"]  # outward normal of ref_side face A
        bend_nb = info["normal_b"]  # outward normal of ref_side face B
        bend_mid = info["mid_pt"]

        # match NAS faces by normal: best dot product with bend normals
        best_a = (-1, -1.0)  # (face_idx, dot)
        best_b = (-1, -1.0)
        for fi, n in nas_normals.items():
            dot_a = abs(Vector3d.Multiply(n, bend_na))
            dot_b = abs(Vector3d.Multiply(n, bend_nb))
            if dot_a > best_a[1]:
                best_a = (fi, dot_a)
            if dot_b > best_b[1]:
                best_b = (fi, dot_b)

        # if both matched to same face, re-pick the second-best
        if best_a[0] == best_b[0]:
            # re-find best_b excluding best_a's face
            best_b = (-1, -1.0)
            for fi, n in nas_normals.items():
                if fi == best_a[0]:
                    continue
                dot_b = abs(Vector3d.Multiply(n, bend_nb))
                if dot_b > best_b[1]:
                    best_b = (fi, dot_b)

        fa = best_a[0]
        fb = best_b[0]
        info["na_face_a"] = fa
        info["na_face_b"] = fb

        # compute bend axis from plane-plane intersection
        axis_line = None
        if fa >= 0 and fb >= 0 and fa in nas_planes and fb in nas_planes:
            rc_pp, pp_line = Intersection.PlanePlane(nas_planes[fa], nas_planes[fb])
            if rc_pp:
                # trim infinite PP line to the NAS extent: project bend midpoint
                # and use internal edge length or bend curve length for extent
                pp_t = pp_line.ClosestParameter(bend_mid)
                pp_center = pp_line.PointAt(pp_t)

                # find internal edge near this bend for extent
                best_edge = None
                best_edge_dist = float("inf")
                for edge in internal_edges:
                    ep = edge.PointAt(edge.Domain.Mid)
                    d = bend_mid.DistanceTo(ep)
                    if d < best_edge_dist:
                        best_edge_dist = d
                        best_edge = edge

                if best_edge is not None and best_edge_dist < 1.0:
                    # use internal edge endpoints projected onto PP line
                    t0 = pp_line.ClosestParameter(best_edge.PointAtStart)
                    t1 = pp_line.ClosestParameter(best_edge.PointAtEnd)
                    axis_line = Line(pp_line.PointAt(t0), pp_line.PointAt(t1))
                else:
                    # use 3D bend curve extent projected onto PP line
                    crv = info["curve_3d"]
                    t0 = pp_line.ClosestParameter(crv.PointAtStart)
                    t1 = pp_line.ClosestParameter(crv.PointAtEnd)
                    axis_line = Line(pp_line.PointAt(t0), pp_line.PointAt(t1))

        if axis_line is None:
            # last resort fallback
            crv = info["curve_3d"]
            axis_line = Line(crv.PointAtStart, crv.PointAtEnd)

        info["curve_na"] = LineCurve(axis_line)
        info["na_axis"] = axis_line

        print("  bend {}: {:.1f}° → NAS faces {}↔{}, axis len={:.2f}\"".format(
            i, info["angle"], fa, fb, axis_line.Length))


def unroll_by_rotation(neutral_axis_brep, ink_curves, bend_infos):
    """unroll the NAS by rotating planar faces around bend axes.
    bypasses Rhino's Unroller which can't handle NAS edge mismatches.
    returns (flat_brep, outside_curves, inside_curves,
             flat_bend_curves, flat_ink_curves) or None."""
    tol = sc.doc.ModelAbsoluteTolerance
    nas = neutral_axis_brep
    n_faces = nas.Faces.Count

    # --- build adjacency from bend_infos ---
    # adjacency[face_idx] = [(neighbor_idx, bend_info, axis_line), ...]
    adjacency = {}
    for bi in bend_infos:
        fa = bi.get("na_face_a", -1)
        fb = bi.get("na_face_b", -1)
        axis = bi.get("na_axis")
        if fa < 0 or fb < 0 or axis is None:
            continue
        adjacency.setdefault(fa, []).append((fb, bi, axis))
        adjacency.setdefault(fb, []).append((fa, bi, axis))

    # --- get face planes ---
    face_planes = {}
    for fi in range(n_faces):
        face = nas.Faces[fi]
        plane_tol = max(tol * 100, 0.1)
        rc, plane = face.TryGetPlane(plane_tol)
        if rc:
            # ensure normal points outward (away from brep interior)
            amp = AreaMassProperties.Compute(face.DuplicateFace(False))
            if amp:
                mid_u = face.Domain(0).Mid
                mid_v = face.Domain(1).Mid
                n = face.NormalAt(mid_u, mid_v)
                if face.OrientationIsReversed:
                    n.Reverse()
                plane = Plane(amp.Centroid, plane.XAxis, plane.YAxis)
                # re-orient so plane.Normal matches outward normal
                if Vector3d.Multiply(plane.Normal, n) < 0:
                    plane = Plane(plane.Origin, plane.XAxis, -plane.YAxis)
            face_planes[fi] = plane

    if len(face_planes) < n_faces:
        print("  warning: only {} of {} faces have planes".format(
            len(face_planes), n_faces))

    # --- BFS: flatten faces onto XY ---
    transforms = {}  # face_idx -> Transform (3D -> flat XY)
    visited = set()

    # seed: face 0 -> WorldXY
    seed = 0
    if seed not in face_planes:
        for fi in face_planes:
            seed = fi
            break
    seed_plane = face_planes[seed]
    transforms[seed] = Transform.PlaneToPlane(seed_plane, Plane.WorldXY)
    visited.add(seed)

    bfs_path = ["face {}".format(seed)]
    queue = [seed]
    while queue:
        current = queue.pop(0)
        current_xform = transforms[current]
        for neighbor, bi, axis_line in adjacency.get(current, []):
            if neighbor in visited:
                continue
            if neighbor not in face_planes:
                continue

            # transform the bend axis to the flattened state
            p1 = Point3d(axis_line.From)
            p2 = Point3d(axis_line.To)
            p1.Transform(current_xform)
            p2.Transform(current_xform)
            axis_dir = Vector3d(p2 - p1)
            axis_dir.Unitize()

            # transform the neighbor's plane to current state
            neighbor_plane = Plane(face_planes[neighbor])
            neighbor_plane.Transform(current_xform)

            # compute rotation to flatten neighbor normal onto Z axis
            n_neighbor = neighbor_plane.Normal
            z = Vector3d.ZAxis

            # project both onto plane perpendicular to axis
            n_perp = n_neighbor - axis_dir * Vector3d.Multiply(n_neighbor, axis_dir)
            z_perp = z - axis_dir * Vector3d.Multiply(z, axis_dir)

            n_len = n_perp.Length
            z_len = z_perp.Length
            if n_len < 1e-10 or z_len < 1e-10:
                # normals parallel to axis — no rotation needed
                transforms[neighbor] = Transform(current_xform)
                visited.add(neighbor)
                queue.append(neighbor)
                bfs_path.append("face {} (0.0°)".format(neighbor))
                continue

            n_perp.Unitize()
            z_perp.Unitize()

            cos_angle = max(-1.0, min(1.0, Vector3d.Multiply(n_perp, z_perp)))
            angle = math.acos(cos_angle)

            # determine sign from cross product
            cross = Vector3d.CrossProduct(n_perp, z_perp)
            if Vector3d.Multiply(cross, axis_dir) < 0:
                angle = -angle

            rotation = Transform.Rotation(angle, axis_dir, p1)

            # combined: first apply current_xform (3D -> current flat),
            # then rotation (flatten neighbor around axis)
            combined = Transform.Multiply(rotation, current_xform)

            transforms[neighbor] = combined
            visited.add(neighbor)
            queue.append(neighbor)
            bfs_path.append("face {} ({:.1f}°)".format(neighbor, math.degrees(angle)))

    print("  BFS: {}".format(" → ".join(bfs_path)))
    if len(transforms) < n_faces:
        print("  warning: BFS reached {} of {} faces".format(
            len(transforms), n_faces))

    # --- transform face breps to flat ---
    flat_faces = []
    for fi in range(n_faces):
        if fi not in transforms:
            continue
        face_brep = nas.Faces[fi].DuplicateFace(False)
        face_brep.Transform(transforms[fi])
        flat_faces.append(face_brep)

    # diagnostic: print flat face info
    for fb in flat_faces:
        amp = AreaMassProperties.Compute(fb)
        if amp:
            c = amp.Centroid
            print("    flat face: area={:.1f}, centroid=({:.2f},{:.2f},{:.4f})".format(
                amp.Area, c.X, c.Y, c.Z))

    # --- transform bend curves ---
    flat_bend_curves = []
    for bi in bend_infos:
        crv = bi["curve_na"].DuplicateCurve()
        fa = bi.get("na_face_a", -1)
        if fa >= 0 and fa in transforms:
            crv.Transform(transforms[fa])
        flat_bend_curves.append(crv)

    # --- transform ink curves ---
    flat_ink_curves = []
    for guid, crv in ink_curves:
        mid = crv.PointAt(crv.Domain.Mid)
        best_fi = None
        best_dist = float("inf")
        for fi in transforms:
            face = nas.Faces[fi]
            rc, u, v = face.ClosestPoint(mid)
            if rc:
                cp = face.PointAt(u, v)
                d = mid.DistanceTo(cp)
                if d < best_dist:
                    best_dist = d
                    best_fi = fi
        if best_fi is not None:
            ink_copy = crv.DuplicateCurve()
            ink_copy.Transform(transforms[best_fi])
            flat_ink_curves.append(ink_copy)

    # --- extract boundary from flat faces ---
    # collect outer loops from each flat face
    outer_loops = []
    inner_loops = []
    for fb in flat_faces:
        for fi_flat in range(fb.Faces.Count):
            face = fb.Faces[fi_flat]
            for li in range(face.Loops.Count):
                loop = face.Loops[li]
                crv = loop.To3dCurve()
                if crv is None:
                    continue
                if not crv.IsClosed:
                    gap = crv.PointAtStart.DistanceTo(crv.PointAtEnd)
                    if gap < tol * 100:
                        rejoined = Curve.JoinCurves([crv], tol * 100)
                        if rejoined and len(rejoined) > 0 and rejoined[0].IsClosed:
                            crv = rejoined[0]
                        else:
                            continue
                    else:
                        continue
                if loop.LoopType == BrepLoopType.Outer:
                    outer_loops.append(crv)
                else:
                    inner_loops.append(crv)

    print("  flat faces: {}, outer loops: {}, inner loops: {}".format(
        len(flat_faces), len(outer_loops), len(inner_loops)))

    # boolean union of outer loops to get combined boundary
    outside_curves = []
    inside_curves = list(inner_loops)  # inner loops are always inside cuts

    if len(outer_loops) == 1:
        outside_curves = outer_loops
    elif len(outer_loops) > 1:
        # try Curve.CreateBooleanUnion
        try:
            union = Curve.CreateBooleanUnion(outer_loops, tol)
            if union and len(union) > 0:
                closed_union = [c for c in union if c.IsClosed]
                if closed_union:
                    # largest = outside, rest = inside (holes created by union)
                    areas = []
                    for c in closed_union:
                        amp = AreaMassProperties.Compute(c)
                        areas.append((amp.Area if amp else 0, c))
                    areas.sort(key=lambda x: x[0], reverse=True)
                    outside_curves = [areas[0][1]]
                    inside_curves.extend(a[1] for a in areas[1:])
                    print("  boolean union: {} curves".format(len(closed_union)))
        except Exception as e:
            print("  boolean union failed: {}".format(e))

        # fallback: rs.CurveBooleanUnion via temp doc objects
        if not outside_curves:
            print("  fallback: rs.CurveBooleanUnion")
            temp_ids = []
            for crv in outer_loops:
                guid = sc.doc.Objects.AddCurve(crv)
                if guid != System.Guid.Empty:
                    temp_ids.append(guid)
            if temp_ids:
                result_ids = rs.CurveBooleanUnion(temp_ids)
                if result_ids:
                    for rid in result_ids:
                        rcrv = rs.coercecurve(rid)
                        if rcrv and rcrv.IsClosed:
                            outside_curves.append(rcrv.DuplicateCurve())
                    rs.DeleteObjects(result_ids)
                # clean up temp curves
                for tid in temp_ids:
                    sc.doc.Objects.Delete(tid, True)

        # second fallback: just use largest loop
        if not outside_curves and outer_loops:
            print("  fallback: largest outer loop")
            areas = []
            for c in outer_loops:
                amp = AreaMassProperties.Compute(c)
                areas.append((amp.Area if amp else 0, c))
            areas.sort(key=lambda x: x[0], reverse=True)
            outside_curves = [areas[0][1]]

    # --- join flat faces into a single brep for alignment ---
    flat_brep = None
    if flat_faces:
        joined = Brep.JoinBreps(flat_faces, tol * 100)
        if joined and len(joined) >= 1:
            flat_brep = joined[0]
        else:
            flat_brep = flat_faces[0]

    if flat_brep is None:
        print("error: no flat geometry produced")
        return None

    return flat_brep, outside_curves, inside_curves, flat_bend_curves, flat_ink_curves


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
    face plane. the flat pattern is in XY — we PlaneToPlane from the flat
    face to the matching NA face. applied via sc.doc.Objects.Transform
    (the only transform method that works reliably in CPython 3)."""
    # compute alignment: PlaneToPlane from flat face to matching NA face
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

        # source: find flat face matching NA face by area
        na_face = neutral_axis.Faces[best_na_idx]
        na_brep = na_face.DuplicateFace(False)
        amp_na = AreaMassProperties.Compute(na_brep)
        na_area = amp_na.Area if amp_na else 0

        best_uf_face = None
        best_area_diff = float("inf")
        amp_uf = None
        flat = unrolled_breps[0]
        for fi in range(flat.Faces.Count):
            uf = flat.Faces[fi]
            uf_b = uf.DuplicateFace(False)
            uf_amp = AreaMassProperties.Compute(uf_b)
            if uf_amp:
                diff = abs(uf_amp.Area - na_area)
                if diff < best_area_diff:
                    best_area_diff = diff
                    best_uf_face = uf
                    amp_uf = uf_amp

        if amp_uf and amp_na and best_uf_face is not None:
            uf_centroid = amp_uf.Centroid
            na_centroid = amp_na.Centroid
            plane_tol = max(sc.doc.ModelAbsoluteTolerance * 100, 0.1)
            rc_uf_plane, uf_plane = best_uf_face.TryGetPlane(plane_tol)
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
    neutral_axis = construct_neutral_axis(ref_side, thickness, original_brep=brep,
                                          other_side=other_side,
                                          partners=partners)
    if neutral_axis is None:
        return
    print("  neutral axis: {} faces".format(neutral_axis.Faces.Count))

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
    # step 10: find ink curves on ref_side (the surface they're drawn on)
    ink_curves = find_ink_curves(ref_side)
    print("  ink curves: {}".format(len(ink_curves)))
    # step 10b: project ink curves onto NAS so they unroll flush
    tol = sc.doc.ModelAbsoluteTolerance
    projected_ink = []
    for guid, crv in ink_curves:
        proj = Curve.ProjectToBrep(crv, neutral_axis, -picked_normal, tol)
        if proj and len(proj) > 0:
            projected_ink.append((guid, proj[0]))
        else:
            projected_ink.append((guid, crv))
    ink_curves = projected_ink
    # step 11: unroll by face rotation
    print("=== unroll ===")
    unroll_result = unroll_by_rotation(neutral_axis, ink_curves, bend_infos)
    if unroll_result is None:
        return
    flat_brep, outside_curves, inside_curves, unrolled_bend, unrolled_ink = unroll_result
    unrolled_breps = [flat_brep]
    print("  {} bend lines, {} ink, cuts: {} outside, {} inside".format(
        len(unrolled_bend), len(unrolled_ink),
        len(outside_curves), len(inside_curves)))
    # step 13: create bend angle text
    text_curves = create_bend_text_curves(bend_infos, unrolled_bend)
    # step 14: add curves to sublayers
    sublayers = ensure_sublayers()
    count = add_output(neutral_axis, unrolled_breps, outside_curves,
                       inside_curves, unrolled_bend, unrolled_ink,
                       text_curves, sublayers,
                       brep=brep, picked_face_index=face_index)

    sc.doc.Views.Redraw()


if __name__ == "__main__":
    unfold_to_2d()
