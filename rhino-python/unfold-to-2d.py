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
import System.Windows.Forms
import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
import math
import Rhino.Geometry as rg
from Rhino.Geometry.Intersect import Intersection
from Rhino.Input.Custom import GetObject
from Rhino.DocObjects import ObjectType


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
STANDARD_GAUGES = [0.100, 0.125, 0.160, 0.190]
BEND_LABEL_HEIGHT = 1.0  # inches
BEND_LABEL_GAP = 0.125   # 1/8" gap between bend line and label edge
BEND_LABEL_MIN_HEIGHT = 0.3  # minimum shrink height for fallback placement

# debug log — collects all print output, optionally copies to clipboard or file
_debug_log = []
_debug_enabled = False
DEBUG_MODES = ["off", "clipboard", "file"]


def dbg(msg):
    """print to rhino command line and collect for debug output."""
    print(msg)
    _debug_log.append(msg)


def prompt_debug_mode():
    """command-line option for debug output. remembers last choice via sc.sticky."""
    global _debug_enabled
    prev = sc.sticky.get("unfold_debug_mode", "off")
    go = Rhino.Input.Custom.GetOption()
    go.SetCommandPrompt("Debug output")
    go.SetDefaultString(prev)
    for m in DEBUG_MODES:
        go.AddOption(m)
    go.AcceptNothing(True)
    result = go.Get()
    if result == Rhino.Input.GetResult.Option:
        choice = go.Option().EnglishName.lower()
    else:
        choice = prev
    sc.sticky["unfold_debug_mode"] = choice
    _debug_enabled = choice != "off"


def flush_debug_log():
    """output collected debug log per user preference."""
    if not _debug_enabled or not _debug_log:
        return
    mode = sc.sticky.get("unfold_debug_mode", "off")
    text = "\n".join(_debug_log)
    if mode == "clipboard":
        System.Windows.Forms.Clipboard.SetText(text)
        print("debug log ({} lines) copied to clipboard".format(len(_debug_log)))
    elif mode == "file":
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = rs.SaveFileName("Save debug log", "Text files (*.txt)|*.txt||",
                               filename="unfold_debug_{}.txt".format(ts))
        if path:
            with open(path, "w") as f:
                f.write(text)
            print("debug log saved to {}".format(path))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _doc_translate(curve, dx, dy, dz):
    """translate a curve using doc round-trip (CPython 3 workaround).
    in-memory Curve.Translate() doesn't work reliably in CPython 3."""
    xf = rg.Transform.Translation(dx, dy, dz)
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
        dbg("error: not a brep/polysurface")
        return None

    if not brep.IsSolid:
        dbg("error: part must be a closed polysurface (solid)")
        return None

    face = objref.Face()
    if face is None:
        if brep.Faces.Count == 1:
            face = brep.Faces[0]
        else:
            dbg("error: click a face (ctrl+shift+click for sub-face)")
            return None

    return brep, face.FaceIndex, objref.ObjectId


