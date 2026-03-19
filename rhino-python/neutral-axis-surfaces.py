#! python3
"""neutral-axis-surfaces: construct neutral axis surfaces for a sheet metal part.

takes a closed polysurface (sheet metal part with material thickness),
classifies faces into sheet/edge, splits into two sides, and constructs
the neutral axis surface at t/2 offset from the picked face side.

outputs the NAS as a polysurface added to the document.

alias: neutral-axis-surfaces -> _-RunPythonScript "path/to/neutral-axis-surfaces.py"
"""

import System
import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
from Rhino.Geometry import (
    AreaMassProperties,
    Brep,
    BrepLoopType,
    LineCurve,
    Line,
    Plane,
    Point3d,
    PointFaceRelation,
    PolylineCurve,
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _doc_translate(curve, dx, dy, dz):
    """translate a curve using doc round-trip (CPython 3 workaround)."""
    xf = Transform.Translation(dx, dy, dz)
    temp_id = sc.doc.Objects.AddCurve(curve)
    new_id = sc.doc.Objects.Transform(temp_id, xf, True)
    obj = sc.doc.Objects.FindId(new_id)
    if obj is not None:
        curve = obj.Geometry.DuplicateCurve()
    sc.doc.Objects.Delete(new_id, True)
    return curve


def pick_part_and_face():
    """select a closed polysurface and pick a sheet face."""
    go = GetObject()
    go.SetCommandPrompt("select part — pick a face (ctrl+shift+click)")
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
    """get outward-pointing normal at the centroid of a brep face."""
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
    """remove inner trim loops (holes) from a brep face."""
    face_brep = face.DuplicateFace(False)
    if face_brep is None:
        return None
    if face.Loops.Count <= 1:
        return face_brep
    outer_crv = face.OuterLoop.To3dCurve()
    if outer_crv is None:
        return face_brep
    tol = sc.doc.ModelAbsoluteTolerance
    planar = Brep.CreatePlanarBreps([outer_crv], tol)
    if planar and len(planar) > 0:
        return planar[0]
    planar = Brep.CreatePlanarBreps([outer_crv], tol * 100)
    if planar and len(planar) > 0:
        return planar[0]
    return face_brep


def _shoot_thickness_ray(brep, face_index, origin, normal, tol):
    """shoot rays BOTH directions from origin and collect hit distances."""
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
    """detect material thickness by shooting rays inward from the picked face."""
    tol = sc.doc.ModelAbsoluteTolerance
    face = brep.Faces[face_index]

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

    u_dom = face.Domain(0)
    v_dom = face.Domain(1)
    for ui in range(5):
        for vi in range(5):
            u = u_dom.T0 + (u_dom.T1 - u_dom.T0) * (ui + 0.5) / 5
            v = v_dom.T0 + (v_dom.T1 - v_dom.T0) * (vi + 0.5) / 5
            pfr = face.IsPointOnFace(u, v)
            is_exterior = (pfr == PointFaceRelation.Exterior or int(pfr) == 2)
            if is_exterior:
                continue
            pt = face.PointAt(u, v)
            normal = face.NormalAt(u, v)
            if face.OrientationIsReversed:
                normal = -normal
            hits = _shoot_thickness_ray(brep, face_index, pt, normal, tol)
            result = _snap_hits_to_gauge(hits)
            if result is not None:
                return result

    # fallback: shortest edge
    edge_lengths = []
    for ei in face.AdjacentEdges():
        edge_lengths.append(brep.Edges[ei].GetLength())
    if edge_lengths:
        edge_lengths.sort()
        raw = edge_lengths[0]
        if 0.01 < raw < 0.5:
            return min(STANDARD_GAUGES, key=lambda g: abs(g - raw))
    return None


def prompt_thickness(auto_thickness):
    """prompt user to accept or override detected thickness."""
    if auto_thickness is not None:
        msg = "detected thickness: {:.4f}. enter to accept or type override".format(auto_thickness)
        return rs.GetReal(msg, auto_thickness, 0.01, 1.0)
    else:
        return rs.GetReal("could not auto-detect thickness. enter thickness",
                          number=0.125, minimum=0.01, maximum=1.0)


def classify_faces(brep, thickness):
    """classify brep faces into sheet faces and edge faces."""
    tol = sc.doc.ModelAbsoluteTolerance
    thick_tol = thickness * 0.2

    face_data = []
    face_breps = []
    for i in range(brep.Faces.Count):
        centroid, normal = get_face_outward_normal(brep, i)
        face_data.append((centroid, normal))
        face_breps.append(_untrim_face(brep.Faces[i]))

    sheet_set = set()
    partners = {}

    for i in range(brep.Faces.Count):
        if i in sheet_set:
            continue
        ci, ni = face_data[i]
        if ci is None or ni is None:
            continue

        found = False
        for direction in [ni, -ni]:
            if found:
                break
            start = ci + direction * 0.001
            end = ci + direction * 2.0
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
                        _, nj = face_data[j]
                        if nj is not None:
                            dot = Vector3d.Multiply(ni, nj)
                            if abs(dot) < 0.5:
                                continue
                        sheet_set.add(i)
                        sheet_set.add(j)
                        if i not in partners:
                            partners[i] = j
                        if j not in partners:
                            partners[j] = i
                        found = True
                        break
                if found:
                    break

    sheet_faces = sorted(sheet_set)
    edge_faces = [i for i in range(brep.Faces.Count) if i not in sheet_set]
    return sheet_faces, edge_faces, partners


def join_sheet_faces(brep, sheet_faces, partners, picked_face_index):
    """join all sheet faces into two polysurfaces representing both sides."""
    tol = sc.doc.ModelAbsoluteTolerance

    color = {}
    for fi in sheet_faces:
        if fi in color:
            continue
        color[fi] = 0
        queue = [fi]
        while queue:
            current = queue.pop(0)
            if current not in partners:
                continue
            partner = partners[current]
            if partner in color:
                continue
            color[partner] = 1 - color[current]
            queue.append(partner)

    ref_color = color.get(picked_face_index, 0)
    side_a_indices = [fi for fi in sheet_faces if color.get(fi, 0) == ref_color]
    side_b_indices = [fi for fi in sheet_faces if color.get(fi, 0) != ref_color]

    if not side_a_indices or not side_b_indices:
        print("error: could not split sheet faces into 2 sides")
        return None, None

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
            joined2 = Brep.JoinBreps(list(joined), tol * 10)
            if joined2 and len(joined2) == 1:
                return joined2[0]
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


def _make_planar(curves, tol):
    """create planar brep from curves, trying normal then loose tolerance."""
    result = Brep.CreatePlanarBreps(curves, tol)
    if not result or len(result) == 0:
        result = Brep.CreatePlanarBreps(curves, tol * 100)
    if result and len(result) > 0:
        return result[0]
    return None


def _build_nas_boundary(face, fi, face_planes, face_normals, offset_dist,
                        ref_side, skipped_faces, tol):
    """build the boundary curve for a NAS face using edge-walk + PP projection."""
    normal = face_normals[fi]
    offset_vec = Vector3d(-normal.X * offset_dist,
                           -normal.Y * offset_dist,
                           -normal.Z * offset_dist)

    outer_crv = face.OuterLoop.To3dCurve()
    if outer_crv is None:
        return None

    # build bend_map: PP axes for adjacent faces only (via trim adjacency)
    bend_map = {}
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
        rc, pp_line = Intersection.PlanePlane(face_planes[fi], face_planes[target])
        if rc:
            bend_map[target] = pp_line

    # walk trims — geometric classification: parallel to + near PP axis = bend
    segments = []
    for trim_obj in face.OuterLoop.Trims:
        edge = trim_obj.Edge
        if edge is None:
            continue
        if trim_obj.IsReversed():
            e_start = edge.PointAtEnd
            e_end = edge.PointAtStart
        else:
            e_start = edge.PointAtStart
            e_end = edge.PointAtEnd

        s_nap = Point3d(e_start.X + offset_vec.X,
                        e_start.Y + offset_vec.Y,
                        e_start.Z + offset_vec.Z)
        e_nap = Point3d(e_end.X + offset_vec.X,
                        e_end.Y + offset_vec.Y,
                        e_end.Z + offset_vec.Z)

        edge_dir = Vector3d(e_nap.X - s_nap.X, e_nap.Y - s_nap.Y,
                            e_nap.Z - s_nap.Z)
        edge_len = edge_dir.Length
        classified = False
        if edge_len > tol:
            edge_dir.Unitize()
            mid_nap = Point3d((s_nap.X + e_nap.X) / 2,
                              (s_nap.Y + e_nap.Y) / 2,
                              (s_nap.Z + e_nap.Z) / 2)
            for tgt, pp_line in bend_map.items():
                t = pp_line.ClosestParameter(mid_nap)
                dist = mid_nap.DistanceTo(pp_line.PointAt(t))
                if dist > offset_dist * 2:
                    continue
                pp_dir = Vector3d(pp_line.Direction)
                pp_dir.Unitize()
                dot = abs(Vector3d.Multiply(edge_dir, pp_dir))
                if dot < 0.999:
                    continue
                t0 = pp_line.ClosestParameter(s_nap)
                t1 = pp_line.ClosestParameter(e_nap)
                pp_s = pp_line.PointAt(t0)
                pp_e = pp_line.PointAt(t1)
                if pp_s.DistanceTo(pp_e) > tol:
                    segments.append(("bend", LineCurve(Line(pp_s, pp_e)), tgt))
                classified = True
                break

        if not classified and edge_len > tol:
            segments.append(("perimeter", LineCurve(Line(s_nap, e_nap)), None))

    if len(segments) < 3:
        projected = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
        return projected if projected.IsClosed else None

    # merge consecutive bend segments with same target
    merged = []
    i = 0
    while i < len(segments):
        typ, crv, target = segments[i]
        if typ == "bend" and target is not None:
            run_start = crv.PointAtStart
            run_end = crv.PointAtEnd
            j = i + 1
            while j < len(segments) and segments[j][2] == target:
                run_end = segments[j][1].PointAtEnd
                j += 1
            merged.append(("bend", LineCurve(Line(run_start, run_end)), target))
            i = j
        else:
            merged.append(segments[i])
            i += 1

    if len(merged) < 3:
        projected = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
        return projected if projected.IsClosed else None

    # compute vertices at each segment transition
    n = len(merged)
    vertices = []
    for i in range(n):
        curr_type, curr_crv, curr_target = merged[i]
        next_type, next_crv, next_target = merged[(i + 1) % n]

        if curr_type == "bend":
            line_a = bend_map[curr_target]
        else:
            line_a = Line(curr_crv.PointAtStart, curr_crv.PointAtEnd)
        if next_type == "bend":
            line_b = bend_map[next_target]
        else:
            line_b = Line(next_crv.PointAtStart, next_crv.PointAtEnd)

        rc, ta, tb = Intersection.LineLine(line_a, line_b)
        if rc:
            pt_a = line_a.PointAt(ta)
            pt_b = line_b.PointAt(tb)
            gap = pt_a.DistanceTo(pt_b)
            if gap < tol * 100:
                vertex = Point3d((pt_a.X + pt_b.X) / 2,
                                 (pt_a.Y + pt_b.Y) / 2,
                                 (pt_a.Z + pt_b.Z) / 2)
            else:
                vertex = face_planes[fi].ClosestPoint(pt_a)
        else:
            if next_type in ("perimeter",):
                vertex = Point3d(next_crv.PointAtStart.X,
                                 next_crv.PointAtStart.Y,
                                 next_crv.PointAtStart.Z)
            else:
                vertex = Point3d(curr_crv.PointAtEnd.X,
                                 curr_crv.PointAtEnd.Y,
                                 curr_crv.PointAtEnd.Z)

        vertices.append(face_planes[fi].ClosestPoint(vertex))

    if len(vertices) < 3:
        projected = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
        return projected if projected.IsClosed else None

    cleaned = [vertices[0]]
    for v in vertices[1:]:
        if v.DistanceTo(cleaned[-1]) > tol:
            cleaned.append(v)
    if len(cleaned) > 1 and cleaned[-1].DistanceTo(cleaned[0]) < tol:
        cleaned.pop()

    if len(cleaned) < 3:
        projected = _doc_translate(outer_crv, offset_vec.X, offset_vec.Y, offset_vec.Z)
        return projected if projected.IsClosed else None

    cleaned.append(cleaned[0])
    return PolylineCurve([Point3d(v.X, v.Y, v.Z) for v in cleaned])


def construct_neutral_axis(ref_side, thickness, original_brep=None, other_side=None,
                           partners=None):
    """construct the neutral axis surface from plane geometry."""
    tol = sc.doc.ModelAbsoluteTolerance
    offset_dist = thickness / 2.0

    face_planes = {}
    face_normals = {}
    for fi in range(ref_side.Faces.Count):
        face = ref_side.Faces[fi]
        plane_tol = max(tol * 10, 0.01)
        rc, plane = face.TryGetPlane(plane_tol)
        if not rc:
            continue
        face_brep = face.DuplicateFace(False)
        amp = AreaMassProperties.Compute(face_brep)
        if amp is None:
            continue
        centroid = amp.Centroid
        rc2, u, v = face.ClosestPoint(centroid)
        if not rc2:
            continue
        normal = face.NormalAt(u, v)
        if face.OrientationIsReversed:
            normal = -normal

        # verify offset direction using partner face
        direction_set = False
        if partners is not None and original_brep is not None:
            orig_fi = None
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
                        normal = -normal
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

        offset_origin = plane.Origin - normal * offset_dist
        face_planes[fi] = Plane(offset_origin, plane.XAxis, plane.YAxis)
        face_normals[fi] = normal

    if len(face_planes) < 1:
        print("error: no planar faces found")
        return None

    print("  {} faces, {} have offset planes".format(
        ref_side.Faces.Count, len(face_planes)))

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
        if face.OuterLoop is None:
            nas_skip += 1
            continue
        if fi in skipped_faces:
            nas_skip += 1
            continue

        boundary = _build_nas_boundary(face, fi, face_planes, face_normals,
                                        offset_dist, ref_side, skipped_faces, tol)
        if boundary is None:
            nas_skip += 1
            continue

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
            face_brep = _make_planar([boundary], tol)

        if face_brep is not None:
            neutral_faces.append(face_brep)
            nas_ok += 1
        else:
            nas_skip += 1

    print("  NAS: {} of {} faces OK".format(nas_ok, nas_ok + nas_skip))

    if not neutral_faces:
        print("error: could not create any neutral axis faces")
        return None

    if len(neutral_faces) == 1:
        return neutral_faces[0]

    joined = Brep.JoinBreps(neutral_faces, tol)
    if joined and len(joined) == 1:
        return joined[0]
    elif joined and len(joined) > 1:
        joined2 = Brep.JoinBreps(joined, tol * 10)
        if joined2 and len(joined2) == 1:
            return joined2[0]
        pieces = list(joined2) if joined2 else list(joined)
        result = pieces[0]
        for pi in range(1, len(pieces)):
            result.Join(pieces[pi], tol * 10, True)
        return result
    return neutral_faces[0]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def neutral_axis_surfaces():
    result = pick_part_and_face()
    if result is None:
        return
    brep, face_index, obj_id = result
    print("picked face: index {}".format(face_index))

    auto_thickness = detect_thickness(brep, face_index)
    thickness = prompt_thickness(auto_thickness)
    if thickness is None:
        return
    print("thickness: {}".format(thickness))

    sheet_faces, edge_faces, partners = classify_faces(brep, thickness)
    print("{} sheet faces, {} edge faces".format(len(sheet_faces), len(edge_faces)))

    if len(sheet_faces) < 2:
        print("error: need at least 2 sheet faces")
        return

    ref_side, other_side = join_sheet_faces(brep, sheet_faces, partners, face_index)
    if ref_side is None:
        return
    print("ref side: {} faces, other side: {} faces".format(
        ref_side.Faces.Count, other_side.Faces.Count))

    neutral_axis = construct_neutral_axis(ref_side, thickness, original_brep=brep,
                                          other_side=other_side,
                                          partners=partners)
    if neutral_axis is None:
        return

    nas_id = sc.doc.Objects.AddBrep(neutral_axis)
    sc.doc.Views.Redraw()
    print("neutral axis surface added ({} faces)".format(neutral_axis.Faces.Count))


if __name__ == "__main__":
    neutral_axis_surfaces()
