# freeze-thaw: current state + plugin proposal

doc covers (1) what the freeze-thaw script does, (2) where it falls short and why, (3) what a c# rhinocommon plugin would unlock, (4) cost + recommendation.

## what it does

position-lock rhino objects. frozen objects:

- remain selectable via single click
- can have attributes edited (layer, color, name, usertext)
- cannot be moved, rotated, scaled, or otherwise have their geometry modified

state is marked by a `THV.Freeze=1` user text key on the object's attributes. survives save/load in the `.3dm`.

## current implementation: python script

`rhino-python/freeze-thaw.py`. ~150 lines. compatible with rhino 7+, ironpython 2.

architecture: REACT-AND-REVERT.

- on freeze: tag selected objects with `THV.Freeze=1`
- on first run per rhino session: register two event handlers (idempotent, stored via `sc.sticky`)
  - `RhinoDoc.ReplaceRhinoObject`: when ANY object is replaced, if it carries the freeze flag, snapshot its pre-edit geometry into a pending dict
  - `RhinoApp.Idle`: drain the pending dict, calling `doc.Objects.Replace` to revert each frozen object back to its snapshot
- on thaw: clear the user text flag

session-scoped state lives in `sc.sticky`. file-persistent state is just the user text flag.

## what works

- move / rotate / scale / control-point edit: caught by `ReplaceRhinoObject`, reverted on idle
- attribute edits: pass through (intended)
- single-click selection: works (no `_Lock` is involved, so picking is unaffected)
- file persistence of frozen state: usertext survives, but handlers do not (have to rerun the script to rearm)

## what is broken

1. **delete is not blocked.** `_Delete` (and any flow that deletes a frozen object as part of a larger operation) destroys it with no recovery. `ReplaceRhinoObject` does not fire on deletion. needs a `DeleteRhinoObject` hook plus re-add on idle.
2. **boolean ops consume frozen operands.** `_BooleanUnion` and similar delete operands and add a result. since delete is not hooked, the frozen operand is gone. result solid stays. same root cause as #1.
3. **handlers do not auto-rearm.** opening a file with frozen objects produces a window where the objects carry the flag but the handlers are not running. user must rerun the script (esc through both prompts) to rearm. easy to forget. silent failure.
4. **visible flicker.** user sees the modification briefly before idle reverts. cosmetic but obvious.
5. **undo pollution.** each idle revert is its own undo record. ctrl-z after an attempted move does not produce predictable behavior.
6. **silent error swallowing.** bare `except Exception: pass` in the replace handler hides real bugs.
7. **block-instance bypass.** a frozen object inside a block instance moves with the instance. the underlying definition geometry is not replaced, so no event fires.

## why python cannot do better

rhino exposes no native primitive that combines "selectable" with "non-modifiable":

- `_Lock` blocks both selection and modification
- `_Hide` makes the object invisible
- layer-level lock blocks selection
- groups do not enforce position
- block instances remain freely transformable

so any "selectable + position-locked" mechanism must be implemented externally by intercepting modification events. the python script api exposes only POST-event handlers, i.e. notifications. pre-modification cancellation is not available from a script. this is the architectural ceiling.

beyond that, the script has no clean way to:

- auto-arm at rhino startup
- own a real rhino command (with command-line tab completion, options, undo records)
- subscribe handlers via a managed plugin lifecycle
- draw a display overlay indicating frozen state

## c# rhinocommon plugin: what changes

a compiled plugin (`.rhp`) running on the same rhino + rhinocommon api unlocks:

1. **`RhinoDoc.BeforeTransformObjects` event with cancellation.** fires BEFORE a transform command modifies geometry. handler sets `e.Cancel = true` if any affected object carries the freeze flag, killing the operation pre-modification. no flicker, no revert, no undo record polluted. open question: this event's cancellation behavior in current rhino needs a quick verification test (10 min in a throwaway plugin). fallback paths covered below if cancellation is not supported.

2. **automatic load at rhino startup.** rhino loads installed plugins on launch, so handlers are always live for every doc opened in that session. eliminates the per-session rearm and the file-open footgun.

