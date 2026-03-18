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
    Curve,
    LineCurve,
    Line,
    Plane,
    Point3d,
    TextEntity,
    Transform,
    Unroller,
    Vector3d,
)
from Rhino.Geometry.Intersect import Intersection
from Rhino.Input.Custom import GetObject, GetString
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


def detect_thickness(brep, face_index):
    """detect material thickness by shooting a ray inward from the picked face
    and intersecting with each exploded face individually.
    snaps to nearest standard aluminum gauge (0.100, 0.125, 0.160, 0.190).
    returns the gauge thickness, or None if measurement is out of range."""
    tol = sc.doc.ModelAbsoluteTolerance
    centroid, normal = get_face_outward_normal(brep, face_index)
    if centroid is None:
        return None

    # shoot a line inward from the face centroid
    inward = -normal
    start = centroid + inward * 0.001  # small nudge off surface
    end = centroid + inward * 2.0
    ray = LineCurve(Line(start, end))

    # intersect ray with each face individually (exploded approach)
    # CurveBrep returns (bool, Curve[] overlapCurves, Point3d[] intersectionPoints)
    hits = []
    for fi in range(brep.Faces.Count):
        if fi == face_index:
            continue
        face_brep = brep.Faces[fi].DuplicateFace(False)
        if face_brep is None:
            continue
        rc, _, intersection_points = Intersection.CurveBrep(ray, face_brep, tol)
        if not rc or intersection_points is None:
            continue
        for pt in intersection_points:
            dist = centroid.DistanceTo(pt)
            if dist > 0.01:
                hits.append(dist)

    if not hits:
        return None

    # pick hit closest to sensible aluminum gauge range (1/16" to 1/4")
    sensible = [d for d in hits if 0.0625 < d < 0.250]
    if not sensible:
        print("warning: no hits in expected thickness range. closest: {:.4f}".format(min(hits)))
        return None

    raw = min(sensible)
    closest_gauge = min(STANDARD_GAUGES, key=lambda g: abs(g - raw))
    return closest_gauge


def prompt_thickness(auto_thickness):
    """prompt user to accept or override detected thickness. returns float."""
    if auto_thickness is not None:
        default_str = "{:.3f}".format(auto_thickness)
        gs = GetString()
        gs.SetCommandPrompt("detected thickness: {}. enter to accept or type override".format(default_str))
        gs.SetDefaultString(default_str)
        gs.AcceptNothing(True)
        gs.Get()
        if gs.CommandResult() != Rhino.Commands.Result.Success:
            return None
        result = gs.StringResult()
        if result is None or result.strip() == "":
            return auto_thickness
        try:
            return float(result.strip())
        except ValueError:
            print("error: invalid thickness value")
            return None
    else:
        gs = GetString()
        gs.SetCommandPrompt("could not auto-detect thickness. enter thickness")
        gs.Get()
        if gs.CommandResult() != Rhino.Commands.Result.Success:
            return None
        try:
            return float(gs.StringResult().strip())
        except (ValueError, AttributeError):
            print("error: invalid thickness value")
            return None


def classify_faces(brep, thickness):
    """classify brep faces into sheet faces and edge faces.
    a sheet face has a parallel partner: shoot rays BOTH directions from its centroid
    and check if either hits another face at ~thickness distance.
    returns (sheet_face_indices, edge_face_indices)."""
    tol = sc.doc.ModelAbsoluteTolerance
    thick_tol = thickness * 0.5  # 50% tolerance for partner distance matching

    # precompute centroids, normals, and duplicated face breps
    face_data = []
    face_breps = []
    for i in range(brep.Faces.Count):
        centroid, normal = get_face_outward_normal(brep, i)
        face_data.append((centroid, normal))
        face_breps.append(brep.Faces[i].DuplicateFace(False))

    # track which faces have a partner (symmetric: if i→j hits, both are sheet)
    sheet_set = set()
    pairs = []  # list of (face_i, face_j) partner pairs

    for i in range(brep.Faces.Count):
        if i in sheet_set:
            continue
        ci, ni = face_data[i]
        if ci is None or ni is None:
            print("  face {}: no centroid/normal, skipping".format(i))
            continue

        found = False
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
                        sheet_set.add(i)
                        sheet_set.add(j)
                        pairs.append((i, j))
                        print("  face {} <-> face {}: partner at {:.4f}\"".format(i, j, dist))
                        found = True
                        break
                if found:
                    break

    sheet_faces = sorted(sheet_set)
    edge_faces = [i for i in range(brep.Faces.Count) if i not in sheet_set]
    return sheet_faces, edge_faces, pairs


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


def construct_neutral_axis(brep, pairs, thickness):
    """construct the neutral axis surface by offsetting the larger face from
    each paired set individually, then joining. per-face offset avoids the
    fillet faces that CreateOffsetBrep adds at bends on a polysurface.
    returns a brep or None."""
    tol = sc.doc.ModelAbsoluteTolerance
    offset_dist = thickness / 2.0

    offset_faces = []
    for fi_a, fi_b in pairs:
        # pick the larger face from each pair
        brep_a = brep.Faces[fi_a].DuplicateFace(False)
        brep_b = brep.Faces[fi_b].DuplicateFace(False)
        if brep_a is None or brep_b is None:
            continue

        area_a = brep_a.GetArea()
        area_b = brep_b.GetArea()

        if area_a >= area_b:
            source = brep_a
            # outer face: offset inward (negative = toward interior)
            dist = -offset_dist
        else:
            source = brep_b
            # inner face: offset outward (positive = toward exterior)
            dist = offset_dist

        try:
            result = Brep.CreateOffsetBrep(source, dist, False, False, tol)
            if result and result[0] and len(result[0]) > 0:
                offset_faces.append(result[0][0])
                continue
        except Exception:
            pass
        print("warning: could not offset face pair ({}, {})".format(fi_a, fi_b))

    if not offset_faces:
        print("error: could not offset any faces")
        return None

    if len(offset_faces) == 1:
        return offset_faces[0]

    joined = Brep.JoinBreps(offset_faces, tol)
    if joined and len(joined) > 0:
        result = sorted(joined, key=lambda b: b.GetArea(), reverse=True)[0]
        # debug: bake neutral axis for inspection
        sc.doc.Objects.AddBrep(result)
        sc.doc.Views.Redraw()
        return result

    return offset_faces[0]


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
    sheet_faces, edge_faces, pairs = classify_faces(brep, thickness)
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

    # step 7: construct neutral axis (per-face offset for sharp corners)
    neutral_axis = construct_neutral_axis(brep, pairs, thickness)
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
