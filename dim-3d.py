#! python3
"""
Dim3D.py - Create a 3D linear dimension annotation between any two points
on a user-defined plane.

Workflow:
1. Select first point (start of measurement)
2. Select second point (end of measurement)
3. Select third point to define the dimension plane orientation (with preview)
4. Pick dimension line location with live preview (constrained to the defined plane)

For Rhino 8, vanilla Python scripting.
Version: 1.3 - Added orientation plane preview
"""

import Rhino
import Rhino.Geometry as rg
import Rhino.Display as rd
import scriptcontext as sc
import System


class OrientationGetPoint(Rhino.Input.Custom.GetPoint):
    """Custom GetPoint with dynamic plane/dimension preview for orientation selection."""
    
    def __init__(self, pt1, pt2, dim_style):
        super().__init__()
        self.pt1 = pt1
        self.pt2 = pt2
        self.dim_style = dim_style
        self.distance = pt1.DistanceTo(pt2)
        self.midpoint = (pt1 + pt2) / 2
        
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
            # Just draw a line indicating we need a non-collinear point
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
        
        # Extension lines
        if offset_dir >= 0:
            ext1_start = self.pt1 + y_axis * self.ext_offset
            ext1_end = dim_pt1 + y_axis * self.ext_extension
            ext2_start = self.pt2 + y_axis * self.ext_offset
            ext2_end = dim_pt2 + y_axis * self.ext_extension
        else:
            ext1_start = self.pt1 - y_axis * self.ext_offset
            ext1_end = dim_pt1 - y_axis * self.ext_extension
            ext2_start = self.pt2 - y_axis * self.ext_offset
            ext2_end = dim_pt2 - y_axis * self.ext_extension
        
        # Draw with slightly dimmed color to indicate it's a preview
        preview_color = System.Drawing.Color.FromArgb(180, 180, 180)
        
        e.Display.DrawLine(ext1_start, ext1_end, preview_color, 1)
        e.Display.DrawLine(ext2_start, ext2_end, preview_color, 1)
        e.Display.DrawLine(dim_pt1, dim_pt2, preview_color, 1)
        
        # Arrows
        self.draw_arrow(e.Display, dim_pt1, self.x_axis, self.arrow_size, z_axis, preview_color)
        self.draw_arrow(e.Display, dim_pt2, -self.x_axis, self.arrow_size, z_axis, preview_color)
        
        # Dimension text
        text_pt = (dim_pt1 + dim_pt2) / 2
        dim_text = self.format_dimension_text(self.distance)
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
    
    def draw_arrow(self, display, tip, direction, size, z_axis, color):
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
    
    def format_dimension_text(self, distance):
        """Format the dimension value according to document settings."""
        precision = self.dim_style.LengthResolution
        if precision == 0:
            return f"{distance:.0f}"
        elif precision == 1:
            return f"{distance:.1f}"
        elif precision == 2:
            return f"{distance:.2f}"
        elif precision == 3:
            return f"{distance:.3f}"
        elif precision == 4:
            return f"{distance:.4f}"
        else:
            return f"{distance:.2f}"


class DimensionGetPoint(Rhino.Input.Custom.GetPoint):
    """Custom GetPoint with dynamic dimension preview for final placement."""
    
    def __init__(self, pt1, pt2, dim_plane, dim_style):
        super().__init__()
        self.pt1 = pt1
        self.pt2 = pt2
        self.dim_plane = dim_plane
        self.dim_style = dim_style
        self.distance = pt1.DistanceTo(pt2)
        
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
        
        if offset_dist >= 0:
            ext1_start = self.pt1 + self.dim_plane.YAxis * self.ext_offset
            ext1_end = dim_pt1 + self.dim_plane.YAxis * self.ext_extension
            ext2_start = self.pt2 + self.dim_plane.YAxis * self.ext_offset
            ext2_end = dim_pt2 + self.dim_plane.YAxis * self.ext_extension
        else:
            ext1_start = self.pt1 - self.dim_plane.YAxis * self.ext_offset
            ext1_end = dim_pt1 - self.dim_plane.YAxis * self.ext_extension
            ext2_start = self.pt2 - self.dim_plane.YAxis * self.ext_offset
            ext2_end = dim_pt2 - self.dim_plane.YAxis * self.ext_extension
        
        e.Display.DrawLine(ext1_start, ext1_end, System.Drawing.Color.White, 1)
        e.Display.DrawLine(ext2_start, ext2_end, System.Drawing.Color.White, 1)
        e.Display.DrawLine(dim_pt1, dim_pt2, System.Drawing.Color.White, 1)
        
        self.draw_arrow(e.Display, dim_pt1, self.dim_plane.XAxis, self.arrow_size)
        self.draw_arrow(e.Display, dim_pt2, -self.dim_plane.XAxis, self.arrow_size)
        
        text_pt = (dim_pt1 + dim_pt2) / 2
        dim_text = self.format_dimension_text(self.distance)
        e.Display.Draw2dText(dim_text, System.Drawing.Color.White, text_pt, True, self.text_height * 12)
    
    def draw_arrow(self, display, tip, direction, size):
        """Draw an arrowhead at the given point."""
        direction.Unitize()
        perp = rg.Vector3d.CrossProduct(direction, self.dim_plane.ZAxis)
        perp.Unitize()
        
        wing1 = -direction * size * 0.9 + perp * size * 0.3
        wing2 = -direction * size * 0.9 - perp * size * 0.3
        
        p1 = tip + wing1
        p2 = tip + wing2
        
        display.DrawLine(tip, p1, System.Drawing.Color.White, 1)
        display.DrawLine(tip, p2, System.Drawing.Color.White, 1)
        display.DrawLine(p1, p2, System.Drawing.Color.White, 1)
    
    def format_dimension_text(self, distance):
        """Format the dimension value according to document settings."""
        precision = self.dim_style.LengthResolution
        if precision == 0:
            return f"{distance:.0f}"
        elif precision == 1:
            return f"{distance:.1f}"
        elif precision == 2:
            return f"{distance:.2f}"
        elif precision == 3:
            return f"{distance:.3f}"
        elif precision == 4:
            return f"{distance:.4f}"
        else:
            return f"{distance:.2f}"