3. **proper handler lifecycle.** managed by rhino's plugin host. survives file open / close / doc switch with no sticky state. no `_arm_handlers` needed.

4. **complete event coverage in one place.** plugin hooks `BeforeTransformObjects`, `DeleteRhinoObject`, optionally `BeginCommand` / `EndCommand`, and `ReplaceRhinoObject` together. python can not elegantly hook `BeforeTransformObjects` and cannot guarantee handler stability across doc loads.

5. **real rhino commands with autocomplete.** `_THV_Freeze`, `_THV_Thaw`, `_THV_SelFrozen` as named commands with command-line tab completion, options, undo records. `_-RunPythonScript "..."` does not get this.

6. **plugin user data per object** if internal-only state is ever needed. for the freeze flag itself, keep usertext -- backwards compatible with files already touched by the script.

7. **display conduit for visual indication.** small lock glyph, tint, or outline on frozen objects without modifying geometry or layers. optional polish.

## design sketch

```
THV.FreezePlugin (.rhp)
+-- OnLoad
|     subscribe BeforeTransformObjects  -> if any e.Objects has THV.Freeze=1, e.Cancel=true
|     subscribe DeleteRhinoObject       -> capture geom + attrs, queue re-add
|     subscribe Idle                    -> drain re-add queue inside BeginUndoRecord
+-- _THV_Freeze command
|     get selection, set THV.Freeze=1 usertext
+-- _THV_Thaw command
|     get selection, clear THV.Freeze usertext
+-- _THV_SelFrozen command (optional)
      filter doc by usertext, select all matches for batch property edit
```

freeze flag remains `THV.Freeze=1` usertext. existing files touched by the python script begin being enforced the moment the plugin loads. no migration step.

## fallback if `BeforeTransformObjects` does not cancel

(uncertainty noted above. ranked best to worst.)

- **A.** in `BeforeTransformObjects`, deselect frozen objects from the affected set before the command processes. rhino transforms whatever is left. user gets predictable behavior on mixed selections.
- **B.** hook `BeginCommand`, identify transform-class commands by guid, deselect frozen objects before the command reads selection.
- **C.** same revert-on-idle pattern the script uses, but implemented in c# with proper structure. still strictly better than the script because of auto-load + `DeleteRhinoObject` hook.

even option C beats the current script meaningfully.

## bug coverage comparison

| issue                              | script | plugin |
|------------------------------------|--------|--------|
| delete not blocked                 | broken | fixed  |
| boolean ops consume frozen objects | broken | fixed  |
| auto-rearm on file open            | broken | fixed  |
| visible flicker on attempted edit  | broken | fixed (with cancellation) |
| undo history pollution             | broken | fixed  |
| silent error swallowing            | broken | fixed (proper logging) |
| block-instance bypass              | broken | partial (needs nested traversal) |
| visual indicator of frozen state   | none   | possible via display conduit |

## cost

- one-time: ~150-300 lines of c#, half a day to write + test
- tooling: visual studio (community is free) + rhinocommon nuget (free)
- distribution: drop the `.rhp` into rhino plugins folder. no code signing required for internal raider use. yak (rhino package manager) is an option later if it ever needs wider distribution
- maintenance: rhinocommon api is stable across rhino versions. expected ongoing work near zero

## recommendation

ship the plugin. the event-driven script is a dead end:

- every newly-discovered modification path needs another event hook
- even with full event coverage, flicker and undo pollution remain
- session-rearm and file-open semantics will keep biting

plugin is a single focused project with finite scope. once written and installed, the problem is solved at the right architectural layer and stays solved.

## next steps

1. spin up a minimal c# rhinocommon plugin scaffold
2. run a 10-minute test to confirm whether `BeforeTransformObjects` cancellation is supported in current rhino (rhino 8)
3. implement against whichever pattern works (preferred: cancellation; fallback: deselect-before-command; last resort: revert-on-idle)
4. test against the bug list above with real geometry
5. install on dev machine, then the shop
