#! python3
"""lay-flat: orient objects so a selected face sits on cplane, face-up.

usage:
  select objects first, then run script.
  pick a face to define orientation. all selected objects get the same transform.

options (on face-pick prompt):
  Copy=Yes/No    copy objects instead of moving them (sticky)
  Place=CPlane/UnderPart/Origin   where to place result (sticky)
    CPlane    - orient to active construction plane (default)
    UnderPart - lay on world XY, bbox center XY-aligned with original
    Origin    - lay on world XY, centered at world origin

alias: lay-flat -> _-RunPythonScript "path/to/lay-flat.py"
"""

import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
from Rhino.Geometry import (
    BoundingBox,
    Brep,
    Plane,
    Point3d,
    Transform,
    Vector3d,
)
from Rhino.Geometry.Intersect import Intersection
from Rhino.Input import GetResult
from Rhino.Input.Custom import GetObject, GetOption, OptionToggle
from Rhino.DocObjects import ObjectType

PLACEMENTS = ["CPlane", "UnderPart", "Origin"]


def face_centroid_and_normal(brep, face):
    """compute face centroid and outward normal from boundary vertices only.
    avoids AreaMassProperties, ClosestPoint, NormalAt — pure vertex math."""
    loop = face.OuterLoop
    if loop is None:
        return None, None

    # collect boundary vertices in order
    pts = []
    for trim in loop.Trims:
        edge = trim.Edge
        if edge is None:
            continue
        # use edge start point (in trim order)
        if trim.IsReversed():
            pts.append(edge.PointAtEnd)
        else:
            pts.append(edge.PointAtStart)

    if len(pts) < 3:
        return None, None

    # centroid: average of boundary vertices
    cx, cy, cz = 0, 0, 0
    for p in pts:
        cx += p.X
        cy += p.Y
        cz += p.Z
    n = len(pts)
    centroid = Point3d(cx / n, cy / n, cz / n)

    # normal: Newell's method for robust polygon normal
    nx, ny, nz = 0, 0, 0
    for i in range(n):
        curr = pts[i]
        nxt = pts[(i + 1) % n]
        nx += (curr.Y - nxt.Y) * (curr.Z + nxt.Z)
        ny += (curr.Z - nxt.Z) * (curr.X + nxt.X)
        nz += (curr.X - nxt.X) * (curr.Y + nxt.Y)
    normal = Vector3d(nx, ny, nz)
    normal.Unitize()

    # ensure outward: test point slightly outside should be outside the brep
    test_pt = centroid + normal * 0.01
    if brep.IsPointInside(test_pt, sc.doc.ModelAbsoluteTolerance, False):
        normal = -normal

    return centroid, normal


def compute_transform(normal, centroid, obj_ids, placement):
    """compute the lay-flat transform for the given placement mode.
    placement: 0=CPlane, 1=UnderPart, 2=Origin
    returns Transform."""

    if placement == 0:
        # CPlane: orient to active construction plane
        cplane = sc.doc.Views.ActiveView.ActiveViewport.ConstructionPlane()
        rotation = Transform.Rotation(normal, cplane.ZAxis, centroid)
        dist = Vector3d.Multiply(centroid - cplane.Origin, cplane.ZAxis)
        translation = Transform.Translation(
            Vector3d.Multiply(-dist, cplane.ZAxis)
        )
        return translation * rotation

    # UnderPart and Origin both target world +Z
    target_z = Vector3d(0, 0, 1)
    rotation = Transform.Rotation(normal, target_z, centroid)

    # compute original bbox center (before transform) for UnderPart
    if placement == 1:
        orig_bbox = BoundingBox.Empty
        for obj_id in obj_ids:
            obj = sc.doc.Objects.FindId(obj_id)
            if obj and obj.Geometry:
                orig_bbox.Union(obj.Geometry.GetBoundingBox(True))
        orig_center = orig_bbox.Center

    # compute bbox after rotation (in memory only)
    rotated_bbox = BoundingBox.Empty
    for obj_id in obj_ids:
        obj = sc.doc.Objects.FindId(obj_id)
        if obj and obj.Geometry:
            geo_copy = obj.Geometry.Duplicate()
            geo_copy.Transform(rotation)
            rotated_bbox.Union(geo_copy.GetBoundingBox(True))

    if not rotated_bbox.IsValid:
        print("warning: could not compute bounding box, falling back to CPlane")
        return compute_transform(normal, centroid, obj_ids, 0)

    rot_center = rotated_bbox.Center

    # centroid is the rotation pivot so it doesn't move during rotation —
    # use its Z to place the selected face exactly at Z=0
    face_z = centroid.Z

    if placement == 1:
        # UnderPart: XY align with original bbox center, selected face at Z=0
        translation = Transform.Translation(
            orig_center.X - rot_center.X,
            orig_center.Y - rot_center.Y,
            -face_z,
        )
    else:
        # Origin: center at world origin, selected face at Z=0
        translation = Transform.Translation(
            -rot_center.X,
            -rot_center.Y,
            -face_z,
        )

    return translation * rotation