def get_face_outward_normal(brep, face_index):
    """get outward-pointing normal at the centroid of a brep face.
    uses DuplicateFace to ensure AreaMassProperties works on a Brep (not BrepFace)."""
    face = brep.Faces[face_index]
    face_brep = face.DuplicateFace(False)
    if face_brep is None:
        return None, None
    amp = rg.AreaMassProperties.Compute(face_brep)
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
    planar = rg.Brep.CreatePlanarBreps([outer_crv], tol)
    if planar and len(planar) > 0:
        return planar[0]
    # imperfect edges: retry with relaxed tolerance for slightly non-planar curves
    planar = rg.Brep.CreatePlanarBreps([outer_crv], tol * 100)
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
        ray = rg.LineCurve(rg.Line(start, end))
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
    amp = rg.AreaMassProperties.Compute(face_brep) if face_brep else None
    if amp is not None:
        centroid = amp.Centroid
        rc, u, v = face.ClosestPoint(centroid)
        if rc:
            normal = face.NormalAt(u, v)
            if face.OrientationIsReversed:
                normal = -normal
            hits = _shoot_thickness_ray(brep, face_index, centroid, normal, tol)
            dbg("  thickness phase 1: centroid hits={}".format(
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
            is_exterior = (pfr == rg.PointFaceRelation.Exterior or int(pfr) == 2)
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
                dbg("  thickness phase 2: found at sample ({},{})".format(ui, vi))
                return result
    dbg("  thickness phase 2: {} interior samples, no gauge hits".format(interior_count))

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
            dbg("  face {}: no centroid/normal, skipping".format(i))
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
            ray = rg.LineCurve(rg.Line(start, end))

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
                            dot = rg.Vector3d.Multiply(ni, nj)
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
                        dbg("  face {} <-> face {}: partner at {:.4f}\"".format(i, j, dist))
                        found = True
                        break
                    else:
                        # track nearest miss for diagnostics
                        if best_dist is None or abs(dist - thickness) < abs(best_dist - thickness):
                            best_dist = dist
                            best_j = j
                            _, nj = face_data[j]
                            best_dot = rg.Vector3d.Multiply(ni, nj) if nj is not None else None
                if found:
                    break
        if not found and best_dist is not None:
            dbg("  face {}: no partner (best: face {} dist={:.4f}\" dot={})".format(
                i, best_j, best_dist,
                "{:.2f}".format(best_dot) if best_dot is not None else "?"))
        elif not found:
            dbg("  face {}: no partner (no ray hits)".format(i))

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
                    dbg("warning: conflict coloring face {} and {} (both side {})".format(
                        current, partner, color[current]))
                continue
            color[partner] = 1 - color[current]
            queue.append(partner)

    # determine which color the picked face got (ref side)
    ref_color = color.get(picked_face_index, 0)
    side_a_indices = [fi for fi in sheet_faces if color.get(fi, 0) == ref_color]
    side_b_indices = [fi for fi in sheet_faces if color.get(fi, 0) != ref_color]
    dbg("  side A (ref): {} ({} faces)".format(side_a_indices, len(side_a_indices)))
    dbg("  side B:       {} ({} faces)".format(side_b_indices, len(side_b_indices)))

    if not side_a_indices or not side_b_indices:
        dbg("error: could not split sheet faces into 2 sides (A={}, B={})".format(
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
        joined = rg.Brep.JoinBreps(face_breps, tol)
        if joined and len(joined) == 1:
            return joined[0]
        elif joined and len(joined) > 1:
            # try looser tolerance
            joined2 = rg.Brep.JoinBreps(list(joined), tol * 10)
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
        dbg("error: failed to join sheet faces into sides")
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
    result = rg.Brep.CreatePlanarBreps(curves, tol)
    if not result or len(result) == 0:
        result = rg.Brep.CreatePlanarBreps(curves, tol * 100)
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
    offset_vec = rg.Vector3d(-normal.X * offset_dist,
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
    amp = rg.AreaMassProperties.Compute(face_brep)
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
    joined = rg.Curve.JoinCurves(curves, tol * 10)
    if joined is None or len(joined) == 0:
        return fallback

    raw_loop = None
    for crv in joined:
        if not crv.IsClosed:
            continue
        contain = crv.Contains(centroid_nap, nap_plane, tol)
        if contain == rg.PointContainment.Inside:
            if raw_loop is None:
                raw_loop = crv
            else:
                # pick largest loop containing centroid (outer boundary, not window holes)
                amp_new = rg.AreaMassProperties.Compute(crv)
                amp_old = rg.AreaMassProperties.Compute(raw_loop)
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
                cf_amp = rg.AreaMassProperties.Compute(cf_brep)
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
    raw_breps = rg.Brep.CreatePlanarBreps([raw_loop], tol)
    if not raw_breps or len(raw_breps) == 0:
        raw_breps = rg.Brep.CreatePlanarBreps([raw_loop], tol * 100)
    if not raw_breps or len(raw_breps) == 0:
        return raw_loop

    trimmed_brep = raw_breps[0]
    for tgt, pp_line in bend_map.items():
        pp_dir = rg.Vector3d(pp_line.Direction)
        pp_dir.Unitize()
        trim_normal = rg.Vector3d.CrossProduct(pp_dir, nap_plane.Normal)
        trim_normal.Unitize()
        pp_mid = pp_line.PointAt(pp_line.ClosestParameter(centroid_nap))
        if rg.Vector3d.Multiply(centroid_nap - pp_mid, trim_normal) < 0:
            trim_normal = -trim_normal
        trim_plane = rg.Plane(pp_mid, trim_normal)

        # trim both orientations and pick the piece containing the centroid
        pieces_pos = trimmed_brep.Trim(trim_plane, tol)
        trim_plane_flip = rg.Plane(pp_mid, -trim_normal)
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
                amp_s = rg.AreaMassProperties.Compute(piece)
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
        seg_dir = rg.Vector3d(pts[i + 1].X - pts[i].X,
                           pts[i + 1].Y - pts[i].Y,
                           pts[i + 1].Z - pts[i].Z)
        seg_len = seg_dir.Length
        if seg_len < tol:
            continue
        seg_dir.Unitize()
        mid = rg.Point3d((pts[i].X + pts[i + 1].X) / 2,
                      (pts[i].Y + pts[i + 1].Y) / 2,
                      (pts[i].Z + pts[i + 1].Z) / 2)
        for tgt, pp_line in bend_map.items():
            t = pp_line.ClosestParameter(mid)
            dist = mid.DistanceTo(pp_line.PointAt(t))
            if dist > tol * 10:
                continue
            # check parallelism: segment must be nearly parallel to PP line
            pp_dir = rg.Vector3d(pp_line.Direction)
            pp_dir.Unitize()
            dot = abs(rg.Vector3d.Multiply(seg_dir, pp_dir))
            if dot < 0.999:
                continue  # angled segment (corner/notch) — don't snap
            # material check: verify adjacent face has material at this location
            adj_face = ref_side.Faces[tgt]
            adj_loop = adj_face.OuterLoop.To3dCurve()
            if adj_loop is not None:
                adj_normal = face_normals[tgt]
                adj_ov = rg.Vector3d(-adj_normal.X * offset_dist,
                                   -adj_normal.Y * offset_dist,
                                   -adj_normal.Z * offset_dist)
                adj_loop_nap = _doc_translate(adj_loop, adj_ov.X, adj_ov.Y, adj_ov.Z)
                adj_plane = face_planes[tgt]
                mid_on_adj = adj_plane.ClosestPoint(mid)
                if adj_loop_nap and adj_loop_nap.IsClosed:
                    contain = adj_loop_nap.Contains(mid_on_adj, adj_plane, tol)
                    if contain != rg.PointContainment.Inside:
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
    pts[-1] = rg.Point3d(pts[0].X, pts[0].Y, pts[0].Z)

    # remove consecutive duplicates and short segments (trim corner artifacts)
    cleaned = [pts[0]]
    for p in pts[1:]:
        if p.DistanceTo(cleaned[-1]) > offset_dist * 0.5:
            cleaned.append(p)
    if len(cleaned) > 1 and cleaned[-1].DistanceTo(cleaned[0]) < offset_dist * 0.5:
        cleaned.pop()
    if len(cleaned) < 3:
        return boundary
    cleaned.append(rg.Point3d(cleaned[0].X, cleaned[0].Y, cleaned[0].Z))

    return rg.PolylineCurve(cleaned)


def construct_neutral_axis(ref_side, thickness, original_brep=None, other_side=None,
                           partners=None, quiet=False):
    """construct the neutral axis surface from plane geometry.
    for each planar face in ref_side, computes the offset plane (t/2 inward),
    then builds each face's boundary from:
      - bend edges: plane-plane intersection of adjacent offset planes
      - perimeter edges: original edge translated to offset plane
    produces sharp corners at bends with exact edge connectivity."""
    tol = sc.doc.ModelAbsoluteTolerance
    offset_dist = thickness / 2.0

    def _log(msg):
        if not quiet:
            dbg(msg)

    # step 1: compute offset plane for each face
    face_planes = {}
    face_normals = {}
    for fi in range(ref_side.Faces.Count):
        face = ref_side.Faces[fi]
        plane_tol = max(tol * 10, 0.01)  # loosen for near-planar faces
        rc, plane = face.TryGetPlane(plane_tol)
        if not rc:
            _log("warning: face {} is not planar, skipping".format(fi))
            continue
        # get outward normal for this face in the ref_side context
        face_brep = face.DuplicateFace(False)
        amp = rg.AreaMassProperties.Compute(face_brep)
        if amp is None:
            _log("warning: face {} AreaMassProperties failed, skipping".format(fi))
            continue
        centroid = amp.Centroid
        rc2, u, v = face.ClosestPoint(centroid)
        if not rc2:
            _log("warning: face {} ClosestPoint failed, skipping".format(fi))
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
            amp_c = rg.AreaMassProperties.Compute(face_brep_c)
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
                    if rg.Vector3d.Multiply(to_partner, normal) > 0:
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
        face_planes[fi] = rg.Plane(offset_origin, plane.XAxis, plane.YAxis)
        face_normals[fi] = normal

    if len(face_planes) < 1:
        _log("error: no planar faces found in ref_side")
        return None

    # step 2: build each neutral axis face using robust helpers
    _log("=== neutral axis construction ===")
    _log("  ref_side has {} faces, {} have offset planes".format(
        ref_side.Faces.Count, len(face_planes)))

    # map ref_side face indices to original brep face indices by centroid matching
    orig_map = {}
    if original_brep is not None:
        for rfi in range(ref_side.Faces.Count):
            rf_brep = ref_side.Faces[rfi].DuplicateFace(False)
            rf_amp = rg.AreaMassProperties.Compute(rf_brep)
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
        amp_check = rg.AreaMassProperties.Compute(face_brep_check)
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
            _log("  {}: no outer loop → SKIPPED".format(fl))
            nas_skip += 1
            continue

        # skip small transition faces (they're in the polysurface for connectivity
        # but shouldn't generate their own NAS face)
        if fi in skipped_faces:
            face_brep_area = face.DuplicateFace(False)
            amp_area = rg.AreaMassProperties.Compute(face_brep_area)
            area_val = amp_area.Area if amp_area else 0
            _log("  {}: area {:.4f} < {:.4f} min → SKIPPED (transition face)".format(
                fl, area_val, min_area))
            nas_skip += 1
            continue

        # build NAS face boundary via BrepPlane intersection + PP trimming
        boundary = _build_nas_boundary(face, fi, face_planes, face_normals,
                                        offset_dist, ref_side, skipped_faces, tol,
                                        original_brep=original_brep)

        if boundary is None:
            _log("  {}: boundary construction failed → SKIPPED".format(fl))
            nas_skip += 1
            continue

        # collect inner loops (window openings) translated to offset plane
        offset_vec = rg.Vector3d(-normal.X * offset_dist,
                               -normal.Y * offset_dist,
                               -normal.Z * offset_dist)
        all_curves = [boundary]
        inner_count = 0
        for li in range(face.Loops.Count):
            lp = face.Loops[li]
            if lp.LoopType == rg.BrepLoopType.Outer:
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
            _log("  {}: {} trims{} → OK".format(
                fl, face.OuterLoop.Trims.Count, inner_str))
            nas_ok += 1
        else:
            _log("  {}: {} trims → CreatePlanarBreps FAILED".format(
                fl, face.OuterLoop.Trims.Count))
            nas_skip += 1

    _log("  result: {} of {} faces OK".format(nas_ok, nas_ok + nas_skip))

    if not neutral_faces:
        _log("error: could not create any neutral axis faces")
        return None

    # step 4: join all faces into a polysurface
    if len(neutral_faces) == 1:
        result = neutral_faces[0]
    else:
        joined = rg.Brep.JoinBreps(neutral_faces, tol)
        if joined and len(joined) == 1:
            result = joined[0]
        elif joined and len(joined) > 1:
            # faces didn't all join — try looser tolerance
            joined2 = rg.Brep.JoinBreps(joined, tol * 10)
            if joined2 and len(joined2) == 1:
                result = joined2[0]
            else:
                # merge into one brep
                pieces = list(joined2) if joined2 else list(joined)
                result = pieces[0]
                for pi in range(1, len(pieces)):
                    result.Join(pieces[pi], tol * 10, True)
                _log("warning: neutral axis joined with loose tolerance ({} pieces)".format(
                    len(pieces)))
        else:
            _log("warning: could not join neutral axis faces")
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

        amp_a = rg.AreaMassProperties.Compute(face_a)
        amp_b = rg.AreaMassProperties.Compute(face_b)
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

        dot = rg.Vector3d.Multiply(na, nb)
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

    # pre-compute NAS face centroids and planes
    nas_centroids = {}  # fi -> Point3d
    nas_planes = {}     # fi -> Plane
    for fi in range(nas.Faces.Count):
        face = nas.Faces[fi]
        face_brep = face.DuplicateFace(False)
        amp = rg.AreaMassProperties.Compute(face_brep)
        if amp:
            nas_centroids[fi] = amp.Centroid
        plane_tol = max(sc.doc.ModelAbsoluteTolerance * 100, 0.1)
        rc, plane = face.TryGetPlane(plane_tol)
        if rc:
            nas_planes[fi] = plane

    # collect internal edges for curve_na extent (optional, best-effort)
    internal_edges = []
    for ei in range(nas.Edges.Count):
        edge = nas.Edges[ei]
        if len(edge.AdjacentFaces()) == 2:
            internal_edges.append(edge)

    for i, info in enumerate(bend_infos):
        bend_mid = info["mid_pt"]
        ca = info["centroid_a"]  # ref_side face A centroid
        cb = info["centroid_b"]  # ref_side face B centroid

        # match NAS faces by centroid proximity to ref_side face centroids
        # (NAS faces are offset t/2 from ref_side faces, centroids are close)
        best_a = (-1, float("inf"))
        best_b = (-1, float("inf"))
        for fi, nc in nas_centroids.items():
            da = ca.DistanceTo(nc)
            db = cb.DistanceTo(nc)
            if da < best_a[1]:
                best_a = (fi, da)
            if db < best_b[1]:
                best_b = (fi, db)

        # if both matched same face, re-pick second-best for b
        if best_a[0] == best_b[0]:
            best_b = (-1, float("inf"))
            for fi, nc in nas_centroids.items():
                if fi == best_a[0]:
                    continue
                db = cb.DistanceTo(nc)
                if db < best_b[1]:
                    best_b = (fi, db)

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
                    axis_line = rg.Line(pp_line.PointAt(t0), pp_line.PointAt(t1))
                else:
                    # use 3D bend curve extent projected onto PP line
                    crv = info["curve_3d"]
                    t0 = pp_line.ClosestParameter(crv.PointAtStart)
                    t1 = pp_line.ClosestParameter(crv.PointAtEnd)
                    axis_line = rg.Line(pp_line.PointAt(t0), pp_line.PointAt(t1))

        if axis_line is None:
            # last resort fallback
            crv = info["curve_3d"]
            axis_line = rg.Line(crv.PointAtStart, crv.PointAtEnd)

        info["curve_na"] = rg.LineCurve(axis_line)
        info["na_axis"] = axis_line

        dbg("  bend {}: {:.1f}° → NAS faces {}↔{}, axis len={:.2f}\"".format(
            i, info["angle"], fa, fb, axis_line.Length))


def unroll_by_rotation(neutral_axis_brep, ink_curves, thickness=0.125, picked_na_face=0):
    """unroll the NAS by rotating planar faces around bend axes.
    adjacency and bend axes are derived directly from NAS internal edges.
    returns (flat_brep, outside_curves, inside_curves,
             flat_bend_curves, flat_ink_curves, flat_normal, nas_edge_bends)
    or None."""
    tol = sc.doc.ModelAbsoluteTolerance
    nas = neutral_axis_brep
    n_faces = nas.Faces.Count

    # --- get face planes (needed before adjacency) ---
    face_planes = {}
    for fi in range(n_faces):
        face = nas.Faces[fi]
        plane_tol = max(tol * 100, 0.1)
        rc, plane = face.TryGetPlane(plane_tol)
        if rc:
            amp = rg.AreaMassProperties.Compute(face.DuplicateFace(False))
            if amp:
                mid_u = face.Domain(0).Mid
                mid_v = face.Domain(1).Mid
                n = face.NormalAt(mid_u, mid_v)
                if face.OrientationIsReversed:
                    n.Reverse()
                plane = rg.Plane(amp.Centroid, plane.XAxis, plane.YAxis)
                if rg.Vector3d.Multiply(plane.Normal, n) < 0:
                    plane = rg.Plane(plane.Origin, plane.XAxis, -plane.YAxis)
            face_planes[fi] = plane

    if len(face_planes) < n_faces:
        dbg("  warning: only {} of {} faces have planes".format(
            len(face_planes), n_faces))

    # --- build adjacency directly from NAS internal edges ---
    # collect ALL internal edges between non-coplanar face pairs, then
    # deduplicate: one adjacency entry per face pair (longest edge for rotation),
    # but keep all edge curves for flat bend line output.
    bend_edges_by_pair = {}  # (min_fa, max_fb) -> list of edge curves
    bend_angles_by_pair = {}  # (min_fa, max_fb) -> angle
    for ei in range(nas.Edges.Count):
        edge = nas.Edges[ei]
        adj = edge.AdjacentFaces()
        if len(adj) != 2:
            continue
        fa, fb = adj[0], adj[1]
        if fa not in face_planes or fb not in face_planes:
            continue
        na = face_planes[fa].Normal
        nb = face_planes[fb].Normal
        dot_n = max(-1.0, min(1.0, rg.Vector3d.Multiply(na, nb)))
        if abs(dot_n) > 0.99:
            continue  # coplanar, not a bend

        pair_key = (min(fa, fb), max(fa, fb))
        edge_crv = edge.DuplicateCurve()
        bend_edges_by_pair.setdefault(pair_key, []).append(edge_crv)
        if pair_key not in bend_angles_by_pair:
            # use abs(dot) because NAS face normals in a joined polysurface
            # may not be consistently oriented (some inward, some outward)
            abs_dot = abs(dot_n)
            bend_angle = round(180.0 - math.degrees(math.acos(abs_dot)), 1)
            bend_angles_by_pair[pair_key] = bend_angle

    # build adjacency (one entry per face pair, longest edge for rotation axis)
    adjacency = {}
    nas_edge_bends = []  # one entry per face pair (for labels)
    all_bend_edges = []  # all edge curves grouped by pair (for flat output)
    for (fa, fb), edges in bend_edges_by_pair.items():
        # pick longest edge for rotation axis
        longest = max(edges, key=lambda c: c.GetLength())
        axis_line = rg.Line(longest.PointAtStart, longest.PointAtEnd)
        bend_angle = bend_angles_by_pair[(fa, fb)]

        bend_entry = {
            "edge_crv": longest,
            "all_edges": edges,
            "axis": axis_line,
            "angle": bend_angle,
            "fa": fa,
            "fb": fb,
        }
        nas_edge_bends.append(bend_entry)
        all_bend_edges.append(bend_entry)
        adjacency.setdefault(fa, []).append((fb, bend_entry, axis_line))
        adjacency.setdefault(fb, []).append((fa, bend_entry, axis_line))

    dbg("  {} unique face-pair bends from NAS edges".format(len(nas_edge_bends)))

    # --- BFS: flatten faces onto XY ---
    transforms = {}  # face_idx -> Transform (3D -> flat XY)
    visited = set()

    # seed: picked NAS face stays in place
    seed = picked_na_face if picked_na_face in face_planes else 0
    if seed not in face_planes:
        for fi in face_planes:
            seed = fi
            break
    seed_plane = face_planes[seed]
    transforms[seed] = rg.Transform.Identity
    flat_normal = seed_plane.Normal  # target normal for all flattened faces
    visited.add(seed)

    # helper: transform a direction vector through a transform
    def xform_normal(n, xf):
        tip = rg.Point3d(n.X, n.Y, n.Z)
        org = rg.Point3d(0, 0, 0)
        tip.Transform(xf)
        org.Transform(xf)
        v = rg.Vector3d(tip - org)
        v.Unitize()
        return v

    # bend deduction: each face accumulates a shift toward the seed
    deduction = thickness * 0.75  # total strip width per bend (t * 0.375 each side)
    face_shifts = {seed: rg.Vector3d(0, 0, 0)}

    bfs_path = ["face {}".format(seed)]
    bfs_order = [seed]  # face indices in BFS traversal order (for iterative merge)
    queue = [seed]
    while queue:
        current = queue.pop(0)
        current_xform = transforms[current]
        for neighbor, bend_entry, axis_line in adjacency.get(current, []):
            if neighbor in visited:
                continue
            if neighbor not in face_planes:
                continue

            # transform the bend axis to the flattened state
            p1 = rg.Point3d(axis_line.From)
            p2 = rg.Point3d(axis_line.To)
            p1.Transform(current_xform)
            p2.Transform(current_xform)
            # store flat axis endpoints — these define the bend line in flat space
            bend_entry["flat_p1"] = rg.Point3d(p1)
            bend_entry["flat_p2"] = rg.Point3d(p2)
            axis_dir = rg.Vector3d(p2 - p1)
            axis_dir.Unitize()

            # direct rotation: compute exact angle to align neighbor
            # normal with flat_normal — no stored angles or sign guessing
            nei_n = xform_normal(face_planes[neighbor].Normal, current_xform)

            # project both normals onto plane perpendicular to bend axis
            nei_proj = nei_n - axis_dir * rg.Vector3d.Multiply(nei_n, axis_dir)
            flat_proj = flat_normal - axis_dir * rg.Vector3d.Multiply(flat_normal, axis_dir)
            nei_proj.Unitize()
            flat_proj.Unitize()

            # signed angle from nei_proj to flat_proj around axis_dir
            dot_val = rg.Vector3d.Multiply(nei_proj, flat_proj)
            cross = rg.Vector3d.CrossProduct(nei_proj, flat_proj)
            sin_val = rg.Vector3d.Multiply(cross, axis_dir)
            flatten_angle = math.atan2(sin_val, dot_val)

            rot = rg.Transform.Rotation(flatten_angle, axis_dir, p1)
            best_combined = rg.Transform.Multiply(rot, current_xform)
            chosen_angle = flatten_angle

            # verify neighbor unfolds to opposite side of bend edge
            cur_amp = rg.AreaMassProperties.Compute(nas.Faces[current].DuplicateFace(False))
            cur_c = rg.Point3d(cur_amp.Centroid)
            cur_c.Transform(current_xform)
            nei_amp = rg.AreaMassProperties.Compute(nas.Faces[neighbor].DuplicateFace(False))
            nei_c = rg.Point3d(nei_amp.Centroid)
            nei_c.Transform(best_combined)

            # side test: cross(axis, centroid_vec) · flat_normal gives signed side
            cur_side = rg.Vector3d.Multiply(
                rg.Vector3d.CrossProduct(axis_dir, rg.Vector3d(cur_c - p1)), flat_normal)
            nei_side = rg.Vector3d.Multiply(
                rg.Vector3d.CrossProduct(axis_dir, rg.Vector3d(nei_c - p1)), flat_normal)

            if cur_side * nei_side > 0:
                # same side — flip by adding pi
                rot2 = rg.Transform.Rotation(flatten_angle + math.pi, axis_dir, p1)
                best_combined = rg.Transform.Multiply(rot2, current_xform)
                chosen_angle = flatten_angle + math.pi

            transforms[neighbor] = best_combined

            # compute bend deduction shift: neighbor moves toward current
            # recompute nei_c with final transform
            nei_c2 = rg.Point3d(nei_amp.Centroid)
            nei_c2.Transform(best_combined)
            # shift direction: project (cur_c - nei_c2) onto plane perp to axis
            # this directly gives the "toward current" direction, no sign guessing
            to_current = rg.Vector3d(cur_c - nei_c2)
            # remove axis-parallel component
            axis_comp = rg.Vector3d.Multiply(to_current, axis_dir)
            shift_dir = to_current - axis_dir * axis_comp
            if shift_dir.Length > tol:
                shift_dir.Unitize()
                shift_vec = shift_dir * deduction
            else:
                # faces are coaxial (centroids on same axis-perp line), fallback
                shift_vec = rg.Vector3d(0, 0, 0)
            face_shifts[neighbor] = face_shifts[current] + shift_vec
            dbg("    shift {}->{}: vec=({:.4f},{:.4f},{:.4f}) cumulative=({:.4f},{:.4f},{:.4f})".format(
                current, neighbor, shift_vec.X, shift_vec.Y, shift_vec.Z,
                face_shifts[neighbor].X, face_shifts[neighbor].Y, face_shifts[neighbor].Z))

            # store rotation info for bend direction detection
            bend_entry["rotation_angle"] = chosen_angle
            bend_entry["bfs_from"] = current
            bend_entry["bfs_to"] = neighbor

            visited.add(neighbor)
            queue.append(neighbor)
            bfs_order.append(neighbor)
            bfs_path.append("face {} ({:.1f}°)".format(
                neighbor, math.degrees(chosen_angle)))

    dbg("  BFS: {}".format(" → ".join(bfs_path)))
    if len(transforms) < n_faces:
        dbg("  warning: BFS reached {} of {} faces".format(
            len(transforms), n_faces))

    # --- compute flat axes for non-BFS-traversed bends ---
    for entry in nas_edge_bends:
        if "flat_p1" not in entry:
            fa, fb = entry["fa"], entry["fb"]
            xf = transforms.get(fa) or transforms.get(fb)
            if xf:
                cp1 = rg.Point3d(entry["axis"].From)
                cp2 = rg.Point3d(entry["axis"].To)
                cp1.Transform(xf)
                cp2.Transform(xf)
                entry["flat_p1"] = cp1
                entry["flat_p2"] = cp2

    # --- build face-to-bends adjacency for trim computation ---
    face_bends = {}  # fi -> list of (bend_entry, axis_dir, perp, face_side)
    half_deduction = thickness * 0.375
    for entry in nas_edge_bends:
        fp1 = entry.get("flat_p1")
        fp2 = entry.get("flat_p2")
        if fp1 is None or fp2 is None:
            continue
        axis_dir = rg.Vector3d(fp2 - fp1)
        axis_dir.Unitize()
        perp = rg.Vector3d.CrossProduct(flat_normal, axis_dir)
        perp.Unitize()
        entry["_pre_shift_p1"] = rg.Point3d(fp1)
        entry["_pre_shift_p2"] = rg.Point3d(fp2)
        entry["_axis_dir"] = axis_dir
        entry["_perp"] = perp

    # --- transform face breps to flat, apply deduction shift (faces will overlap) ---
    flat_faces = []
    flat_face_breps = {}
    fn = flat_normal
    dbg("  target normal: ({:.4f},{:.4f},{:.4f})".format(fn.X, fn.Y, fn.Z))
    for fi in range(n_faces):
        if fi not in transforms:
            continue
        result_n = xform_normal(face_planes[fi].Normal, transforms[fi])
        dot = rg.Vector3d.Multiply(result_n, flat_normal)
        face_brep = nas.Faces[fi].DuplicateFace(False)
        face_brep.Transform(transforms[fi])
        # apply bend deduction shift (no trimming — union resolves overlaps)
        if fi in face_shifts:
            shift = face_shifts[fi]
            if shift.Length > 0.0001:
                face_brep.Transform(rg.Transform.Translation(shift))
        flat_faces.append(face_brep)
        flat_face_breps[fi] = face_brep
        amp = rg.AreaMassProperties.Compute(face_brep)
        seed_tag = " (SEED)" if fi == seed else ""
        dbg("    face {}: area={:.1f}{}".format(fi, amp.Area if amp else 0, seed_tag))

    # --- detect cross-bend holes via un-shifted face merge ---
    # un-shifted face breps (rotation only, no deduction shift) tile perfectly
    # at bend edges. joining + MergeCoplanarFaces turns paired notches into
    # inner loops, revealing through-holes that cross bend lines.
    cross_bend_holes = []
    unshifted_face_breps = []
    unshifted_fi_list = []
    proj_to_seed = rg.Transform.PlanarProjection(face_planes[seed])
    for fi in range(n_faces):
        if fi not in transforms:
            continue
        fb_us = nas.Faces[fi].DuplicateFace(False)
        fb_us.Transform(transforms[fi])
        fb_us.Transform(proj_to_seed)  # kill FP drift from sequential rotations
        unshifted_face_breps.append(fb_us)
        unshifted_fi_list.append(fi)

    if len(unshifted_face_breps) > 1:
        # collect single-face inner loop centroids for filtering
        individual_hole_centroids = []
        for fb_us in unshifted_face_breps:
            if fb_us.Faces.Count > 0:
                for li in range(fb_us.Faces[0].Loops.Count):
                    lp = fb_us.Faces[0].Loops[li]
                    if lp.LoopType == rg.BrepLoopType.Inner:
                        lc = lp.To3dCurve()
                        if lc:
                            lc_amp = rg.AreaMassProperties.Compute(lc)
                            if lc_amp:
                                individual_hole_centroids.append(lc_amp.Centroid)

        joined_us = rg.Brep.JoinBreps(unshifted_face_breps, tol * 10)
        if joined_us and len(joined_us) >= 1:
            merged_us = joined_us[0]
            # try merging coplanar faces with increasing tolerance
            merge_ok = False
            for merge_tol in [tol, tol * 10, tol * 100]:
                if merged_us.MergeCoplanarFaces(merge_tol):
                    merge_ok = True
                    break
            if merge_ok:
                # extract inner loops from merged result
                all_merged_holes = []
                for fi2 in range(merged_us.Faces.Count):
                    face2 = merged_us.Faces[fi2]
                    for li2 in range(face2.Loops.Count):
                        lp2 = face2.Loops[li2]
                        if lp2.LoopType == rg.BrepLoopType.Inner:
                            hc = lp2.To3dCurve()
                            if hc is not None:
                                all_merged_holes.append(hc)
                # filter: keep only holes NOT already found on individual faces
                for hc in all_merged_holes:
                    hc_amp = rg.AreaMassProperties.Compute(hc)
                    if hc_amp is None:
                        continue
                    hc_centroid = hc_amp.Centroid
                    is_existing = False
                    for ec in individual_hole_centroids:
                        if hc_centroid.DistanceTo(ec) < tol * 100:
                            is_existing = True
                            break
                    if not is_existing:
                        cross_bend_holes.append(hc)
            else:
                dbg("  warning: MergeCoplanarFaces failed, skipping cross-bend hole detection")

    # shift cross-bend hole curves to match deducted pattern
    shifted_cross_bend_holes = []
    for hc in cross_bend_holes:
        # split at bend lines (un-shifted positions)
        split_params = []
        for entry in nas_edge_bends:
            pre_p1 = entry.get("_pre_shift_p1")
            pre_p2 = entry.get("_pre_shift_p2")
            if pre_p1 is None or pre_p2 is None:
                continue
            bend_dir = rg.Vector3d(pre_p2 - pre_p1)
            bend_dir.Unitize()
            ext_line = rg.LineCurve(pre_p1 - bend_dir * 500, pre_p2 + bend_dir * 500)
            ccx = Intersection.CurveCurve(hc, ext_line, tol, tol)
            if ccx:
                for ix in range(ccx.Count):
                    evt = ccx[ix]
                    if evt.IsPoint:
                        split_params.append(evt.ParameterA)

        if not split_params:
            # doesn't cross any bend -- add as-is
            shifted_cross_bend_holes.append(hc)
            continue

        split_params.sort()
        # remove duplicate params (from nearly-coincident bend lines)
        unique_params = [split_params[0]]
        for sp in split_params[1:]:
            if abs(sp - unique_params[-1]) > tol:
                unique_params.append(sp)

        segments = hc.Split(unique_params)
        if not segments or len(segments) < 2:
            # split failed, add un-shifted
            shifted_cross_bend_holes.append(hc)
            continue

        # shift each segment by its face's deduction
        shifted_segs = []
        for seg in segments:
            mid = seg.PointAt(seg.Domain.Mid)
            best_fi = None
            best_dist = float("inf")
            for idx, fb_us in enumerate(unshifted_face_breps):
                cp = fb_us.ClosestPoint(mid)
                d = mid.DistanceTo(cp)
                if d < best_dist:
                    best_dist = d
                    best_fi = unshifted_fi_list[idx]
            seg_copy = seg.DuplicateCurve()
            if best_fi is not None:
                shift = face_shifts.get(best_fi, rg.Vector3d(0, 0, 0))
                if shift.Length > 0.0001:
                    seg_copy.Transform(rg.Transform.Translation(shift))
            shifted_segs.append(seg_copy)

        # rejoin into closed curve
        rejoined = rg.Curve.JoinCurves(shifted_segs, tol * 100)
        if rejoined:
            for rc in rejoined:
                if rc.IsClosed:
                    shifted_cross_bend_holes.append(rc)
                else:
                    rc2 = rc.DuplicateCurve()
                    if rc2.MakeClosed(tol * 100):
                        shifted_cross_bend_holes.append(rc2)
        else:
            # rejoin failed, add un-shifted
            shifted_cross_bend_holes.append(hc)

    if cross_bend_holes:
        dbg("  cross-bend holes: {} detected, {} shifted".format(
            len(cross_bend_holes), len(shifted_cross_bend_holes)))

    # --- merge overlapping faces into single outline, preserving inner loops ---
    boundary_by_fi = {}  # face index -> outer boundary curve
    inner_loop_curves = shifted_cross_bend_holes[:]  # start with cross-bend holes
    for fi in flat_face_breps:
        fb = flat_face_breps[fi]
        if fb.Faces.Count > 0:
            face0 = fb.Faces[0]
            for li in range(face0.Loops.Count):
                loop = face0.Loops[li]
                loop_crv = loop.To3dCurve()
                if loop_crv is None:
                    continue
                if loop.LoopType == rg.BrepLoopType.Outer:
                    boundary_by_fi[fi] = loop_crv
                elif loop.LoopType == rg.BrepLoopType.Inner:
                    inner_loop_curves.append(loop_crv)
        else:
            edges = [fb.Edges[ei].DuplicateCurve() for ei in range(fb.Edges.Count)]
            joined = rg.Curve.JoinCurves(edges, tol * 10)
            if joined:
                boundary_by_fi[fi] = max(joined, key=lambda c: c.GetLength())
    dbg("  extracted {} outer boundaries, {} inner loops (holes)".format(
        len(boundary_by_fi), len(inner_loop_curves)))

    # iterative pairwise union in BFS order — each face overlaps its parent
    # (much more robust than all-at-once union which fails on narrow overlaps)
    merged_outline = None
    if len(boundary_by_fi) > 0:
        # start with seed face boundary
        start_fi = bfs_order[0] if bfs_order[0] in boundary_by_fi else next(iter(boundary_by_fi))
        merged_outline = boundary_by_fi[start_fi]
        merge_count = 1
        for fi in bfs_order[1:]:
            if fi not in boundary_by_fi:
                continue
            merged = False
            for union_tol in [tol, tol * 10, tol * 100]:
                result = rg.Curve.CreateBooleanUnion(
                    [merged_outline, boundary_by_fi[fi]], union_tol)
                if result and len(result) > 0:
                    merged_outline = max(result, key=lambda c: c.GetLength())
                    merge_count += 1
                    merged = True
                    break
            if not merged:
                dbg("  warning: pairwise union failed for face {}".format(fi))
        dbg("  boundary union: {} of {} faces merged (BFS iterative)".format(
            merge_count, len(boundary_by_fi)))

    # create merged brep: outer boundary + inner loop holes
    merged_brep = None
    if merged_outline is not None:
        # collect all curves for CreatePlanarBreps: outer + inner loops
        all_planar_curves = [merged_outline] + inner_loop_curves
        merged_breps = rg.Brep.CreatePlanarBreps(all_planar_curves, tol)
        if merged_breps and len(merged_breps) > 0:
            # pick the brep that contains the outer boundary (largest area)
            merged_brep = max(merged_breps, key=lambda b:
                rg.AreaMassProperties.Compute(b).Area
                if rg.AreaMassProperties.Compute(b) else 0)
            amp_merged = rg.AreaMassProperties.Compute(merged_brep)
            n_holes = sum(1 for fi2 in range(merged_brep.Faces.Count)
                         for li2 in range(merged_brep.Faces[fi2].Loops.Count)
                         if merged_brep.Faces[fi2].Loops[li2].LoopType == rg.BrepLoopType.Inner)
            dbg("  merged flat pattern: area={:.1f}, {} holes".format(
                amp_merged.Area if amp_merged else 0, n_holes))

    # --- apply deduction shifts to bend axis endpoints ---
    # the bend line goes where the two trimmed faces meet:
    #   axis + S_current + perp * sign_toward_current * half_deduction
    for entry in nas_edge_bends:
        fa, fb = entry["fa"], entry["fb"]
        shift_a = face_shifts.get(fa, rg.Vector3d(0, 0, 0))
        shift_b = face_shifts.get(fb, rg.Vector3d(0, 0, 0))
        # current face = closer to seed = smaller shift
        if shift_a.Length <= shift_b.Length:
            bend_shift = rg.Vector3d(shift_a)
            current_fi = fa
        else:
            bend_shift = rg.Vector3d(shift_b)
            current_fi = fb
        # add half_deduction correction toward the current face
        perp = entry.get("_perp")
        fp1_pre = entry.get("_pre_shift_p1")
        if perp is not None and fp1_pre is not None and current_fi in face_planes:
            # which side is current face on? use original NAS face centroid (pre-transform)
            # transformed to flat space
            cur_plane_centroid = rg.Point3d(face_planes[current_fi].Origin)
            cur_plane_centroid.Transform(transforms[current_fi])
            to_cur = rg.Vector3d(cur_plane_centroid - fp1_pre)
            sign_c = 1.0 if rg.Vector3d.Multiply(to_cur, perp) > 0 else -1.0
            bend_shift += perp * sign_c * half_deduction
        if "flat_p1" in entry:
            entry["flat_p1"] += bend_shift
            entry["flat_p2"] += bend_shift

    # --- build flat bend lines: extend axes, project onto seed plane, trim by NAS outline ---
    # use merged brep for trimming (no overlaps), fall back to joined faces
    if merged_brep is not None:
        trim_brep = merged_brep
    else:
        joined = rg.Brep.JoinBreps([fb for fb in flat_faces], tol * 10)
        trim_brep = joined[0] if joined and len(joined) > 0 else flat_faces[0]
    seed_plane_for_proj = face_planes[seed]
    proj_xform = rg.Transform.PlanarProjection(seed_plane_for_proj)

    flat_bend_curves = []
    for bi, entry in enumerate(nas_edge_bends):
        fp1 = entry.get("flat_p1")
        fp2 = entry.get("flat_p2")
        entry["flat_curves"] = []
        if fp1 is None or fp2 is None:
            continue

        # extend to long line, project onto seed plane, trim by NAS outline
        direction = rg.Vector3d(fp2 - fp1)
        direction.Unitize()
        long_p1 = fp1 - direction * 1000
        long_p2 = fp2 + direction * 1000
        long_line = rg.LineCurve(long_p1, long_p2)
        long_line.Transform(proj_xform)

        rc_int = Intersection.CurveBrep(long_line, trim_brep, tol)
        if rc_int and len(rc_int) >= 3:
            overlap_curves = rc_int[1]
            if overlap_curves and len(overlap_curves) > 0:
                # join collinear segments that CurveBrep splits unnecessarily
                joined = rg.Curve.JoinCurves(overlap_curves, tol)
                result = []
                for jc in (joined if joined else overlap_curves):
                    simplified = jc.Simplify(rg.CurveSimplifyOptions.All, tol, tol * 10)
                    result.append(simplified if simplified else jc)
                for oc in result:
                    entry["flat_curves"].append(oc)
                    flat_bend_curves.append(oc)
                continue

        # fallback: untrimmed line between the axis points
        fallback = rg.LineCurve(fp1, fp2)
        entry["flat_curves"].append(fallback)
        flat_bend_curves.append(fallback)

    # --- transform ink curves (same rotation as their NAS face, then project to flat plane) ---
    flat_ink_curves = []
    seed_plane = face_planes[seed]
    proj_to_plane = rg.Transform.PlanarProjection(seed_plane)
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
            ink_copy.Transform(proj_to_plane)
            # apply bend deduction shift
            shift = face_shifts.get(best_fi, rg.Vector3d(0, 0, 0))
            if shift.Length > 0.0001:
                ink_copy.Transform(rg.Transform.Translation(shift))
            flat_ink_curves.append(ink_copy)

    # --- bake flat pattern to visual verification layer ---
    bake_layer = "03 - Bake"
    if not rs.IsLayer(bake_layer):
        rs.AddLayer(bake_layer)
    layer_idx = sc.doc.Layers.FindByFullPath(bake_layer, -1)
    baked_guids = []
    bake_target = merged_brep if merged_brep is not None else flat_faces[0]
    a = Rhino.DocObjects.ObjectAttributes()
    a.LayerIndex = layer_idx
    a.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
    a.ObjectColor = System.Drawing.Color.FromArgb(100, 255, 100)
    guid = sc.doc.Objects.AddBrep(bake_target, a)
    if guid != System.Guid.Empty:
        baked_guids.append(guid)
    dbg("  baked {} flat faces to '{}'".format(len(flat_faces), bake_layer))

    # extract outside/inside cut curves from merged brep
    outside_curves = []
    inside_curves = []
    source_brep = merged_brep if merged_brep is not None else (flat_faces[0] if flat_faces else None)
    if source_brep is None:
        dbg("error: no flat geometry produced")
        return None
    for fi2 in range(source_brep.Faces.Count):
        face2 = source_brep.Faces[fi2]
        for li2 in range(face2.Loops.Count):
            loop = face2.Loops[li2]
            loop_crv = loop.To3dCurve()
            if loop_crv is None:
                continue
            if loop.LoopType == rg.BrepLoopType.Outer:
                outside_curves.append(loop_crv)
            elif loop.LoopType == rg.BrepLoopType.Inner:
                inside_curves.append(loop_crv)
    dbg("  output curves: {} outside, {} inside (holes)".format(
        len(outside_curves), len(inside_curves)))
    flat_brep = source_brep
    return flat_brep, outside_curves, inside_curves, flat_bend_curves, flat_ink_curves, flat_normal, nas_edge_bends, transforms, face_shifts


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

        dot = rg.Vector3d.Multiply(inside_vec, picked_normal)
        info["direction"] = "UP" if dot > 0 else "DN"


def find_ink_curves(brep):
    """find curves on '09 - Ink lines' layer that are associated with the brep.
    returns list of (curve_guid, curve_geometry)."""
    ink_layer = "09 - Ink lines"
    if not rs.IsLayer(ink_layer):
        return []

    tol = sc.doc.ModelAbsoluteTolerance * 10
    result = []

    # collect objects from main layer AND all sublayers (e.g. 09 - Ink lines::Cabin sides)
    all_objects = []
    layer_idx = sc.doc.Layers.FindByFullPath(ink_layer, -1)
    if layer_idx < 0:
        return []
    layers_to_search = [layer_idx]
    children = sc.doc.Layers[layer_idx].GetChildren()
    if children:
        layers_to_search.extend([ch.Index for ch in children])
    for li in layers_to_search:
        objs = sc.doc.Objects.FindByLayer(sc.doc.Layers[li])
        if objs:
            all_objects.extend(objs)

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





def _label_fits(label_curves, outside_crv, inside_crvs, plane, tol):
    """test whether all label curves sit on solid material.
    checks bbox corners + center against outside boundary (must be inside)
    and all inside holes (must not be inside any)."""
    if not label_curves or outside_crv is None:
        return False
    # compute bounding box of all label curves
    bbox = rg.BoundingBox.Empty
    for crv in label_curves:
        bbox.Union(crv.GetBoundingBox(True))
    if not bbox.IsValid:
        return False
    # test 5 points: 4 corners + center
    mn = bbox.Min
    mx = bbox.Max
    test_pts = [
        rg.Point3d(mn.X, mn.Y, mn.Z),
        rg.Point3d(mx.X, mn.Y, mn.Z),
        rg.Point3d(mx.X, mx.Y, mn.Z),
        rg.Point3d(mn.X, mx.Y, mn.Z),
        rg.Point3d((mn.X + mx.X) / 2, (mn.Y + mx.Y) / 2, (mn.Z + mx.Z) / 2),
    ]
    for pt in test_pts:
        # must be inside outside boundary
        if outside_crv.Contains(pt, plane, tol) != rg.PointContainment.Inside:
            return False
        # must not be inside any hole
        for hole in inside_crvs:
            if hole.Contains(pt, plane, tol) == rg.PointContainment.Inside:
                return False
    return True


def _make_label_curves(text_content, text_height, mid_pt, tang_proj, target_y,
                       pn, side_sign, gap, slide_offset, label_ds, mecsoft):
    """create centered text curves offset from bend line midpoint.

    approach: create text at World XY origin, bbox-center it, then transform
    to the target position. offset = side_sign * (0.5*text_height + gap) in
    the target_y direction. text is centered along the bend line.

    returns list of transformed curves, or [] on failure."""
    xy_plane = rg.Plane.WorldXY
    ds = label_ds.Duplicate()
    ds.TextHeight = text_height
    if mecsoft is not None:
        ds.Font = mecsoft
    te = rg.TextEntity.Create(text_content, xy_plane, ds, False, 0, 0)
    if te is None:
        return []
    te.TextHeight = text_height
    if mecsoft is not None:
        te.Font = mecsoft
    curves = te.CreateCurves(ds, False)
    if not curves or len(curves) == 0:
        return []
    curves = list(curves)

    # step 1: compute bbox at origin and center it
    bbox = rg.BoundingBox.Empty
    for crv in curves:
        bbox.Union(crv.GetBoundingBox(True))
    if not bbox.IsValid:
        return []
    bbox_center = rg.Point3d(
        (bbox.Min.X + bbox.Max.X) / 2,
        (bbox.Min.Y + bbox.Max.Y) / 2,
        0)
    center_xform = rg.Transform.Translation(-rg.Vector3d(bbox_center))

    # step 2: build target plane at offset position
    # offset perpendicular to bend line: half the text height + gap
    perp_offset = side_sign * (0.5 * text_height + gap)
    # slide along bend line for collision avoidance
    target_origin = mid_pt + target_y * perp_offset + tang_proj * slide_offset

    # build plane: X = along bend (tang_proj), Y = perpendicular (target_y)
    # ensure plane normal faces picked_normal direction
    target_plane = rg.Plane(target_origin, tang_proj, target_y)
    if rg.Vector3d.Multiply(target_plane.Normal, pn) < 0:
        # flip X axis to reverse normal direction
        target_plane = rg.Plane(target_origin, -tang_proj, target_y)

    # step 3: compose centering + placement transform
    place_xform = rg.Transform.PlaneToPlane(xy_plane, target_plane)
    combined = place_xform * center_xform
    for crv in curves:
        crv.Transform(combined)
    return curves


def _get_label_bbox(curves):
    """compute bounding box of label curves."""
    bbox = rg.BoundingBox.Empty
    for crv in curves:
        bbox.Union(crv.GetBoundingBox(True))
    return bbox


def _try_label_at(text_content, text_height, mid_pt, tang_proj, target_y,
                  pn, side_sign, gap, slide_offset, outside_crv, inside_crvs,
                  flat_plane, label_ds, mecsoft, placed_bboxes, tol):
    """try placing a label at a specific position. returns (curves, bbox) or (None, None).
    slide_offset shifts the label along tang_proj (for collision avoidance)."""
    curves = _make_label_curves(text_content, text_height, mid_pt, tang_proj,
                                target_y, pn, side_sign, gap, slide_offset,
                                label_ds, mecsoft)
    if not curves:
        return None, None
    bbox = _get_label_bbox(curves)
    if not bbox.IsValid:
        return None, None
    # check containment on solid material
    if not _label_fits(curves, outside_crv, inside_crvs, flat_plane, tol):
        return None, None
    # check collision with already-placed labels
    if placed_bboxes:
        expanded = rg.BoundingBox(
            rg.Point3d(bbox.Min.X - BEND_LABEL_GAP, bbox.Min.Y - BEND_LABEL_GAP, bbox.Min.Z),
            rg.Point3d(bbox.Max.X + BEND_LABEL_GAP, bbox.Max.Y + BEND_LABEL_GAP, bbox.Max.Z))
        for other in placed_bboxes:
            if (expanded.Min.X < other.Max.X and expanded.Max.X > other.Min.X and
                    expanded.Min.Y < other.Max.Y and expanded.Max.Y > other.Min.Y):
                return None, None
    return curves, bbox


def _place_bend_label(text_content, main_crv, pn, outside_crv, inside_crvs,
                      flat_plane, label_ds, mecsoft, mark_attr, placed_bboxes):
    """place bend label on solid material with fallback chain.
    returns (guids, bbox) or ([], None). appends bbox to placed_bboxes on success."""
    tol = sc.doc.ModelAbsoluteTolerance

    mid_pt = main_crv.PointAt(main_crv.Domain.Mid)

    # compute tangent direction (normalized to consistent orientation)
    tangent = main_crv.TangentAt(main_crv.Domain.Mid)
    tangent.Unitize()
    if abs(tangent.Y) >= abs(tangent.X) and abs(tangent.Y) >= abs(tangent.Z):
        if tangent.Y < 0:
            tangent = -tangent
    elif abs(tangent.X) >= abs(tangent.Z):
        if tangent.X < 0:
            tangent = -tangent
    else:
        if tangent.Z < 0:
            tangent = -tangent

    # project tangent perpendicular to picked_normal
    tang_proj = tangent - pn * rg.Vector3d.Multiply(tangent, pn)
    if tang_proj.Length < 1e-6:
        tang_proj = tangent
    tang_proj.Unitize()

    # perpendicular direction (across bend line)
    target_y = rg.Vector3d.CrossProduct(pn, tang_proj)
    target_y.Unitize()

    # rotated 90 degrees: swap tang_proj and target_y
    tang_proj_rot = rg.Vector3d(target_y)
    target_y_rot = rg.Vector3d(-tang_proj)

    # try placement strategies in order
    # each: (height, tang_proj, target_y, side_sign)
    strategies = []
    for height in [BEND_LABEL_HEIGHT, 0.75, 0.5, BEND_LABEL_MIN_HEIGHT]:
        strategies.append((height, tang_proj, target_y, +1))
        strategies.append((height, tang_proj, target_y, -1))
        if height == BEND_LABEL_HEIGHT:
            strategies.append((height, tang_proj_rot, target_y_rot, +1))
            strategies.append((height, tang_proj_rot, target_y_rot, -1))

    # for each strategy, try centered first, then slide along bend line
    bend_len = main_crv.GetLength()
    slide_offsets = [0.0]
    for s in [0.25, 0.5]:
        d = bend_len * s
        slide_offsets.extend([d, -d])

    for height, tp, ty, side_sign in strategies:
        for slide in slide_offsets:
            curves, bbox = _try_label_at(
                text_content, height, mid_pt, tp, ty, pn, side_sign,
                BEND_LABEL_GAP, slide, outside_crv, inside_crvs,
                flat_plane, label_ds, mecsoft, placed_bboxes, tol)
            if curves is not None:
                guids = _add_label_curves(curves, mark_attr)
                placed_bboxes.append(bbox)
                return guids, bbox

    # fallback: place at default position with "?" appended
    dbg("    warning: '{}' could not fit on solid material, marking suspect".format(
        text_content))
    suspect_text = text_content + "?"
    curves = _make_label_curves(suspect_text, BEND_LABEL_HEIGHT, mid_pt,
                                tang_proj, target_y, pn, +1, BEND_LABEL_GAP,
                                0.0, label_ds, mecsoft)
    if curves:
        guids = _add_label_curves(curves, mark_attr)
        bbox = _get_label_bbox(curves)
        placed_bboxes.append(bbox)
        return guids, bbox
    return [], None


def _add_label_curves(curves, mark_attr):
    """add label curves to doc, group them, return guids."""
    guids = []
    for crv in curves:
        guid = sc.doc.Objects.AddCurve(crv, mark_attr)
        if guid != System.Guid.Empty:
            guids.append(guid)
    if guids:
        grp = sc.doc.Groups.Add()
        for g in guids:
            sc.doc.Groups.AddToGroup(grp, g)
    return guids


def add_output(outside_curves, inside_curves, unrolled_bend, unrolled_ink,
               text_curves, sublayers):
    """add 2D curves to fabrication sublayers. all geometry is in flat space."""
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
    global _debug_log
    _debug_log = []  # reset each run
    # step 1: select part and pick face
    result = pick_part_and_face()
    if result is None:
        return
    brep, face_index, obj_id = result
    obj_name = rs.ObjectName(obj_id) or "(unnamed)"
    obj_layer = rs.ObjectLayer(obj_id) or ""
    dbg("=== unfold-to-2d ===")
    dbg("  part: {} [layer: {}]".format(obj_name, obj_layer))
    dbg("  {} faces total, picked face: index {}".format(brep.Faces.Count, face_index))

    # early validation: catch bad input before heavy computation
    if brep.Faces.Count < 4:
        dbg("error: part has only {} faces -- need at least 4 for sheet metal unfolding".format(
            brep.Faces.Count))
        return

    tol = sc.doc.ModelAbsoluteTolerance
    plane_check_tol = max(tol * 10, 0.01)
    planar_count = 0
    picked_is_planar = False
    for fi in range(brep.Faces.Count):
        ok, _ = brep.Faces[fi].TryGetPlane(plane_check_tol)
        if ok:
            planar_count += 1
            if fi == face_index:
                picked_is_planar = True

    if not picked_is_planar:
        dbg("error: picked face is not planar -- pick a flat sheet face, not a curved or edge face")
        return

    if planar_count < 2:
        dbg("error: only {} of {} faces are planar -- this script requires planar sheet metal faces".format(
            planar_count, brep.Faces.Count))
        return

    dbg("  validation: {}/{} faces planar, picked face OK".format(planar_count, brep.Faces.Count))

    picked_centroid, picked_normal = get_face_outward_normal(brep, face_index)
    if picked_normal is None:
        dbg("error: could not compute face normal")
        return
    dbg("  picked_normal: ({:.4f},{:.4f},{:.4f})".format(
        picked_normal.X, picked_normal.Y, picked_normal.Z))

    # step 2: detect thickness
    auto_thickness = detect_thickness(brep, face_index)
    thickness = prompt_thickness(auto_thickness)
    if thickness is None:
        return
    dbg("thickness: {}".format(thickness))

    # debug output option
    prompt_debug_mode()

    # step 3: classify faces
    dbg("=== face classification ===")
    sheet_faces, edge_faces, partners = classify_faces(brep, thickness)
    dbg("  {} sheet faces, {} edge faces".format(len(sheet_faces), len(edge_faces)))

    # print compact partner list
    seen_pairs = set()
    pair_strs = []
    for fi in sorted(partners.keys()):
        pair = tuple(sorted([fi, partners[fi]]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            pair_strs.append("{}↔{}".format(pair[0], pair[1]))
    dbg("  partners: {}".format(", ".join(pair_strs)))

    # recompute picked_normal sign from partner geometry (OrientationIsReversed
    # is unreliable in Rhino 8 CPython 3 -- double-flips some faces).
    # centroid-to-centroid gives reliable DIRECTION hint (which side is out),
    # then we apply that sign to the true geometric face normal.
    if face_index in partners:
        partner_fi = partners[face_index]
        partner_centroid, _ = get_face_outward_normal(brep, partner_fi)
        if partner_centroid is not None and picked_centroid is not None:
            outward_hint = rg.Vector3d(picked_centroid - partner_centroid)
            face = brep.Faces[face_index]
            rc, u, v = face.ClosestPoint(picked_centroid)
            if rc:
                raw_normal = face.NormalAt(u, v)
                if rg.Vector3d.Multiply(raw_normal, outward_hint) < 0:
                    raw_normal = -raw_normal
                picked_normal = raw_normal
                dbg("  picked_normal (partner-corrected): ({:.4f},{:.4f},{:.4f})".format(
                    picked_normal.X, picked_normal.Y, picked_normal.Z))

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
        dbg("error: need at least 2 sheet faces")
        return

    # step 4: join sheet faces -> 2 polysurfaces (graph-colored by partner pairs)
    # ref_side contains the picked face, other_side is the opposite
    side_a, side_b = join_sheet_faces(brep, sheet_faces, partners, face_index)
    if side_a is None:
        return

    dbg("  side A: {} faces, side B: {} faces".format(
        side_a.Faces.Count, side_b.Faces.Count))

    # try NAS from both sides, use whichever produces more faces
    nas_a = construct_neutral_axis(side_a, thickness, original_brep=brep,
                                    other_side=side_b, partners=partners, quiet=True)
    nas_b = construct_neutral_axis(side_b, thickness, original_brep=brep,
                                    other_side=side_a, partners=partners, quiet=True)
    count_a = nas_a.Faces.Count if nas_a else 0
    count_b = nas_b.Faces.Count if nas_b else 0

    if count_a >= count_b:
        ref_side, other_side = side_a, side_b
    else:
        ref_side, other_side = side_b, side_a

    # rebuild NAS from the winning side with output visible
    neutral_axis = construct_neutral_axis(ref_side, thickness, original_brep=brep,
                                          other_side=other_side,
                                          partners=partners)
    if neutral_axis is None:
        return
    dbg("  neutral axis: {} faces (other side had {})".format(
        neutral_axis.Faces.Count, min(count_a, count_b)))

    # step 7: find ink curves
    ink_curves = find_ink_curves(ref_side)
    dbg("  ink curves: {}".format(len(ink_curves)))

    # step 8: find NAS face closest to picked face (for BFS seed + alignment)
    picked_centroid, _ = get_face_outward_normal(brep, face_index)
    picked_na_face = 0
    if picked_centroid is not None:
        best_dist = float("inf")
        for nfi in range(neutral_axis.Faces.Count):
            nf_brep = neutral_axis.Faces[nfi].DuplicateFace(False)
            nf_amp = rg.AreaMassProperties.Compute(nf_brep)
            if nf_amp is not None:
                d = picked_centroid.DistanceTo(nf_amp.Centroid)
                if d < best_dist:
                    best_dist = d
                    picked_na_face = nfi

    # step 9: unroll NAS (adjacency + bend lines derived from NAS edges directly)
    dbg("=== unroll ===")
    unroll_result = unroll_by_rotation(neutral_axis, ink_curves, thickness, picked_na_face)
    if unroll_result is None:
        return
    flat_brep, outside_curves, inside_curves, unrolled_bend, unrolled_ink, flat_normal, nas_edge_bends, transforms, face_shifts = unroll_result
    dbg("  {} bend lines, {} ink, cuts: {} outside, {} inside".format(
        len(unrolled_bend), len(unrolled_ink),
        len(outside_curves), len(inside_curves)))

    # step 10: compute bend directions for press brake
    # UP = ram pushes up (valley fold from picked face side).
    # DN = flip part, bend from other side (mountain fold from picked face side).
    # method: orient NAS face normals outward using original brep, then use
    # cross-product of edge tangent x normal_A dotted with normal_B to
    # determine concavity (valley vs mountain from outside).
    tol = sc.doc.ModelAbsoluteTolerance
    plane_tol = max(tol * 10, 0.01)

    # orient NAS face normals outward using original brep
    oriented_normals = {}
    for fi in range(neutral_axis.Faces.Count):
        face_brep = neutral_axis.Faces[fi].DuplicateFace(False)
        ok, fplane = neutral_axis.Faces[fi].TryGetPlane(plane_tol)
        if not ok:
            continue
        famp = rg.AreaMassProperties.Compute(face_brep)
        if famp is None:
            continue
        n = rg.Vector3d(fplane.Normal)
        n.Unitize()
        # test point offset from NAS face centroid in normal direction
        # NAS is t/2 inside the part. offset by t puts us t/2 outside
        # if outward, or 3t/2 inside if inward.
        test_pt = famp.Centroid + n * thickness
        if brep.IsPointInside(test_pt, tol, False):
            n = -n  # was pointing inward, flip to outward
        oriented_normals[fi] = n

    for entry in nas_edge_bends:
        fa_idx, fb_idx = entry["fa"], entry["fb"]
        if fa_idx not in oriented_normals or fb_idx not in oriented_normals:
            entry["direction"] = "UP"
            continue
        n_a = oriented_normals[fa_idx]
        n_b = oriented_normals[fb_idx]
        edge_crv = entry["edge_crv"]
        edge_mid = edge_crv.PointAt(edge_crv.Domain.Mid)
        edge_tan = edge_crv.TangentAt(edge_crv.Domain.Mid)
        edge_tan.Unitize()
        # cross(edge_tangent, outward_normal_A) gives a vector perpendicular
        # to the edge in the plane of face A, pointing toward the "outside"
        # of face A at the edge
        perp = rg.Vector3d.CrossProduct(edge_tan, n_a)
        # dot with face B's outward normal: if positive, face B opens away
        # from outside (valley from outside = UP). if negative, mountain = DN.
        concavity_dot = rg.Vector3d.Multiply(perp, n_b)
        entry["direction"] = "UP" if concavity_dot > 0 else "DN"
        dbg("    bend {}↔{}: concavity_dot={:.4f} → {}".format(
            fa_idx, fb_idx, concavity_dot, entry["direction"]))
    dbg("=== bends ===")
    for entry in nas_edge_bends:
        dbg("  bend: {:.1f} {} (NAS faces {}↔{})".format(
            entry["angle"], entry["direction"], entry["fa"], entry["fb"]))

    # step 11: add curves to sublayers (all output is in flat space)
    sublayers = ensure_sublayers()

    # step 12: bend angle labels — placed on solid material with fallback chain
    mark_idx = sc.doc.Layers.FindByFullPath(sublayers["mark"], -1)
    mark_attr = Rhino.DocObjects.ObjectAttributes()
    mark_attr.LayerIndex = mark_idx
    label_ds = sc.doc.DimStyles.Current.Duplicate()
    label_ds.TextHeight = BEND_LABEL_HEIGHT
    mecsoft = Rhino.DocObjects.Font.FromQuartetProperties("MecSoft_Font-1", False, False)
    if mecsoft is not None:
        label_ds.Font = mecsoft

    # join boundary curves for containment testing
    tol = sc.doc.ModelAbsoluteTolerance
    outside_crv = None
    if outside_curves:
        joined = rg.Curve.JoinCurves(outside_curves, tol)
        if joined:
            raw_outside = max(joined, key=lambda c: c.GetLength())
            # inset outside boundary by 1/8" so labels have cushion from edge
            # try both offset directions, pick the one with smaller area (= inset)
            raw_amp = rg.AreaMassProperties.Compute(raw_outside)
            raw_area = raw_amp.Area if raw_amp else float("inf")
            outside_crv = raw_outside  # fallback
            for sign in [-1, 1]:
                off = raw_outside.Offset(rg.Plane.WorldXY, sign * BEND_LABEL_GAP,
                                         tol, rg.CurveOffsetCornerStyle.Sharp)
                if off and len(off) == 1 and off[0].IsClosed:
                    off_amp = rg.AreaMassProperties.Compute(off[0])
                    if off_amp and off_amp.Area < raw_area:
                        outside_crv = off[0]
                        break
    inside_holes = []
    if inside_curves:
        joined_inner = rg.Curve.JoinCurves(inside_curves, tol)
        if joined_inner:
            for hole in joined_inner:
                if not hole.IsClosed:
                    continue
                # expand holes outward by 1/8" so labels have cushion
                # try both directions, pick larger area (= expanded)
                hole_amp = rg.AreaMassProperties.Compute(hole)
                hole_area = hole_amp.Area if hole_amp else 0
                best_hole = hole
                for sign in [-1, 1]:
                    exp = hole.Offset(rg.Plane.WorldXY, sign * BEND_LABEL_GAP,
                                      tol, rg.CurveOffsetCornerStyle.Sharp)
                    if exp and len(exp) == 1 and exp[0].IsClosed:
                        exp_amp = rg.AreaMassProperties.Compute(exp[0])
                        if exp_amp and exp_amp.Area > hole_area:
                            best_hole = exp[0]
                            break
                inside_holes.append(best_hole)

    # get flat plane for containment (from flat_brep or picked_normal)
    flat_plane = None
    if flat_brep and flat_brep.Faces.Count > 0:
        ok, fp = flat_brep.Faces[0].TryGetPlane(max(tol * 100, 0.1))
        if ok:
            flat_plane = fp
    if flat_plane is None:
        flat_plane = rg.Plane(rg.Point3d.Origin, picked_normal)

    pn = rg.Vector3d(picked_normal)
    pn.Unitize()
    dbg("  label picked_normal: ({:.4f},{:.4f},{:.4f})".format(pn.X, pn.Y, pn.Z))
    dbg("  outside boundary: {}, {} holes".format(
        "found" if outside_crv else "MISSING", len(inside_holes)))

    placed_bboxes = []
    for entry in nas_edge_bends:
        flat_crvs = entry.get("flat_curves", [])
        if not flat_crvs:
            continue
        main_crv = max(flat_crvs, key=lambda c: c.GetLength())
        angle_int = int(round(entry["angle"]))
        direction = entry.get("direction", "UP")
        text_content = "{} {}".format(angle_int, direction)

        guids, bbox = _place_bend_label(text_content, main_crv, pn, outside_crv,
                                        inside_holes, flat_plane, label_ds, mecsoft,
                                        mark_attr, placed_bboxes)
        dbg("    label '{}': {} curves placed".format(text_content, len(guids)))

    # step 13: output cuts + bend/ink curves to sublayers
    count = add_output(outside_curves, inside_curves, unrolled_bend, unrolled_ink,
                       [], sublayers)
    dbg("  {} curves added to sublayers".format(count))

    sc.doc.Views.Redraw()
    flush_debug_log()


if __name__ == "__main__":
    unfold_to_2d()
