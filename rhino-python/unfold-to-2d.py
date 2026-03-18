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
  180 = flat. "135 UP" = bent 45° toward picked face. "135 DN" = bent 45° away.

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
    Plane,
    Point3d,
    TextEntity,
    Transform,
    Unroller,
    Vector3d,
)
from Rhino.Input.Custom import GetObject, GetString
from Rhino.DocObjects import ObjectType


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
DEFAULT_K_FACTOR = 0.44
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
    """get outward-pointing normal at the centroid of a brep face."""
    face = brep.Faces[face_index]
    amp = AreaMassProperties.Compute(face)
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
    """detect material thickness from the shortest edge of the picked face.
    for a sheet face, the shortest edges are the thickness edges connecting
    the outer face to the inner face through perimeter edge faces."""
    face = brep.Faces[face_index]
    edge_lengths = []
    for ei in face.AdjacentEdges():
        edge_lengths.append(brep.Edges[ei].GetLength())
    edge_lengths.sort()
    # shortest edge of a sheet face = material thickness
    if edge_lengths and edge_lengths[0] > 0.01:
        return round(edge_lengths[0], 4)
    return None


def prompt_thickness(auto_thickness):
    """prompt user to accept or override detected thickness. returns float."""
    if auto_thickness is not None:
        default_str = "{:.4f}".format(auto_thickness)
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


def classify_faces(brep, thickness, picked_face_index):
    """classify brep faces into sheet faces and edge faces.
    edge faces are narrow strips whose width ≈ thickness (area / longest edge).
    returns (outer_sheet_indices, inner_sheet_indices, edge_face_indices)."""
    thick_tol = thickness * 0.4  # 40% tolerance for matching thickness dimension

    edge_faces = []
    sheet_faces = []

    for i in range(brep.Faces.Count):
        face = brep.Faces[i]
        amp = AreaMassProperties.Compute(face)
        if amp is None:
            sheet_faces.append(i)
            continue

        area = amp.Area
        edge_indices = face.AdjacentEdges()
        max_edge_len = 0
        for ei in edge_indices:
            el = brep.Edges[ei].GetLength()
            if el > max_edge_len:
                max_edge_len = el

        if max_edge_len > 0:
            # approximate face width = area / longest edge
            # edge faces (thin strips) have width ≈ thickness
            approx_width = area / max_edge_len
            if abs(approx_width - thickness) < thick_tol:
                edge_faces.append(i)
            else:
                sheet_faces.append(i)
        else:
            sheet_faces.append(i)

    # separate sheet faces into outer and inner using flood fill from picked face
    outer_sheet, inner_sheet = _flood_fill_outer_faces(
        brep, sheet_faces, edge_faces, picked_face_index, thickness,
    )

    return outer_sheet, inner_sheet, edge_faces


def _flood_fill_outer_faces(brep, sheet_faces, edge_faces, picked_face_index, thickness):
    """flood fill from the picked face through edge faces to find all outer sheet faces.
    key insight: from an edge face, we reach two sheet faces — one outer, one inner.
    we reject candidates that are antiparallel to a known outer face adjacent to the
    same edge face (that's the inner counterpart)."""
    sheet_set = set(sheet_faces)
    edge_set = set(edge_faces)

    # build face adjacency via shared edges
    adjacency = {}
    for i in range(brep.Faces.Count):
        adjacency[i] = set()

    for ei in range(brep.Edges.Count):
        edge = brep.Edges[ei]
        adj_faces = edge.AdjacentFaces()
        for a in adj_faces:
            for b in adj_faces:
                if a != b:
                    adjacency[a].add(b)
                    adjacency[b].add(a)

    # flood fill from picked face
    outer = set()
    visited = set()
    queue = [picked_face_index]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        if current in sheet_set:
            outer.add(current)
            # from a sheet face, traverse only to adjacent edge faces
            for neighbor in adjacency[current]:
                if neighbor in edge_set and neighbor not in visited:
                    queue.append(neighbor)
        elif current in edge_set:
            # from an edge face, find which adjacent sheet faces are outer vs inner.
            # check against the known outer face(s) that also border this edge face:
            # if the candidate is antiparallel to a sibling outer → it's the inner counterpart.
            sibling_outers = [f for f in adjacency[current] if f in outer]

            for neighbor in adjacency[current]:
                if neighbor in sheet_set and neighbor not in visited:
                    _, fn = get_face_outward_normal(brep, neighbor)
                    if fn is None:
                        continue

                    is_inner = False
                    for so in sibling_outers:
                        _, so_n = get_face_outward_normal(brep, so)
                        if so_n is not None:
                            dot = Vector3d.Multiply(fn, so_n)
                            if dot < -0.7:
                                is_inner = True
                                break

                    if not is_inner:
                        queue.append(neighbor)

    inner = set(sheet_set) - outer
    return list(outer), list(inner)