def lay_flat():
    # grab pre-selected objects, or ask user to select
    pre = [obj.Id for obj in sc.doc.Objects.GetSelectedObjects(False, False)]

    if not pre:
        go_sel = GetObject()
        go_sel.SetCommandPrompt("select objects to lay flat")
        go_sel.GeometryFilter = (
            ObjectType.Brep
            | ObjectType.Surface
            | ObjectType.Extrusion
            | ObjectType.InstanceReference
            | ObjectType.Mesh
            | ObjectType.SubD
            | ObjectType.Curve
            | ObjectType.Point
        )
        go_sel.EnablePreSelect(False, True)
        go_sel.SubObjectSelect = False
        go_sel.GroupSelect = True
        go_sel.GetMultiple(1, 0)
        if go_sel.CommandResult() != Rhino.Commands.Result.Success:
            return
        pre = [go_sel.Object(i).ObjectId for i in range(go_sel.ObjectCount)]

    if not pre:
        print("nothing selected")
        return

    # clear selection so sub-face picking works
    rs.UnselectAllObjects()
    sc.doc.Views.Redraw()

    # read sticky settings
    copy_mode = sc.sticky.get("lay_flat_copy", False)
    placement_idx = sc.sticky.get("lay_flat_placement", 0)

    # options step: let user toggle settings, press Enter to proceed
    while True:
        gopt = GetOption()
        gopt.SetCommandPrompt(
            "lay-flat options (Copy={} Place={}) press Enter to pick face".format(
                "Yes" if copy_mode else "No", PLACEMENTS[placement_idx]
            )
        )
        gopt.AcceptNothing(True)

        copy_toggle = OptionToggle(copy_mode, "No", "Yes")
        gopt.AddOptionToggle("Copy", copy_toggle)
        gopt.AddOptionList("Place", PLACEMENTS, placement_idx)

        result = gopt.Get()

        if result == GetResult.Option:
            copy_mode = copy_toggle.CurrentValue
            opt = gopt.Option()
            if opt and opt.CurrentListOptionIndex >= 0:
                placement_idx = opt.CurrentListOptionIndex
            sc.sticky["lay_flat_copy"] = copy_mode
            sc.sticky["lay_flat_placement"] = placement_idx
            continue

        if result == GetResult.Nothing:
            break  # Enter pressed, proceed to face pick

        return  # Escape/cancel

    # pick orientation face
    go = GetObject()
    go.SetCommandPrompt("which face up?")
    go.GeometryFilter = ObjectType.Surface
    go.SubObjectSelect = True
    go.EnablePreSelect(False, True)
    go.DeselectAllBeforePostSelect = False
    go.GroupSelect = False
    go.Get()

    if go.CommandResult() != Rhino.Commands.Result.Success:
        return

    objref = go.Object(0)
    brep = objref.Brep()

    if brep is None:
        print("error: not a brep/polysurface")
        return

    face = objref.Face()
    if face is None:
        if brep.Faces.Count == 1:
            face = brep.Faces[0]
        else:
            print("error: click a face (ctrl+shift+click for sub-face)")
            return

    centroid, normal = face_centroid_and_normal(brep, face)
    if centroid is None:
        print("error: could not compute face geometry")
        return
    print("  lay-flat centroid: {:.4f}, {:.4f}, {:.4f}".format(centroid.X, centroid.Y, centroid.Z))
    print("  lay-flat normal:   {:.4f}, {:.4f}, {:.4f}".format(normal.X, normal.Y, normal.Z))

    # make sure the reference face's parent object is in the list
    ref_id = objref.ObjectId
    if ref_id not in pre:
        pre.append(ref_id)

    # compute transform for the chosen placement mode
    xform = compute_transform(normal, centroid, pre, placement_idx)

    # apply to all objects
    delete_original = not copy_mode
    result_ids = []
    for obj_id in pre:
        new_id = sc.doc.Objects.Transform(obj_id, xform, delete_original)
        if new_id:
            result_ids.append(new_id)

    # select output geometry
    for rid in result_ids:
        sc.doc.Objects.Select(rid)

    sc.doc.Views.Redraw()
    tag = " (copy)" if copy_mode else ""
    mode = PLACEMENTS[placement_idx]
    print("laid flat{}: {} object(s) [{}]".format(tag, len(result_ids), mode))


if __name__ == "__main__":
    lay_flat()
