"""Create Inside cut / Mark / Outside cut sublayers under a selected layer."""
import rhinoscriptsyntax as rs
import scriptcontext as sc
import System.Drawing.Color as Color

def create_sublayers():
    # get all layer names for selection
    layers = rs.LayerNames()
    if not layers:
        print("no layers found.")
        return

    current = rs.CurrentLayer()
    layer = rs.ListBox(sorted(layers), "select parent layer", "create sublayers", current)
    if not layer:
        return

    sublayers = [
        ("Inside cut",  Color.FromArgb(255, 0, 255)),   # magenta
        ("Mark",        Color.FromArgb(0, 127, 0)),      # dark green
        ("Outside cut", Color.FromArgb(0, 0, 255)),      # blue
    ]

    for name, color in sublayers:
        full_name = "{}::{}".format(layer, name)
        if rs.IsLayer(full_name):
            print("layer already exists: {}".format(full_name))
        else:
            rs.AddLayer(full_name, color)
            print("created: {}".format(full_name))

create_sublayers()