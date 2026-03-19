#! python3
"""lay-flat: orient objects so a selected face sits on cplane, face-up.

usage:
  select objects first, then run script.
  pick a face to define orientation. all selected objects get the same transform.

options (on face-pick prompt):
  Copy=Yes/No    copy objects instead of moving them (sticky)
  Place=CPlane/UnderPart/Origin/Select   where to place result (sticky)
    CPlane    - orient to active construction plane (default)
    UnderPart - lay on world XY, bbox center XY-aligned with original
    Origin    - lay on world XY, centered at world origin
    Select    - lay flat then pick a point to place (live wireframe preview)
  Color=Yes/No   apply custom display color to output objects (sticky)
    first time: opens color picker. remembered for future runs.
    PickColor  - (when Color=Yes) re-open picker to change color

alias: lay-flat -> _-RunPythonScript "path/to/lay-flat.py"
"""

import System.Drawing

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
from Rhino.Input.Custom import GetObject, GetOption, GetPoint, OptionToggle
from Rhino.DocObjects import ObjectType

PLACEMENTS = ["CPlane", "UnderPart", "Origin", "Select"]
PREVIEW_COLOR = System.Drawing.Color.FromArgb(180, 180, 180)


class PlaceGetPoint(GetPoint):
    """GetPoint with dynamic wireframe preview of geometry following the cursor."""

    def __init__(self, geo_list, base_center):
        super().__init__()
        self.geo_list = geo_list  # list of Brep/Curve/etc geometry
        self.base_center = base_center  # Point3d — current bbox center of geo

    def OnDynamicDraw(self, e):
        offset = e.CurrentPoint - self.base_center
        xf = Transform.Translation(Vector3d(offset))
        for geo in self.geo_list:
            moved = geo.Duplicate()
            moved.Transform(xf)
            if isinstance(moved, Brep):
                e.Display.DrawBrepWires(moved, PREVIEW_COLOR)
            else:
                try:
                    e.Display.DrawObject(moved, xf)
                except Exception:
                    pass


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
    placement: 0=CPlane, 1=UnderPart, 2=Origin, 3=Select (same as Origin initially)
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
        # Origin / Select: center at world origin, selected face at Z=0
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
    color_on = sc.sticky.get("lay_flat_color_on", False)
    color_rgb = sc.sticky.get("lay_flat_color_rgb", None)  # (r, g, b) or None

    # options step: let user toggle settings, press Enter to proceed
    while True:
        gopt = GetOption()
        color_tag = ""
        if color_on and color_rgb:
            color_tag = " #{:02X}{:02X}{:02X}".format(*color_rgb)
        gopt.SetCommandPrompt(
            "lay-flat options (Copy={} Place={} Color={}{}) press Enter to pick face".format(
                "Yes" if copy_mode else "No",
                PLACEMENTS[placement_idx],
                "Yes" if color_on else "No",
                color_tag,
            )
        )
        gopt.AcceptNothing(True)

        copy_toggle = OptionToggle(copy_mode, "No", "Yes")
        gopt.AddOptionToggle("Copy", copy_toggle)
        gopt.AddOptionList("Place", PLACEMENTS, placement_idx)
        color_toggle = OptionToggle(color_on, "No", "Yes")
        gopt.AddOptionToggle("Color", color_toggle)
        pick_idx = -1
        if color_on:
            pick_idx = gopt.AddOption("PickColor")

        result = gopt.Get()

        if result == GetResult.Option:
            copy_mode = copy_toggle.CurrentValue
            old_color_on = color_on
            color_on = color_toggle.CurrentValue

            opt = gopt.Option()
            if opt and opt.CurrentListOptionIndex >= 0:
                placement_idx = opt.CurrentListOptionIndex

            # user just turned color on and has no saved color — show picker
            if color_on and not old_color_on and not color_rgb:
                picked = rs.GetColor()
                if picked:
                    color_rgb = (picked[0], picked[1], picked[2])
                else:
                    color_on = False  # cancelled picker, leave color off

            # user clicked PickColor
            if color_on and opt and opt.Index == pick_idx:
                default = System.Drawing.Color.FromArgb(*color_rgb) if color_rgb else None
                picked = rs.GetColor(default)
                if picked:
                    color_rgb = (picked[0], picked[1], picked[2])

            sc.sticky["lay_flat_copy"] = copy_mode
            sc.sticky["lay_flat_placement"] = placement_idx
            sc.sticky["lay_flat_color_on"] = color_on
            if color_rgb:
                sc.sticky["lay_flat_color_rgb"] = color_rgb
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

    # Select mode: let user pick placement point with live preview
    if placement_idx == 3 and result_ids:
        # gather transformed geometry for preview
        geo_list = []
        bbox = BoundingBox.Empty
        for rid in result_ids:
            obj = sc.doc.Objects.FindId(rid)
            if obj and obj.Geometry:
                geo_list.append(obj.Geometry.Duplicate())
                bbox.Union(obj.Geometry.GetBoundingBox(True))
        base_center = bbox.Center

        gp = PlaceGetPoint(geo_list, base_center)
        gp.SetCommandPrompt("pick placement point")
        gp.Get()
        if gp.CommandResult() != Rhino.Commands.Result.Success:
            # cancelled — undo the transform by deleting results, but only if copied
            if copy_mode:
                for rid in result_ids:
                    sc.doc.Objects.Delete(rid, True)
            sc.doc.Views.Redraw()
            print("placement cancelled")
            return

        pick_pt = gp.Point()
        move = Transform.Translation(Vector3d(pick_pt - base_center))
        moved_ids = []
        for rid in result_ids:
            new_id = sc.doc.Objects.Transform(rid, move, True)
            if new_id:
                moved_ids.append(new_id)
        result_ids = moved_ids

    # apply custom color if enabled
    if color_on and color_rgb:
        obj_color = System.Drawing.Color.FromArgb(*color_rgb)
        for rid in result_ids:
            obj = sc.doc.Objects.FindId(rid)
            if obj:
                attr = obj.Attributes.Duplicate()
                attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
                attr.ObjectColor = obj_color
                sc.doc.Objects.ModifyAttributes(rid, attr, True)

    # select output geometry
    for rid in result_ids:
        sc.doc.Objects.Select(rid)

    sc.doc.Views.Redraw()
    tag = " (copy)" if copy_mode else ""
    mode = PLACEMENTS[placement_idx]
    color_tag = ""
    if color_on and color_rgb:
        color_tag = " color=#{:02X}{:02X}{:02X}".format(*color_rgb)
    print("laid flat{}: {} object(s) [{}]{}".format(tag, len(result_ids), mode, color_tag))


if __name__ == "__main__":
    lay_flat()
