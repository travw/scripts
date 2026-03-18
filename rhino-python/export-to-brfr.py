"""
ExportToBrfr.py
Rhino Python script to export named views as a .brfr file

INSTALLATION:
1. Save this file to a location on your computer
2. In Rhino, run: RunPythonScript
3. Browse to this file and run it
   - Or add to your scripts folder and create an alias

USAGE:
1. Set up Named Views in Rhino for each "slide" you want
2. Optionally add text dots or annotations that will be captured
3. Run the script
4. Select which views to include
5. Add captions for each slide
6. Choose output location
7. Open the .brfr file in any browser at brfr.app

TIPS:
- Name your views descriptively: "01_Overview", "02_Base_Assembly", etc.
- Views are sorted alphabetically, so use number prefixes for order
- Add dimensions and annotations in Rhino - they'll be captured in the render
- Use a white or light background for fabrication drawings
- Set your viewport to the size you want (taller = more portrait, better for mobile)

Author: Brfr Project
License: MIT
"""

import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
import System
import os
import json
import zipfile
import tempfile
import datetime
from System.Drawing import Bitmap, Imaging, Size, Color

def get_named_views():
    """Get all named views in the document, sorted alphabetically."""
    views = []
    named_views = sc.doc.NamedViews
    for i in range(named_views.Count):
        view = named_views[i]
        views.append({
            'index': i,
            'name': view.Name,
            'viewport': view.Viewport
        })
    return sorted(views, key=lambda x: x['name'].lower())

def capture_view(view_info, width=1080, height=1920, transparent=False):
    """
    Capture a named view as a bitmap.
    Default size is 1080x1920 (9:16 portrait, ideal for mobile).
    """
    # Get the active view
    active_view = sc.doc.Views.ActiveView
    if not active_view:
        return None
    
    # Store current viewport settings
    original_vp = active_view.ActiveViewport.IsValidCamera
    
    # Restore the named view
    sc.doc.NamedViews.Restore(view_info['index'], active_view.ActiveViewport)
    Rhino.RhinoDoc.ActiveDoc.Views.Redraw()
    
    # Set up capture settings
    settings = Rhino.Display.ViewCaptureSettings(active_view, Size(width, height), 72)
    settings.DrawGrid = False
    settings.DrawAxes = False
    settings.DrawWorldAxes = False
    settings.TransparentBackground = transparent
    
    # Capture the view
    bitmap = Rhino.Display.ViewCapture.Capture(settings)
    
    return bitmap

def bitmap_to_bytes(bitmap, format="png"):
    """Convert a System.Drawing.Bitmap to bytes."""
    stream = System.IO.MemoryStream()
    if format.lower() == "png":
        bitmap.Save(stream, Imaging.ImageFormat.Png)
    else:
        bitmap.Save(stream, Imaging.ImageFormat.Jpeg)
    return stream.ToArray()

def get_document_metadata():
    """Extract useful metadata from the Rhino document."""
    doc = Rhino.RhinoDoc.ActiveDoc
    return {
        'filename': os.path.basename(doc.Path) if doc.Path else "Untitled",
        'units': str(doc.ModelUnitSystem),
        'created': datetime.datetime.now().isoformat()
    }

def create_brfr(views_data, output_path, title, author=""):
    """
    Create a .brfr file from captured views.
    
    views_data: list of dicts with 'name', 'caption', 'bitmap' keys
    output_path: where to save the .brfr file
    title: brief title
    author: optional author name
    """
    
    # Create manifest
    manifest = {
        "version": "0.1.0",
        "title": title,
        "author": author,
        "created": datetime.datetime.now().isoformat(),
        "generator": "Rhino ExportToBrfr",
        "metadata": get_document_metadata(),
        "theme": {
            "background": "#0a0a0f",
            "foreground": "#f0f0f5",
            "accent": "#6366f1"
        },
        "slides": []
    }
    
    # Create a temporary directory to build the zip
    temp_dir = tempfile.mkdtemp()
    media_dir = os.path.join(temp_dir, "media")
    os.makedirs(media_dir)
    
    try:
        # Process each view
        for i, view in enumerate(views_data):
            slide_num = str(i + 1).zfill(3)
            filename = "slide-{}.png".format(slide_num)
            filepath = os.path.join(media_dir, filename)
            
            # Save the bitmap
            if view['bitmap']:
                view['bitmap'].Save(filepath, Imaging.ImageFormat.Png)
            
            # Create slide entry
            slide = {
                "id": "slide-{}".format(slide_num),
                "type": "image",
                "media": "media/{}".format(filename),
                "fit": "contain"  # Use contain for technical drawings
            }
            
            # Add caption if provided
            if view.get('caption'):
                slide["text"] = {
                    "content": view['caption'],
                    "position": "bottom",
                    "style": "caption"
                }
            
            manifest["slides"].append(slide)
        
        # Write manifest
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        
        # Create the zip file with .brfr extension
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(manifest_path, "manifest.json")
            for filename in os.listdir(media_dir):
                filepath = os.path.join(media_dir, filename)
                zf.write(filepath, "media/{}".format(filename))
        
        return True
        
    except Exception as e:
        print("Error creating brfr: {}".format(str(e)))
        return False
        
    finally:
        # Clean up temp directory
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