def identify_internal_bends(brep, outer_sheet, edge_faces):
    """identify which edge faces are internal bends vs perimeter edges.
    an internal bend connects two different outer sheet faces.
    returns list of (edge_face_index, outer_face_a, outer_face_b)."""
    outer_set = set(outer_sheet)
    bends = []

    for efi in edge_faces:
        edge_face = brep.Faces[efi]
        # find outer sheet faces adjacent to this edge face
        adjacent_outer = set()
        for ei in edge_face.AdjacentEdges():
            edge = brep.Edges[ei]
            for adj_fi in edge.AdjacentFaces():
                if adj_fi in outer_set and adj_fi != efi:
                    adjacent_outer.add(adj_fi)

        if len(adjacent_outer) >= 2:
            # this edge face connects two outer sheet faces = internal bend
            outer_list = list(adjacent_outer)
            bends.append((efi, outer_list[0], outer_list[1]))

    return bends


def find_ink_curves(brep):
    """find curves on '09 - Ink lines' layer that are associated with the brep.
    returns list of (curve_guid, curve_geometry)."""
    ink_layer = "09 - Ink lines"
    if not rs.IsLayer(ink_layer):
        return []

    tol = sc.doc.ModelAbsoluteTolerance * 10  # generous tolerance for association
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

        # check if curve is near the brep by sampling points
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


def construct_neutral_axis(brep, outer_sheet, thickness, k_factor):
    """construct the neutral axis surface by offsetting outer sheet faces inward.
    returns a brep representing the neutral axis surface, or None."""
    offset_dist = thickness * k_factor
    tol = sc.doc.ModelAbsoluteTolerance

    offset_faces = []
    for fi in outer_sheet:
        face = brep.Faces[fi]
        # get outward normal to determine offset direction
        _, normal = get_face_outward_normal(brep, fi)
        if normal is None:
            continue

        # offset the face inward (opposite of outward normal)
        face_brep = face.DuplicateFace(False)
        if face_brep is None:
            continue

        # try Surface.Offset first (simpler and more reliable for single faces)
        srf = face.UnderlyingSurface()
        offset_srf = srf.Offset(-offset_dist, tol)
        if offset_srf is not None:
            offset_brep = offset_srf.ToBrep()
            if offset_brep is not None:
                offset_faces.append(offset_brep)
                continue

        # fallback: use Brep.CreateOffsetBrep
        # returns (Brep[] outBreps, Brep[] outBlends, Brep[] outWalls)
        try:
            result = Brep.CreateOffsetBrep(
                face_brep, -offset_dist, True, True, tol,
            )
            if result and result[0] and len(result[0]) > 0:
                for ob in result[0]:
                    if ob is not None:
                        offset_faces.append(ob)
        except Exception:
            pass  # skip this face if offset fails entirely

    if not offset_faces:
        print("error: could not offset any faces to create neutral axis")
        return None

    # join all offset faces into one surface
    if len(offset_faces) == 1:
        return offset_faces[0]

    joined = Brep.JoinBreps(offset_faces, tol)
    if joined and len(joined) > 0:
        # prefer the largest joined result
        joined_sorted = sorted(joined, key=lambda b: b.GetArea(), reverse=True)
        return joined_sorted[0]

    print("warning: could not join offset faces, using largest piece")
    return offset_faces[0]


