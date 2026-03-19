#! python3
"""
Dim3D.py - Create 3D linear dimension annotations between any two points
on a user-defined plane.

Workflow:
1. Select first point (start of measurement)
2. Select second point (end of measurement)
3. Select third point to define the dimension plane orientation (with preview)
   - Or use "LastPlane" option to reuse previously defined plane
4. Pick dimension line location with live preview (constrained to the defined plane)
5. Repeat from step 1 (Escape exits)

Dimensions are placed on the "06 - Annotations" layer.

For Rhino 8, vanilla Python scripting.

alias: dim3d -> _-RunPythonScript "path/to/dim-3d.py"
"""

import Rhino
import Rhino.Geometry as rg
import Rhino.Display as rd
import scriptcontext as sc
import System


ANNOTATIONS_LAYER = "06 - Annotations"
STICKY_PLANE_KEY = "dim3d_last_plane"


def format_dimension_text(distance, precision):
    """Format a dimension value with the given decimal precision."""
    p = max(0, min(precision, 6))
    return f"{distance:.{p}f}"


def draw_arrow(display, tip, direction, size, z_axis, color):
    """Draw an arrowhead at the given point."""
    direction.Unitize()
    perp = rg.Vector3d.CrossProduct(direction, z_axis)
    perp.Unitize()

    wing1 = -direction * size * 0.9 + perp * size * 0.3
    wing2 = -direction * size * 0.9 - perp * size * 0.3

    p1 = tip + wing1
    p2 = tip + wing2

    display.DrawLine(tip, p1, color, 1)
    display.DrawLine(tip, p2, color, 1)
    display.DrawLine(p1, p2, color, 1)


def draw_extension_lines(display, pt1, pt2, dim_pt1, dim_pt2, y_axis, offset_dist, ext_offset, ext_extension, color):
    """Draw extension lines from measured points to dimension line."""
    sign = 1 if offset_dist >= 0 else -1
    ext1_start = pt1 + y_axis * sign * ext_offset
    ext1_end = dim_pt1 + y_axis * sign * ext_extension
    ext2_start = pt2 + y_axis * sign * ext_offset
    ext2_end = dim_pt2 + y_axis * sign * ext_extension

    display.DrawLine(ext1_start, ext1_end, color, 1)
    display.DrawLine(ext2_start, ext2_end, color, 1)


def ensure_annotations_layer():
    """Ensure the annotations layer exists. Returns the layer index."""
    layer_index = sc.doc.Layers.FindByFullPath(ANNOTATIONS_LAYER, -1)
    if layer_index < 0:
        import rhinoscriptsyntax as rs
        rs.AddLayer(ANNOTATIONS_LAYER, System.Drawing.Color.FromArgb(0, 128, 0))
        layer_index = sc.doc.Layers.FindByFullPath(ANNOTATIONS_LAYER, -1)
    return layer_index


