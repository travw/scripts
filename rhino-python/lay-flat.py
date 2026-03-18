"""lay-flat: orient objects so a selected face sits on cplane, face-up.

usage:
  select objects first, then run script.
  pick a face to define orientation. all selected objects get the same transform.

alias: lay-flat -> _-RunPythonScript "path/to/lay-flat.py"
"""

import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
from Rhino.Geometry import AreaMassProperties, Plane, Transform, Vector3d, Point3d
from Rhino.Input.Custom import GetObject
from Rhino.DocObjects import ObjectType


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

    # face centroid
    amp = AreaMassProperties.Compute(face)
    if amp is None:
        print("error: couldn't compute face centroid")
        return
    centroid = amp.Centroid

    # outward normal at centroid
    rc, u, v = face.ClosestPoint(centroid)
    if not rc:
        print("error: closest point failed")
        return
    normal = face.NormalAt(u, v)
    if face.OrientationIsReversed:
        normal = -normal

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