def generate_bend_lines(brep, bends, neutral_axis_brep, outer_sheet, thickness, k_factor):
    """generate bend line curves on the neutral axis surface at internal bend locations.
    also computes bend angles and up/down direction.
    returns list of BendInfo dicts."""
    tol = sc.doc.ModelAbsoluteTolerance
    bend_infos = []

    for edge_face_idx, face_a_idx, face_b_idx in bends:

        # compute dihedral angle between the two outer sheet faces
        _, normal_a = get_face_outward_normal(brep, face_a_idx)
        _, normal_b = get_face_outward_normal(brep, face_b_idx)
        if normal_a is None or normal_b is None:
            continue

        # angle between normals — for sheet metal, the bend angle
        # dot product gives cos(angle between normals)
        dot = Vector3d.Multiply(normal_a, normal_b)
        dot = max(-1.0, min(1.0, dot))  # clamp for acos
        angle_between_normals = math.degrees(math.acos(dot))

        # the included angle (what fabricators see) is 180 - angle_between_normals
        # because when normals are parallel (flat), angle_between_normals = 0 → included = 180
        # when bent 90°, normals are perpendicular, angle_between_normals = 90 → included = 90
        included_angle = round(180.0 - angle_between_normals, 1)

        # determine UP/DN direction
        # get the bend edge direction (shared edge between the two outer faces)
        bend_edge_curve = _find_bend_edge_curve(brep, edge_face_idx, face_a_idx, face_b_idx)
        if bend_edge_curve is None:
            continue

        # project bend line onto the neutral axis surface
        bend_on_na = _project_curve_to_brep(bend_edge_curve, neutral_axis_brep, tol)
        if bend_on_na is None:
            bend_on_na = bend_edge_curve  # fallback: use original

        # get midpoint and tangent of bend curve for direction computation
        mid_t = bend_edge_curve.Domain.Mid
        mid_pt = bend_edge_curve.PointAt(mid_t)
        bend_tangent = bend_edge_curve.TangentAt(mid_t)

        direction = "UP"  # refined later by determine_bend_directions()

        bend_infos.append({
            "edge_face": edge_face_idx,
            "face_a": face_a_idx,
            "face_b": face_b_idx,
            "curve_3d": bend_edge_curve,
            "curve_na": bend_on_na,
            "angle": included_angle,
            "direction": direction,
            "normal_a": normal_a,
            "normal_b": normal_b,
            "mid_pt": mid_pt,
            "tangent": bend_tangent,
        })

    return bend_infos


def _find_bend_edge_curve(brep, edge_face_idx, face_a_idx, face_b_idx):
    """find the edge curve shared between the edge face and one of the outer faces,
    or the logical bend line at the transition."""
    face_a_edges = set(brep.Faces[face_a_idx].AdjacentEdges())
    edge_face_edges = set(brep.Faces[edge_face_idx].AdjacentEdges())

    shared = face_a_edges.intersection(edge_face_edges)
    if shared:
        # pick the longest shared edge as the bend line
        best_edge = None
        best_length = 0
        for ei in shared:
            edge = brep.Edges[ei]
            length = edge.GetLength()
            if length > best_length:
                best_length = length
                best_edge = edge
        if best_edge is not None:
            return best_edge.DuplicateCurve()

    return None


def _project_curve_to_brep(curve, target_brep, tol):
    """project a curve onto a brep surface. returns the projected curve or None."""
    # sample points from the curve and find closest points on the brep
    pts = []
    num_samples = 20
    domain = curve.Domain
    for i in range(num_samples + 1):
        t = domain.T0 + (i / num_samples) * (domain.T1 - domain.T0)
        pt = curve.PointAt(t)
        closest_pt = target_brep.ClosestPoint(pt)
        if closest_pt.IsValid:
            pts.append(closest_pt)

    if len(pts) < 2:
        return None

    # create interpolated curve through projected points
    projected = Curve.CreateInterpolatedCurve(pts, 3)
    return projected