def main():
    """Main function - runs the ExportToBrfr command."""
    
    # Check if we have named views
    views = get_named_views()
    if not views:
        rs.MessageBox(
            "No Named Views found.\n\n" +
            "Create Named Views first:\n" +
            "1. Set up your viewport\n" +
            "2. Run: NamedView > Save\n" +
            "3. Repeat for each slide you want",
            title="ExportToBrfr"
        )
        return
    
    # Let user select which views to include
    view_names = [v['name'] for v in views]
    selected = rs.MultiListBox(
        view_names,
        message="Select views to include in the brief:\n(Views will be ordered alphabetically)",
        title="ExportToBrfr - Select Views",
        defaults=view_names  # Select all by default
    )
    
    if not selected:
        print("Export cancelled.")
        return
    
    # Filter to selected views only
    selected_views = [v for v in views if v['name'] in selected]
    
    # Get brief title
    doc_name = os.path.splitext(os.path.basename(sc.doc.Path or "Untitled"))[0]
    title = rs.StringBox(
        message="Brief title:",
        default_value=doc_name,
        title="ExportToBrfr - Title"
    )
    
    if not title:
        print("Export cancelled.")
        return
    
    # Ask for captions
    add_captions = rs.MessageBox(
        "Add captions to each slide?\n\n" +
        "You can use the view name as a starting point\n" +
        "and edit it for each slide.",
        title="ExportToBrfr - Captions",
        buttons=4  # Yes/No
    )
    
    # Capture views and optionally get captions
    views_data = []
    
    print("\nCapturing {} views...".format(len(selected_views)))
    
    for i, view in enumerate(selected_views):
        print("  [{}/{}] Capturing: {}".format(i+1, len(selected_views), view['name']))
        
        # Capture the view
        bitmap = capture_view(view)
        
        if not bitmap:
            print("    Warning: Failed to capture view")
            continue
        
        # Get caption if requested
        caption = ""
        if add_captions == 6:  # Yes
            # Clean up view name for default caption
            default_caption = view['name']
            # Remove number prefixes like "01_" or "1. "
            import re
            default_caption = re.sub(r'^[\d]+[_\.\-\s]*', '', default_caption)
            # Replace underscores with spaces
            default_caption = default_caption.replace('_', ' ')
            
            caption = rs.StringBox(
                message="Caption for slide {} of {}:\n\nView: {}".format(
                    i+1, len(selected_views), view['name']
                ),
                default_value=default_caption,
                title="ExportToBrfr - Caption"
            )
            
            if caption is None:  # User cancelled
                print("Export cancelled.")
                return
        
        views_data.append({
            'name': view['name'],
            'caption': caption or "",
            'bitmap': bitmap
        })
    
    if not views_data:
        rs.MessageBox("No views were captured.", title="ExportToBrfr")
        return
    
    # Get output path
    default_filename = "{}.brfr".format(title.replace(' ', '-').lower())
    output_path = rs.SaveFileName(
        title="Save Brief As",
        filter="Brfr files (*.brfr)|*.brfr||",
        filename=default_filename
    )
    
    if not output_path:
        print("Export cancelled.")
        return
    
    # Ensure .brfr extension
    if not output_path.lower().endswith('.brfr'):
        output_path += '.brfr'
    
    # Create the brfr file
    print("\nCreating brief...")
    success = create_brfr(views_data, output_path, title)
    
    if success:
        print("\nSuccess! Brief saved to:")
        print("  {}".format(output_path))
        print("\nOpen in browser at brfr.app or share directly.")
        
        # Offer to open the output folder
        open_folder = rs.MessageBox(
            "Brief exported successfully!\n\n" +
            output_path + "\n\n" +
            "Open containing folder?",
            title="ExportToBrfr - Complete",
            buttons=4  # Yes/No
        )
        
        if open_folder == 6:  # Yes
            folder = os.path.dirname(output_path)
            os.startfile(folder)
    else:
        rs.MessageBox(
            "Failed to create brief.\nCheck the command line for details.",
            title="ExportToBrfr - Error"
        )

# Run the script
if __name__ == "__main__" or True:  # Always run when loaded
    main()
