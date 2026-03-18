#! python3
"""lay-flat: orient objects so a selected face sits on cplane, face-up.

usage:
  select objects first, then run script.
  pick a face to define orientation. all selected objects get the same transform.

alias: lay-flat -> _-RunPythonScript "path/to/lay-flat.py"
"""

import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
from Rhino.Geometry import (
    Brep,
    Plane,
    Point3d,
    Transform,
    Vector3d,
)
from Rhino.Geometry.Intersect import Intersection
from Rhino.Input.Custom import GetObject
from Rhino.DocObjects import ObjectType


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

    # now pick the orientation face
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
    if go.ObjectCount == 0:
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

    # active cplane
    cplane = sc.doc.Views.ActiveView.ActiveViewport.ConstructionPlane()

    # step 1: rotation around centroid to align face normal -> cplane +Z
    rotation = Transform.Rotation(normal, cplane.ZAxis, centroid)

    # step 2: translate along cplane Z only, so face centroid lands on cplane
    # (centroid doesn't move during rotation bc it's the pivot)
    dist = Vector3d.Multiply(centroid - cplane.Origin, cplane.ZAxis)
    translation = Transform.Translation(
        Vector3d.Multiply(-dist, cplane.ZAxis)
    )

    # combined: rotate then translate
    xform = translation * rotation

    # make sure the reference face's parent object is in the list
    ref_id = objref.ObjectId
    if ref_id not in pre:
        pre.append(ref_id)

    # apply to all objects
    count = 0
    for obj_id in pre:
        if sc.doc.Objects.Transform(obj_id, xform, True):
            count += 1

    sc.doc.Views.Redraw()
    print("laid flat: {} object(s)".format(count))


if __name__ == "__main__":
    lay_flat()