def determine_bend_directions(bend_infos, picked_normal):
    """refine UP/DN direction for each bend relative to the picked face normal."""
    for info in bend_infos:
        normal_a = info["normal_a"]
        normal_b = info["normal_b"]
        tangent = info["tangent"]

        # the bend folds face_b relative to face_a
        # cross product of normal_a x tangent gives the "fold direction"
        cross = Vector3d.CrossProduct(normal_a, tangent)
        cross.Unitize()

        # check if face_b is "above" or "below" by comparing its normal rotation
        # relative to picked_normal
        # if the fold moves face_b toward the picked normal direction = UP
        # determine by checking if normal_b has a component toward picked_normal
        # compared to what it would be if flat (i.e., same as normal_a)

        # simpler: average of the two normals points in the fold direction
        avg_normal = normal_a + normal_b
        avg_normal.Unitize()

        # if the average normal agrees with picked_normal, the fold is opening "up"
        dot_with_picked = Vector3d.Multiply(cross, picked_normal)

        info["direction"] = "UP" if dot_with_picked > 0 else "DN"


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

    # ensure parent exists
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

    # track curve indices to separate ink curves from bend curves after unroll
    num_ink = len(ink_curves)
    num_bend = len(bend_infos)

    for _, crv in ink_curves:
        unroller.AddFollowingGeometry(crv)

    for info in bend_infos:
        unroller.AddFollowingGeometry(info["curve_na"])

    # PerformUnroll returns (Brep[], out Curve[], out Point3d[], out TextDot[])
    # in Python, out params become additional return values
    unrolled_breps, out_curves, out_points, out_dots = unroller.PerformUnroll()

    if unrolled_breps is None or len(unrolled_breps) == 0:
        print("error: unroll failed")
        return None

    # separate curves back into ink and bend
    out_curve_list = list(out_curves) if out_curves else []
    unrolled_ink = out_curve_list[:num_ink]
    unrolled_bend = out_curve_list[num_ink:num_ink + num_bend]

    return unrolled_breps, unrolled_ink, unrolled_bend


def classify_unrolled_curves(unrolled_breps):
    """classify boundary curves from unrolled breps into outside cut and inside cut.
    returns (outside_curves, inside_curves)."""
    outside = []
    inside = []

    for brep in unrolled_breps:
        naked = brep.DuplicateNakedEdgeCurves(True, False)
        if naked is None or len(naked) == 0:
            continue

        # join naked edges into closed curves
        tol = sc.doc.ModelAbsoluteTolerance
        joined = Curve.JoinCurves(naked, tol)
        if joined is None or len(joined) == 0:
            continue

        if len(joined) == 1:
            # single boundary = outside cut (no holes)
            outside.extend(joined)
        else:
            # multiple boundaries: largest area = outside, rest = inside (holes)
            areas = []
            for crv in joined:
                amp = AreaMassProperties.Compute(crv)
                area = amp.Area if amp else 0
                areas.append((area, crv))
            areas.sort(key=lambda x: x[0], reverse=True)

            outside.append(areas[0][1])  # largest = perimeter
            for _, crv in areas[1:]:
                inside.append(crv)  # smaller = holes

    return outside, inside


