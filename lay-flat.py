"""lay-flat: orient polysurface so selected face sits on cplane, face-up.

usage:
  _-RunPythonScript "path/to/lay-flat.py"
  or create alias: lay-flat -> _-RunPythonScript "path/to/lay-flat.py"

workflow:
  1. click a face on a polysurface (ctrl+shift+click for sub-selection)
  2. object reorients so that face is coplanar with cplane, normal = +Z
  3. object body ends up below cplane (negative Z side)
  4. loops until enter/esc
"""

import Rhino
import scriptcontext as sc
from Rhino.Geometry import AreaMassProperties, Plane, Transform
from Rhino.Input.Custom import GetObject
from Rhino.DocObjects import ObjectType


def lay_flat():
    while True:
        go = GetObject()
        go.SetCommandPrompt("which face up? (enter when done)")
        go.GeometryFilter = ObjectType.Surface
        go.SubObjectSelect = True
        go.EnablePreSelect(False, True)
        go.DeselectAllBeforePostSelect = False
        go.AcceptNothing(True)
        go.GroupSelect = False
        go.Get()

        if go.CommandResult() != Rhino.Commands.Result.Success:
            break
        if go.ObjectCount == 0:
            break

        objref = go.Object(0)
        obj_id = objref.ObjectId
        brep = objref.Brep()

        if brep is None:
            print("error: not a brep/polysurface")
            continue

        face = objref.Face()
        if face is None:
            if brep.Faces.Count == 1:
                face = brep.Faces[0]
            else:
                print("error: click a face, not the whole object")
                print("       try ctrl+shift+click for sub-face selection")
                continue

        # face centroid
        amp = AreaMassProperties.Compute(face)
        if amp is None:
            print("error: couldn't compute face centroid")
            continue
        centroid = amp.Centroid

        # surface normal at centroid
        rc, u, v = face.ClosestPoint(centroid)
        if not rc:
            print("error: closest point failed")
            continue

        normal = face.NormalAt(u, v)

        # flip to outward-facing normal if face orientation is reversed
        if face.OrientationIsReversed:
            normal = -normal

        # source plane: face centroid, outward normal as Z axis
        source_plane = Plane(centroid, normal)

        # target: active construction plane
        cplane = sc.doc.Views.ActiveView.ActiveViewport.ConstructionPlane()

        # orient face -> cplane
        xform = Transform.PlaneToPlane(source_plane, cplane)
        sc.doc.Objects.Transform(obj_id, xform, True)
        sc.doc.Views.Redraw()
        print("laid flat")


if __name__ == "__main__":
    lay_flat()
