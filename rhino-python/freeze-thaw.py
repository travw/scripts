"""
THV_FreezeXYZ
-------------
Position-lock selected objects via an explicit Freeze / Thaw prompt.

Frozen objects:
  - Can be clicked / selected normally
  - Can have properties edited (layer, color, name, user text, etc.)
  - Cannot be moved, rotated, scaled, or otherwise transformed
    (any geometry change is reverted automatically on the next idle tick)

Workflow per run:
  1. Mode prompt: choose Freeze or Thaw (default Freeze, click option or
     type F / T then Enter). Esc just (re-)arms session handlers and exits.
  2. Object prompt: respects preselection. Acts only in the chosen mode,
     so a mixed selection in Thaw mode never accidentally freezes anything.

Behavior is enforced by event handlers that live for the current Rhino
session. After reopening a file, run the script once (Esc through both
prompts) to re-arm.

Compatible: Rhino 7+, IronPython 2.7
"""

import Rhino
import scriptcontext as sc
import rhinoscriptsyntax as rs


# ---- Constants -----------------------------------------------------------

KEY_FREEZE = "THV.Freeze"
VAL_FREEZE = "1"

MODE_FREEZE = "Freeze"
MODE_THAW   = "Thaw"

STICKY_HANDLERS  = "thv_freeze_handlers_v1"
STICKY_PENDING   = "thv_freeze_pending_v1"
STICKY_REVERTING = "thv_freeze_reverting_v1"


# ---- Event handlers ------------------------------------------------------

def _on_replace_object(sender, e):
    """Capture pre-edit geometry of any frozen object touched by an edit."""
    if sc.sticky.get(STICKY_REVERTING, False):
        return
    try:
        new_obj = e.NewRhinoObject
        old_obj = e.OldRhinoObject
        if new_obj is None or old_obj is None:
            return
        if new_obj.Attributes.GetUserString(KEY_FREEZE) != VAL_FREEZE:
            return
        pending = sc.sticky.get(STICKY_PENDING, {})
        # Keep only the FIRST snapshot per object so we revert to the
        # original state even across drags that fire several events.
        if new_obj.Id not in pending:
            pending[new_obj.Id] = old_obj.Geometry.Duplicate()
            sc.sticky[STICKY_PENDING] = pending
    except Exception:
        pass


def _on_idle(sender, e):
    """Revert any captured frozen objects after the edit completes."""
    pending = sc.sticky.get(STICKY_PENDING, None)
    if not pending:
        return
    # Clear the queue first so re-entry during Replace is harmless.
    sc.sticky[STICKY_PENDING] = {}
    doc = Rhino.RhinoDoc.ActiveDoc
    if doc is None:
        return
    sc.sticky[STICKY_REVERTING] = True
    try:
        for obj_id, original_geom in pending.items():
            if doc.Objects.FindId(obj_id) is None:
                continue
            doc.Objects.Replace(obj_id, original_geom)
        doc.Views.Redraw()
    finally:
        sc.sticky[STICKY_REVERTING] = False


def _arm_handlers():
    """Register event handlers once per Rhino session."""
    if sc.sticky.get(STICKY_HANDLERS, False):
        return
    Rhino.RhinoDoc.ReplaceRhinoObject += _on_replace_object
    Rhino.RhinoApp.Idle += _on_idle
    sc.sticky[STICKY_HANDLERS] = True


# ---- Apply ---------------------------------------------------------------

def _apply(ids, mode):
    """Set or clear the freeze flag based on mode. Idempotent per object."""
    applied = 0
    already = 0
    rs.EnableRedraw(False)
    try:
        for obj_id in ids:
            current = rs.GetUserText(obj_id, KEY_FREEZE)
            is_frozen = (current == VAL_FREEZE)
            if mode == MODE_FREEZE:
                if is_frozen:
                    already += 1
                else:
                    rs.SetUserText(obj_id, KEY_FREEZE, VAL_FREEZE)
                    applied += 1
            else:  # MODE_THAW
                if is_frozen:
                    rs.SetUserText(obj_id, KEY_FREEZE, None)
                    applied += 1
                else:
                    already += 1
    finally:
        rs.EnableRedraw(True)
    return applied, already


# ---- Main ----------------------------------------------------------------

def main():
    # Always arm the session handlers (idempotent).
    _arm_handlers()

    # Mode prompt -- explicit choice, no accidental toggling.
    mode = rs.GetString("Mode", MODE_FREEZE, [MODE_FREEZE, MODE_THAW])
    if mode is None:
        print("THV_FreezeXYZ: handlers armed for this session.")
        return
    if mode not in (MODE_FREEZE, MODE_THAW):
        return

    prompt = "Select objects to " + mode.lower()
    ids = rs.GetObjects(prompt, preselect=True, select=False)
    if not ids:
        return

    applied, already = _apply(ids, mode)

    verb = "Frozen" if mode == MODE_FREEZE else "Thawed"
    state = "frozen" if mode == MODE_FREEZE else "thawed"
    msg = "THV_FreezeXYZ: " + verb + ": " + str(applied)
    if already:
        msg += " (skipped " + str(already) + " already " + state + ")"
    print(msg)


if __name__ == "__main__":
    main()