def create_bend_text_curves(bend_infos, unrolled_bend_curves):
    """create text as curve geometry for bend angle annotations.
    returns list of curve arrays."""
    all_text_curves = []

    if len(unrolled_bend_curves) != len(bend_infos):
        print("warning: bend curve count mismatch ({} vs {})".format(
            len(unrolled_bend_curves), len(bend_infos)))
        return all_text_curves

    for i, crv in enumerate(unrolled_bend_curves):
        info = bend_infos[i]
        angle = info["angle"]
        direction = info["direction"]

        # format text
        angle_int = int(round(angle))
        text_content = "{} {}".format(angle_int, direction)

        # position: midpoint of bend line, offset perpendicular
        mid_t = crv.Domain.Mid
        mid_pt = crv.PointAt(mid_t)
        tangent = crv.TangentAt(mid_t)
        tangent.Unitize()

        # perpendicular in XY plane (since unrolled is flat)
        perp = Vector3d(-tangent.Y, tangent.X, 0)
        perp.Unitize()

        # offset the text position
        text_origin = mid_pt + perp * TEXT_HEIGHT * 2

        # create text entity
        text_plane = Plane(text_origin, Vector3d.XAxis, Vector3d.YAxis)

        # align text along the bend line direction
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
    # compute offset: to the right of the 3D part
    bb_3d = brep_3d.GetBoundingBox(True)
    bb_width = bb_3d.Max.X - bb_3d.Min.X

    # compute bounding box of all unrolled geometry
    all_2d_geo = []
    all_2d_geo.extend(unrolled_breps)
    for c in outside_curves:
        all_2d_geo.append(c)

    if not all_2d_geo:
        print("error: no 2D geometry to place")
        return 0

    # get unrolled bounding box
    bb_2d = all_2d_geo[0].GetBoundingBox(True)
    for geo in all_2d_geo[1:]:
        bb_2d.Union(geo.GetBoundingBox(True))

    # translation: move unrolled output to the right of the 3D part
    offset_x = bb_3d.Max.X + bb_width * PLACEMENT_GAP_FACTOR - bb_2d.Min.X
    # align Y to 3D part center
    offset_y = (bb_3d.Min.Y + bb_3d.Max.Y) / 2 - (bb_2d.Min.Y + bb_2d.Max.Y) / 2
    # flatten to Z=0
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

    # add outside cut curves
    for crv in outside_curves:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_outside) != System.Guid.Empty:
            count += 1

    # add inside cut curves
    for crv in inside_curves:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_inside) != System.Guid.Empty:
            count += 1

    # add bend lines (mark layer)
    for crv in unrolled_bend:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_mark) != System.Guid.Empty:
            count += 1

    # add ink curves / placement marks (mark layer)
    for crv in unrolled_ink:
        crv.Transform(xform)
        if sc.doc.Objects.AddCurve(crv, attr_mark) != System.Guid.Empty:
            count += 1

    # add bend angle text curves (mark layer)
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

    # get picked face normal for direction reference
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
    outer_sheet, inner_sheet, edge_faces = classify_faces(brep, thickness, face_index)
    print("faces: {} outer sheet, {} inner sheet, {} edge".format(
        len(outer_sheet), len(inner_sheet), len(edge_faces)))

    if not outer_sheet:
        print("error: no outer sheet faces identified")
        return

    # step 5: identify internal bends
    bends = identify_internal_bends(brep, outer_sheet, edge_faces)
    print("internal bends: {}".format(len(bends)))

    # step 6: find ink curves
    ink_curves = find_ink_curves(brep)
    print("ink curves found: {}".format(len(ink_curves)))

    # step 7: construct neutral axis surface
    neutral_axis = construct_neutral_axis(brep, outer_sheet, thickness, DEFAULT_K_FACTOR)
    if neutral_axis is None:
        return
    print("neutral axis surface: {} faces".format(neutral_axis.Faces.Count))

    # step 8-9: generate bend lines and compute angles
    bend_infos = generate_bend_lines(brep, bends, neutral_axis, outer_sheet, thickness, DEFAULT_K_FACTOR)
    determine_bend_directions(bend_infos, picked_normal)
    for info in bend_infos:
        print("  bend: {:.1f} {}".format(info["angle"], info["direction"]))

    # step 10: unroll
    unroll_result = unroll_neutral_axis(neutral_axis, ink_curves, bend_infos)
    if unroll_result is None:
        return
    unrolled_breps, unrolled_ink, unrolled_bend = unroll_result
    print("unrolled: {} brep(s), {} ink curves, {} bend lines".format(
        len(unrolled_breps), len(unrolled_ink), len(unrolled_bend)))

    # step 11: classify unrolled boundary curves
    outside_curves, inside_curves = classify_unrolled_curves(unrolled_breps)
    print("cuts: {} outside, {} inside".format(len(outside_curves), len(inside_curves)))

    # step 12: create bend angle text
    text_curves = create_bend_text_curves(bend_infos, unrolled_bend)

    # ensure sublayers exist
    sublayers = ensure_sublayers()

    # step 13: place 2D output
    count = place_2d_output(
        brep, unrolled_breps, outside_curves, inside_curves,
        unrolled_ink, unrolled_bend, text_curves, sublayers,
    )

    sc.doc.Views.Redraw()
    print("unfold complete: {} curves placed on 2D geo sublayers".format(count))


if __name__ == "__main__":
    unfold_to_2d()
