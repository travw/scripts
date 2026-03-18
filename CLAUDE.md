# CLAUDE.md -- raider-scripts

scripts repo for rhino 8 / grasshopper automation at raider boats.
aluminum boat design, fabrication, and shop workflow tooling.

## what this repo is

standalone rhino python scripts and grasshopper C# script components
used in parametric aluminum boat hull design and CNC fabrication.
scripts are run via `_-RunPythonScript`, rhino aliases, or embedded
in grasshopper C# script components.

MIT licensed. no third-party grasshopper plugin dependencies.

## repo structure

```
/rhino-python/      standalone rhino python scripts (.py)
/grasshopper-cs/    grasshopper C# script components (.cs)
/CLAUDE.md          you are here
/LICENSE            MIT
```

## environment

- rhino 8, windows
- grasshopper: vanilla only (no food4rhino plugins in scripts)
- python: rhino's IronPython 2 (legacy) or CPython 3 (`#! python3` shebang)
  - prefer python 3 for new scripts unless IronPython is required
  - python 3 scripts MUST start with `#! python3`
- C#: .NET framework via grasshopper's C# script component
- units: inches, always
- tolerance: document tolerance, typically 0.001" for modeling, 1/16"-1/8" for fab

## conventions

### python scripts

- use `rhinoscriptsyntax as rs` for UI/selection, `Rhino.Geometry` for math
- use `scriptcontext as sc` for doc access
- entry point pattern: define a main function, call it at bottom:
  ```python
  def my_command():
      # ...
      pass

  if __name__ == "__main__":
      my_command()
  ```
- error handling: fail loudly with `print()` messages. no silent failures.
- user interaction: `rs.GetObject`, `rs.ListBox`, `rs.MessageBox` for simple UI.
  `Rhino.Input.Custom.GetPoint` / `GetObject` for anything needing dynamic draw.
- for scripts with dynamic preview (like dim-3d), subclass `GetPoint` and
  override `OnDynamicDraw`

### grasshopper C# components

- file extension: `.cs`
- follows GH_ScriptInstance pattern with RunScript method
- type hints and output variable names must match component setup
- inputs/outputs documented in the summary comment block
- use `Rhino.Geometry.Unroller`, `Brep.CreateDevelopableLoft`, etc. directly
- NEVER use meshes as geometry output. surfaces/polysurfaces only.
  zero-thickness surfaces OK for intermediary ops if thickness is
  accounted for downstream.

### naming

- filenames: `kebab-case.py` or `kebab-case.cs`
- functions: `snake_case`
- classes: `PascalCase`
- constants: `UPPER_SNAKE`

### general

- geometry is always surfaces/polysurfaces, never meshes
- end state for parts: closed polysurfaces with material thickness
- data output format: CSV when exporting tabular data
- layer names follow numbered convention (01-14), see layer table below
- favor generalizable, reusable solutions over one-off hacks
- comprehensive fixes over incremental patches

## layer conventions

scripts that create/reference layers must use these names exactly:

```
01 - Default
02 - Reference        profiles, grids, scale figures
03 - Bake             GH output geometry
04 - Booleans         intersection geo, used booleans
05 - Clipping         layouts/drawings
06 - Annotations
07 - Jig              steel-framed build jig
08 - Static parts     3D model by assembly (Hull bottoms, Hardtop, etc.)
09 - Ink lines        bend marks per part
10 - Fab 3D           in-situ vs flat plate states
11 - 2D geo           flat patterns, sublayers: Inside cut / Outside cut / Mark
12 - Cutfiles
13 - Title block
14 - zArchive
```

sublayer separator is `::` (e.g., `11 - 2D geo::Inside cut`).
sublayer colors for 2D geo: magenta (inside cut), dark green (mark), blue (outside cut).

## domain context

this is for raider boats (raiderboats.com). high-end welded aluminum fishing
boats for PNW offshore. models range 21'-30'.

fabrication pipeline: parametric rhino/GH model -> flat patterns -> DXF ->
RhinoCAM -> G-code -> Multicam 5000 CNC router (5'x10' bed).

materials: aluminum sheet 0.125"-0.250" (occasional 0.375").
alloys: 5052-H32 general, 5086-H116 for hull bottoms/CVK.
press brake limit: 0.190" x 13'.
bend allowance: k-factor method, 0.40-0.45 typical for air-bent aluminum.

key geometric concepts:
- developable surfaces (aluminum bends, doesn't stretch)
- DevLoft / CreateDevelopableLoft for hull panels that capture real forming behavior
- ruling lines matter for accurate t-frame cross-sections
- unrolling surfaces to flat patterns is a core operation
- chine, sheer, CVK (center vertical keel) are primary hull curves

## writing new scripts

1. check if similar functionality exists in the repo first
2. include a docstring at the top explaining purpose and usage
3. include alias suggestion if it's a standalone script:
   `alias: my-script -> _-RunPythonScript "path/to/my-script.py"`
4. test with real geometry from the boat models, not toy geometry
5. handle edge cases: empty selections, single-face breps, coincident points, etc.

## things to watch out for

- IronPython 2 vs CPython 3: f-strings only work in python 3 scripts.
  use `.format()` for python 2 compatibility or add the `#! python3` shebang.
- `rs.ObjectType()` returns different values than `ObjectType` enum -- don't mix
- `sc.doc` vs `ghdoc`: in grasshopper scripts, `sc.doc` is the rhino doc.
  set `sc.doc = ghdoc` only if you need to operate on GH geometry.
- brep face normals: check `OrientationIsReversed` before trusting `NormalAt`
- `sc.sticky` persists across script runs in the same rhino session.
  use it for toggle state, cached values, etc. keys should be namespaced.
- when working with layers, always check `rs.IsLayer()` before creating
- System.Drawing.Color, not python tuples, for layer colors
