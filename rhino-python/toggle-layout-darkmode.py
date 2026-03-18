"""
ToggleLayoutDarkMode.py
Toggles layout background between current color and black,
Annotations layer between current color and cyan,
and any black layers to white.

Uses Rhino's sticky dictionary to remember original colors
so they can be restored on the next toggle.
"""

import Rhino
import scriptcontext as sc
import System.Drawing as sd

def toggle_layout_dark_mode():
    # Dark mode target colors
    DARK_BG = sd.Color.Black
    DARK_ANNOTATION = sd.Color.Cyan
    
    # Sticky keys for state persistence
    STATE_KEY = "layout_dark_mode_active"
    ORIG_BG_KEY = "layout_dark_mode_orig_bg"
    ORIG_ANN_KEY = "layout_dark_mode_orig_annotation"
    BLACK_LAYERS_KEY = "layout_dark_mode_black_layers"
    
    # Find the Annotations layer (case-insensitive)
    annotations_layer = None
    for layer in sc.doc.Layers:
        if layer.Name and layer.Name.lower() == "06 - annotations":
            annotations_layer = layer
            break
    
    if annotations_layer is None:
        print("Error: 'Annotations' layer not found")
        return False
    
    is_dark = sc.sticky.get(STATE_KEY, False)
    
    if is_dark:
        # Restore original colors
        orig_bg = sc.sticky.get(ORIG_BG_KEY)
        orig_ann = sc.sticky.get(ORIG_ANN_KEY)
        black_layer_ids = sc.sticky.get(BLACK_LAYERS_KEY, [])
        
        if orig_bg:
            Rhino.ApplicationSettings.AppearanceSettings.PageviewPaperColor = orig_bg
        if orig_ann:
            annotations_layer.Color = orig_ann
        
        # Restore white layers back to black
        restored_count = 0
        for layer_id in black_layer_ids:
            layer = sc.doc.Layers.FindId(layer_id)
            if layer:
                layer.Color = sd.Color.Black
                restored_count += 1
        
        sc.sticky[STATE_KEY] = False
        print("Dark mode OFF")
        print("  Background: restored")
        print("  Annotations: restored")
        if restored_count > 0:
            print("  Layers restored to black: {}".format(restored_count))
    else:
        # Store current colors
        sc.sticky[ORIG_BG_KEY] = Rhino.ApplicationSettings.AppearanceSettings.PageviewPaperColor
        sc.sticky[ORIG_ANN_KEY] = annotations_layer.Color
        
        # Find and convert black layers to white
        black_layer_ids = []
        for layer in sc.doc.Layers:
            if not layer.Name:
                continue
            # Check for black (R=0, G=0, B=0)
            if layer.Color.R == 0 and layer.Color.G == 0 and layer.Color.B == 0:
                black_layer_ids.append(layer.Id)
                layer.Color = sd.Color.White
        
        sc.sticky[BLACK_LAYERS_KEY] = black_layer_ids
        
        # Apply dark mode
        Rhino.ApplicationSettings.AppearanceSettings.PageviewPaperColor = DARK_BG
        annotations_layer.Color = DARK_ANNOTATION
        
        sc.sticky[STATE_KEY] = True
        print("Dark mode ON")
        print("  Background: black")
        print("  Annotations: cyan")
        if black_layer_ids:
            print("  Layers switched to white: {}".format(len(black_layer_ids)))
    
    sc.doc.Views.Redraw()
    return True

if __name__ == "__main__":
    toggle_layout_dark_mode()