def Dim3D():
    """Create a 3D linear dimension between two points on a user-defined plane."""
    
    # Step 1: Get first point
    rc, pt1 = Rhino.Input.RhinoGet.GetPoint("Select first point", False)
    if rc != Rhino.Commands.Result.Success:
        return Rhino.Commands.Result.Cancel
    
    # Step 2: Get second point with rubber-band line
    gp = Rhino.Input.Custom.GetPoint()
    gp.SetCommandPrompt("Select second point")
    gp.SetBasePoint(pt1, True)
    gp.DrawLineFromPoint(pt1, True)
    gp.Get()
    if gp.CommandResult() != Rhino.Commands.Result.Success:
        return Rhino.Commands.Result.Cancel
    pt2 = gp.Point()
    
    # Validate: points must not be coincident
    dist = pt1.DistanceTo(pt2)
    tol = sc.doc.ModelAbsoluteTolerance
    if dist < tol:
        print("Error: First and second points are coincident")
        return Rhino.Commands.Result.Failure
    
    # Get dimension style early for previews
    dim_style = sc.doc.DimStyles.Current
    
    # Step 3: Get third point with orientation preview
    gp2 = OrientationGetPoint(pt1, pt2, dim_style)
    gp2.SetCommandPrompt("Select third point to define dimension plane")
    gp2.SetBasePoint(pt1, True)
    gp2.Get()
    if gp2.CommandResult() != Rhino.Commands.Result.Success:
        return Rhino.Commands.Result.Cancel
    pt3 = gp2.Point()
    
    # Construct plane from three points
    x_axis = pt2 - pt1
    x_axis.Unitize()
    
    temp_vec = pt3 - pt1
    z_axis = rg.Vector3d.CrossProduct(x_axis, temp_vec)
    
    if z_axis.IsTiny():
        print("Error: Third point is collinear with first two points. Cannot define plane.")
        return Rhino.Commands.Result.Failure
    
    z_axis.Unitize()
    y_axis = rg.Vector3d.CrossProduct(z_axis, x_axis)
    y_axis.Unitize()
    
    dim_plane = rg.Plane(pt1, x_axis, y_axis)
    
    # Step 4: Get dimension line position with live preview
    gp3 = DimensionGetPoint(pt1, pt2, dim_plane, dim_style)
    gp3.SetCommandPrompt("Pick dimension line location")
    gp3.Constrain(dim_plane, False)
    gp3.SetBasePoint(pt1, False)
    gp3.Get()
    if gp3.CommandResult() != Rhino.Commands.Result.Success:
        return Rhino.Commands.Result.Cancel
    dim_line_pt = gp3.Point()
    
    # Convert world points to plane coordinates (2D)
    rc1, u1, v1 = dim_plane.ClosestParameter(pt1)
    rc2, u2, v2 = dim_plane.ClosestParameter(pt2)
    rc3, u3, v3 = dim_plane.ClosestParameter(dim_line_pt)
    
    if not (rc1 and rc2 and rc3):
        print("Error: Failed to project points to dimension plane")
        return Rhino.Commands.Result.Failure
    
    # Create 2D points in plane coordinates
    ext1_2d = rg.Point2d(u1, v1)
    ext2_2d = rg.Point2d(u2, v2)
    dimline_2d = rg.Point2d(u3, v3)
    
    # Create the linear dimension
    dim = rg.LinearDimension(
        dim_plane,
        ext1_2d,
        ext2_2d,
        dimline_2d
    )
    
    if dim is None or not dim.IsValid:
        print("Error: Failed to create dimension geometry")
        return Rhino.Commands.Result.Failure
    
    # Apply dimension style
    dim.DimensionStyleId = dim_style.Id
    
    # Add dimension to document
    guid = sc.doc.Objects.AddLinearDimension(dim)
    if guid == System.Guid.Empty:
        print("Error: Failed to add dimension to document")
        return Rhino.Commands.Result.Failure
    
    sc.doc.Views.Redraw()
    
    # Report success
    units = sc.doc.GetUnitSystemName(True, False, False, False)
    print(f"Dimension created: {dist:.4f} {units}")
    
    return Rhino.Commands.Result.Success


if __name__ == "__main__":
    Dim3D()
