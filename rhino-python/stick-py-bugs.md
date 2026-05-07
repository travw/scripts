# stick-nest.py — bugs and shortcomings

audit notes. urgency in three buckets.

## bugs (fix these)

### 1. layout grouping silently drops per-bin locations
`group_identical_layouts` (line ~254) keys by sorted lengths only, then uses
`bin_list[0].cuts` as the representative. two bins with identical lengths but
DIFFERENT locations collapse into one displayed layout using only the first
bin's labels.

example: layout A (x2). bin 1 = [(24, "aft floor"), (12, "aft floor")].
bin 2 = [(24, "fwd floor"), (12, "cabin floor")]. report shows both as
"aft floor / aft floor". fabricator cuts the right lengths but doesn't know
which piece goes where.

affected: `build_report`, `build_tsv`, `export_csv`, html "by profile" tab.
`build_location_index` iterates `all_bins` correctly so the by-location view
is accurate.

fix: aggregate locations per (length) within each layout group, OR refuse
to merge bins with mismatched location sets.

### 2. oversize header lies when oversize lengths differ
`build_report` line ~506:
```python
lines.append("  ** OVERSIZE (each needs {} full + partial):".format(
    os_cuts[0][2]
))
```
comment claims "same for identical cuts" but `os_cuts` can contain different
lengths with different `full` counts. drop the header or compute per-row.

### 3. html injection on user-edited strings
profile names and location strings go raw into html via f-strings (lines ~792,
800-803, 879, 885 etc). a location containing `<` eats the rest of the line.
fix: `import html; html.escape(s)` per insertion.

### 4. invalid css `break-inside: avoid-if-possible`
line ~687. not a real value. chrome treats as `auto`. profile sections split
across pages anyway. use `avoid` or remove.

### 5. `save_config` is not atomic
line ~333. crash mid-write zeros the config. write to `path + ".tmp"` then
`os.replace(tmp, path)`.

### 6. tsv tab/newline injection
`build_tsv` line ~603. a location with `\t` or `\n` corrupts the tsv. strip
or quote per field.

## shortcomings (add these)

### remnant inventory
biggest waste-reduction feature missing. shop keeps offcut rack. add a
config section listing existing remnants per profile, treat them as
pre-existing bins, sort longest-first, BFD into them before fresh sticks.
~30 lines plus a config schema bump.

### rhino bake
draw each stick layout in 2D on layer 11 with cut marks, length labels,
layout id. lets you print straight from rhino and skip the browser dance.
~50 lines.

### auto-scan is too eager
`rs.AllObjects()` grabs hidden/locked. consider filtering by visible-layer
or confirming when count > 100.

### hardcoded fab tolerance
`floor_to_sixteenth` is hardwired. make `round_to` a config field
(1/16, 1/32, 1/8).

### hardcoded stock length range
`rs.GetReal(... 12.0, 480.0)` will reject 6" stubs and 600" sticks.

### bfd leaves 1-2% on the table
trivial wins:
- run BFD and FFD, keep the one using fewer sticks
- run BFD with K random shuffles, keep best
gate behind a cuts.length threshold to keep it fast.

### no "match pair" concept
port/stbd parts that must come from the same stock. probably out of scope.

## minor

- `(oversize partial)` magic suffix used for filtering in
  `build_location_index` via `endswith`. fragile. add a flag tuple element.
- `grand_layouts` declared in `build_report` (~483) and `export_html` (~777),
  never used. dead.
- `(unlabeled)` placeholder collides with user-typed value. use `None`
  internally and stringify at display.
- mtime cache has 1s resolution; rapid edits miss. config is ~1KB json,
  just reload every time.
- `prompt_unknown_profiles` cancel mid-loop discards entered profiles.
  save incrementally.
- popup is missing a "save txt" button despite already having `lines`.

## verdict

solid working tool. layout-grouping location loss is the only urgent bug
(misleads fabricator). html escape + atomic config save are quick hardening.
remnant inventory is the highest-value feature; rhino bake close second.