class OrientationGetPoint(Rhino.Input.Custom.GetPoint):
    """Custom GetPoint with dynamic plane/dimension preview for orientation selection."""

    def __init__(self, pt1, pt2, dim_style):
        super().__init__()
        self.pt1 = pt1
        self.pt2 = pt2
        self.dim_style = dim_style
        self.distance = pt1.DistanceTo(pt2)
        self.midpoint = (pt1 + pt2) / 2
        self.precision = dim_style.LengthResolution

        # Base vector for dimension direction
        self.x_axis = pt2 - pt1
        self.x_axis.Unitize()

        # Drawing parameters
        self.text_height = dim_style.TextHeight
        self.arrow_size = dim_style.ArrowLength
        self.ext_offset = dim_style.ExtensionLineOffset
        self.ext_extension = dim_style.ExtensionLineExtension

        # Default preview offset distance (proportional to dimension length)
        self.preview_offset = max(self.distance * 0.15, self.text_height * 3)

    def OnDynamicDraw(self, e):
        """Draw dimension preview as cursor moves to show resulting plane orientation."""

        current_pt = e.CurrentPoint

        # Calculate plane from pt1, pt2, and current cursor position
        temp_vec = current_pt - self.pt1
        z_axis = rg.Vector3d.CrossProduct(self.x_axis, temp_vec)

        # Check if cursor is collinear with pt1-pt2
        if z_axis.IsTiny():
            e.Display.DrawLine(self.pt1, self.pt2, System.Drawing.Color.Red, 2)
            e.Display.DrawDottedLine(self.pt2, current_pt, System.Drawing.Color.Red)
            return

        z_axis.Unitize()
        y_axis = rg.Vector3d.CrossProduct(z_axis, self.x_axis)
        y_axis.Unitize()

        # Determine which side of the line the cursor is on for preview offset direction
        cursor_side = temp_vec * y_axis
        offset_dir = 1 if cursor_side >= 0 else -1
        offset_dist = self.preview_offset * offset_dir

        # Draw the pt1-pt2 line
        e.Display.DrawLine(self.pt1, self.pt2, System.Drawing.Color.White, 1)

        # Draw dotted line to cursor showing plane definition
        e.Display.DrawDottedLine(self.pt1, current_pt, System.Drawing.Color.Gray)

        # Draw dimension preview at default offset
        dim_pt1 = self.pt1 + y_axis * offset_dist
        dim_pt2 = self.pt2 + y_axis * offset_dist

        preview_color = System.Drawing.Color.FromArgb(180, 180, 180)

        draw_extension_lines(
            e.Display, self.pt1, self.pt2, dim_pt1, dim_pt2,
            y_axis, offset_dist, self.ext_offset, self.ext_extension, preview_color
        )
        e.Display.DrawLine(dim_pt1, dim_pt2, preview_color, 1)

        # Arrows
        draw_arrow(e.Display, dim_pt1, self.x_axis, self.arrow_size, z_axis, preview_color)
        draw_arrow(e.Display, dim_pt2, -self.x_axis, self.arrow_size, z_axis, preview_color)

        # Dimension text
        text_pt = (dim_pt1 + dim_pt2) / 2
        dim_text = format_dimension_text(self.distance, self.precision)
        e.Display.Draw2dText(dim_text, preview_color, text_pt, True, self.text_height * 12)

        # Draw small plane indicator rectangle
        plane_size = self.distance * 0.1
        corner1 = self.pt1 + y_axis * offset_dist * 0.3
        corner2 = self.pt1 + self.x_axis * plane_size + y_axis * offset_dist * 0.3
        corner3 = self.pt1 + self.x_axis * plane_size + y_axis * (offset_dist * 0.3 + plane_size * offset_dir)
        corner4 = self.pt1 + y_axis * (offset_dist * 0.3 + plane_size * offset_dir)

        e.Display.DrawDottedLine(corner1, corner2, System.Drawing.Color.DimGray)
        e.Display.DrawDottedLine(corner2, corner3, System.Drawing.Color.DimGray)
        e.Display.DrawDottedLine(corner3, corner4, System.Drawing.Color.DimGray)
        e.Display.DrawDottedLine(corner4, corner1, System.Drawing.Color.DimGray)


class DimensionGetPoint(Rhino.Input.Custom.GetPoint):
    """Custom GetPoint with dynamic dimension preview for final placement."""

    def __init__(self, pt1, pt2, dim_plane, dim_style):
        super().__init__()
        self.pt1 = pt1
        self.pt2 = pt2
        self.dim_plane = dim_plane
        self.dim_style = dim_style
        self.distance = pt1.DistanceTo(pt2)
        self.precision = dim_style.LengthResolution

        # Drawing parameters
        self.text_height = dim_style.TextHeight
        self.arrow_size = dim_style.ArrowLength
        self.ext_offset = dim_style.ExtensionLineOffset
        self.ext_extension = dim_style.ExtensionLineExtension

    def OnDynamicDraw(self, e):
        """Draw dimension preview as cursor moves."""

        current_pt = e.CurrentPoint
        cp = self.dim_plane.ClosestPoint(current_pt)

        offset_vec = cp - self.pt1
        offset_dist = offset_vec * self.dim_plane.YAxis

        dim_pt1 = self.pt1 + self.dim_plane.YAxis * offset_dist
        dim_pt2 = self.pt2 + self.dim_plane.YAxis * offset_dist

        draw_extension_lines(
            e.Display, self.pt1, self.pt2, dim_pt1, dim_pt2,
            self.dim_plane.YAxis, offset_dist,
            self.ext_offset, self.ext_extension, System.Drawing.Color.White
        )
        e.Display.DrawLine(dim_pt1, dim_pt2, System.Drawing.Color.White, 1)

        draw_arrow(e.Display, dim_pt1, self.dim_plane.XAxis, self.arrow_size, self.dim_plane.ZAxis, System.Drawing.Color.White)
        draw_arrow(e.Display, dim_pt2, -self.dim_plane.XAxis, self.arrow_size, self.dim_plane.ZAxis, System.Drawing.Color.White)

        text_pt = (dim_pt1 + dim_pt2) / 2
        dim_text = format_dimension_text(self.distance, self.precision)
        e.Display.Draw2dText(dim_text, System.Drawing.Color.White, text_pt, True, self.text_height * 12)


def Dim3D():
    """Create 3D linear dimensions between two points on a user-defined plane. Loops until cancelled."""

    dim_style = sc.doc.DimStyles.Current
    units = sc.doc.GetUnitSystemName(True, False, False, False)
    layer_index = ensure_annotations_layer()

    while True:
        # Step 1: Get first point
        rc, pt1 = Rhino.Input.RhinoGet.GetPoint("First point (Escape to exit)", False)
        if rc != Rhino.Commands.Result.Success:
            break

        # Step 2: Get second point with rubber-band line
        gp = Rhino.Input.Custom.GetPoint()
        gp.SetCommandPrompt("Second point")
        gp.SetBasePoint(pt1, True)
        gp.DrawLineFromPoint(pt1, True)
        gp.Get()
        if gp.CommandResult() != Rhino.Commands.Result.Success:
            break
        pt2 = gp.Point()

        # Validate: points must not be coincident
        dist = pt1.DistanceTo(pt2)
        tol = sc.doc.ModelAbsoluteTolerance
        if dist < tol:
            print("Error: Points are coincident")
            continue

        # Step 3: Get third point or reuse last plane
        has_last_plane = STICKY_PLANE_KEY in sc.sticky
        dim_plane = None

        gp2 = OrientationGetPoint(pt1, pt2, dim_style)
        gp2.SetCommandPrompt("Third point to define plane")
        gp2.SetBasePoint(pt1, True)

        if has_last_plane:
            gp2.AddOption("LastPlane")

        result = gp2.Get()

        if gp2.CommandResult() != Rhino.Commands.Result.Success:
            break

        if result == Rhino.Input.GetResult.Option:
            # User chose LastPlane -- rebuild plane through new pt1/pt2
            last_z = sc.sticky[STICKY_PLANE_KEY]
            x_axis = pt2 - pt1
            x_axis.Unitize()
            # Project stored z_axis to be perpendicular to new x_axis
            z_axis = last_z - (last_z * x_axis) * x_axis
            if z_axis.IsTiny():
                print("Error: Last plane normal is parallel to current line. Pick a third point.")
                continue
            z_axis.Unitize()
            y_axis = rg.Vector3d.CrossProduct(z_axis, x_axis)
            y_axis.Unitize()
            dim_plane = rg.Plane(pt1, x_axis, y_axis)
        else:
            # Build plane from three points
            pt3 = gp2.Point()
            x_axis = pt2 - pt1
            x_axis.Unitize()
            temp_vec = pt3 - pt1
            z_axis = rg.Vector3d.CrossProduct(x_axis, temp_vec)

            if z_axis.IsTiny():
                print("Error: Third point is collinear. Cannot define plane.")
                continue

            z_axis.Unitize()
            y_axis = rg.Vector3d.CrossProduct(z_axis, x_axis)
            y_axis.Unitize()
            dim_plane = rg.Plane(pt1, x_axis, y_axis)

            # Cache plane normal for reuse
            sc.sticky[STICKY_PLANE_KEY] = rg.Vector3d(z_axis)

        # Step 4: Get dimension line position with live preview
        gp3 = DimensionGetPoint(pt1, pt2, dim_plane, dim_style)
        gp3.SetCommandPrompt("Dimension line location")
        gp3.Constrain(dim_plane, False)
        gp3.SetBasePoint(pt1, False)
        gp3.Get()
        if gp3.CommandResult() != Rhino.Commands.Result.Success:
            break
        dim_line_pt = gp3.Point()

        # Convert world points to plane coordinates (2D)
        rc1, u1, v1 = dim_plane.ClosestParameter(pt1)
        rc2, u2, v2 = dim_plane.ClosestParameter(pt2)
        rc3, u3, v3 = dim_plane.ClosestParameter(dim_line_pt)

        if not (rc1 and rc2 and rc3):
            print("Error: Failed to project points to dimension plane")
            continue

        # Create the linear dimension
        dim = rg.LinearDimension(
            dim_plane,
            rg.Point2d(u1, v1),
            rg.Point2d(u2, v2),
            rg.Point2d(u3, v3)
        )

        if dim is None or not dim.IsValid:
            print("Error: Failed to create dimension geometry")
            continue

        dim.DimensionStyleId = dim_style.Id

        # Add dimension to annotations layer
        attr = Rhino.DocObjects.ObjectAttributes()
        attr.LayerIndex = layer_index
        guid = sc.doc.Objects.AddLinearDimension(dim, attr)
        if guid == System.Guid.Empty:
            print("Error: Failed to add dimension to document")
            continue

        sc.doc.Views.Redraw()
        print(f"Dimension: {dist:.4f} {units}")


if __name__ == "__main__":
    Dim3D()
