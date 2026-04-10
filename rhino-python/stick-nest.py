#! python3
# -*- coding: utf-8 -*-
"""
stick-nest.py
-------------
Linear nesting cut optimizer for stick-type extrusions (channel, tube, angle).
Reads profile/length/location UserText attributes from selected objects,
runs best-fit decreasing bin packing per profile, outputs a printable cut
recipe with fractional inch display.

alias: stick-nest -> _-RunPythonScript "path/to/stick-nest.py"

OBJECT SETUP (per object in Rhino):
  1. Select an object and open Properties (F3)
  2. In "Attribute User Text", add:
       profile   e.g. 2x1_channel, 1.5_sq_tube
       length    set by length-attribute.py (or manual)
       location  e.g. aft floor, cabin floor, t-frames  (optional)
"""

import rhinoscriptsyntax as rs
import scriptcontext as sc
import Rhino
import math
import datetime
import csv
import json
import os
import System.Windows.Forms as WinForms
import System.Drawing as Drawing


# ---- Unit conversion (from bom-mvw_001.py) -----------------------------------

def to_inches(value):
    """Convert a value from the current Rhino unit system to inches."""
    meters_per_unit = Rhino.RhinoMath.UnitScale(
        sc.doc.ModelUnitSystem, Rhino.UnitSystem.Meters
    )
    inches_per_meter = 39.3701
    return value * meters_per_unit * inches_per_meter


def floor_to_sixteenth(inches):
    """Floor a value in inches DOWN to the nearest 1/16."""
    return math.floor(inches * 16.0) / 16.0


# ---- Fractional display ------------------------------------------------------

def fmt_fraction(inches):
    """Format inches as fractional string: '18 3/4' or '24' or '0'."""
    if inches <= 0:
        return "0"
    whole = int(inches)
    sixteenths = int(round((inches - whole) * 16))
    if sixteenths == 16:
        whole += 1
        sixteenths = 0
    if sixteenths == 0:
        return str(whole)
    g = math.gcd(sixteenths, 16)
    num = sixteenths // g
    den = 16 // g
    if whole == 0:
        return "{}/{}".format(num, den)
    return "{} {}/{}".format(whole, num, den)


# ---- Data structures ---------------------------------------------------------

class Bin:
    """One stock stick with cuts placed on it."""
    def __init__(self, stock_length):
        self.stock_length = stock_length
        self.cuts = []       # list of (length, location) tuples
        self.remaining = stock_length

    def can_fit(self, length, kerf):
        needed = length + (kerf if self.cuts else 0)
        return self.remaining >= needed

    def add(self, length, location, kerf):
        needed = length + (kerf if self.cuts else 0)
        self.cuts.append((length, location))
        self.remaining -= needed


# ---- Object collection -------------------------------------------------------

def get_objects():
    """Get objects to process: pre-selected > auto-scan > manual prompt."""
    pre = rs.SelectedObjects()
    if pre:
        print("  using {} pre-selected object(s)".format(len(pre)))
        return list(pre)

    # auto-scan: everything with a "profile" attribute
    all_objs = rs.AllObjects()
    if all_objs:
        attributed = []
        for obj in all_objs:
            val = rs.GetUserText(obj, "profile")
            if val:
                attributed.append(obj)
        if attributed:
            print("  found {} object(s) with 'profile' attribute".format(len(attributed)))
            return attributed

    # manual prompt
    picked = rs.GetObjects("Select objects for stick nesting")
    if picked:
        return list(picked)

    return None


def collect_cuts(obj_ids):
    """
    Read profile/length/location from objects.
    Returns (profile_cuts, errors) where:
      profile_cuts = {profile: [(length_inches, location), ...]}
      errors = [(obj_id, reason), ...]
    """
    profile_cuts = {}
    errors = []

    for obj_id in obj_ids:
        profile = rs.GetUserText(obj_id, "profile")
        if not profile:
            errors.append((obj_id, "missing 'profile' attribute"))
            continue

        length_str = rs.GetUserText(obj_id, "length")
        if not length_str:
            errors.append((obj_id, "missing 'length' attribute"))
            continue

        try:
            raw_length = float(length_str)
        except ValueError:
            errors.append((obj_id, "non-numeric 'length': {}".format(length_str)))
            continue

        length_inches = floor_to_sixteenth(to_inches(raw_length))
        if length_inches <= 0:
            errors.append((obj_id, "zero or negative length after conversion"))
            continue

        location = rs.GetUserText(obj_id, "location") or "(unlabeled)"
        location = location.strip()

        profile = profile.strip()
        if profile not in profile_cuts:
            profile_cuts[profile] = []
        profile_cuts[profile].append((length_inches, location))

    return profile_cuts, errors


def report_errors(errors, total_objects):
    """Print errors and ask whether to continue if partial."""
    if not errors:
        return True

    print("")
    print("  WARNINGS: {} of {} objects skipped".format(len(errors), total_objects))
    for obj_id, reason in errors[:20]:
        layer = rs.ObjectLayer(obj_id) or "?"
        print("    [{}] {}".format(layer, reason))
    if len(errors) > 20:
        print("    ... and {} more".format(len(errors) - 20))

    valid = total_objects - len(errors)
    if valid <= 0:
        print("  no valid objects. aborting.")
        return False

    result = rs.MessageBox(
        "{} of {} objects have issues (see command line).\n\nContinue with {} valid objects?".format(
            len(errors), total_objects, valid
        ),
        4 | 48,  # YesNo | Warning
        "Stick Nesting"
    )
    return result == 6  # Yes


# ---- Bin packing -------------------------------------------------------------

def best_fit_decreasing(cuts, stock_length, kerf):
    """
    Best-fit decreasing 1D bin packing.
    cuts: list of (length, location) tuples

    Oversize cuts (longer than stock) each consume N-1 full sticks plus a
    partial from the Nth stick.  The partials are collected and bin-packed
    together so that offcuts from separate oversize pieces share sticks.

    Returns (bins, oversize_info) where:
      bins      = list of Bin (normal + oversize-partial bins)
      oversize_info = {
          "cuts":        [(length, location, full_sticks, partial), ...],
          "full_sticks": total full sticks consumed,
          "partial_bins": list of Bin for the partial pieces,
      }
    """
    oversize_cuts = []
    partials = []       # (partial_length, location) to bin-pack
    total_full = 0
    for l, loc in cuts:
        if l > stock_length:
            full = int(l // stock_length)       # full sticks consumed entirely
            partial = l - full * stock_length   # leftover partial piece
            total_full += full
            oversize_cuts.append((l, loc, full, partial))
            if partial > 0:
                partials.append((partial, f"{loc} (oversize partial)"))

    fittable = [(l, loc) for l, loc in cuts if l <= stock_length]

    # bin-pack the oversize partials together with normal cuts
    all_to_pack = fittable + partials
    all_to_pack.sort(key=lambda c: c[0], reverse=True)

    bins = []
    for length, location in all_to_pack:
        best_idx = -1
        best_leftover = float("inf")

        for i, b in enumerate(bins):
            if b.can_fit(length, kerf):
                leftover = b.remaining - length - (kerf if b.cuts else 0)
                if leftover < best_leftover:
                    best_leftover = leftover
                    best_idx = i

        if best_idx >= 0:
            bins[best_idx].add(length, location, kerf)
        else:
            new_bin = Bin(stock_length)
            new_bin.add(length, location, kerf)
            bins.append(new_bin)

    oversize_info = {
        "cuts": oversize_cuts,
        "full_sticks": total_full,
    }
    return bins, oversize_info


# ---- Layout grouping ---------------------------------------------------------

def group_identical_layouts(bins):
    """
    Group bins with identical sorted cut lengths.
    Returns list of dicts: {id, count, cuts, remnant, all_bins}
    sorted by count descending then remnant ascending.
    """
    groups = {}
    for b in bins:
        sig = tuple(sorted([c[0] for c in b.cuts]))
        if sig not in groups:
            groups[sig] = []
        groups[sig].append(b)

    result = []
    for sig, bin_list in groups.items():
        rep = bin_list[0]  # representative bin
        result.append({
            "count": len(bin_list),
            "cuts": rep.cuts,           # (length, location) from representative
            "remnant": rep.remaining,
            "all_bins": bin_list,
        })

    # sort: most repeated first, then least waste
    result.sort(key=lambda g: (-g["count"], g["remnant"]))

    # assign layout IDs
    for i, g in enumerate(result):
        if i < 26:
            g["id"] = chr(65 + i)
        else:
            g["id"] = chr(65 + (i // 26) - 1) + chr(65 + (i % 26))

    return result


# ---- Configuration -----------------------------------------------------------

CONFIG_FILENAME = "stick-nest-config.json"


def _default_config():
    return {"default_stock_length": 240.0, "kerf": 0.125, "profiles": {}}


def config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config():
    """Load config from JSON, falling back to defaults. Re-reads if file changed."""
    path = config_path()
    try:
        file_mtime = os.path.getmtime(path)
    except OSError:
        file_mtime = 0

    cached = sc.sticky.get("stick_nest_config")
    cached_mtime = sc.sticky.get("stick_nest_config_mtime", -1)
    if cached and cached_mtime == file_mtime:
        return cached

    cfg = _default_config()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            cfg["default_stock_length"] = data.get("default_stock_length", cfg["default_stock_length"])
            cfg["kerf"] = data.get("kerf", cfg["kerf"])
            cfg["profiles"] = data.get("profiles", {})
        except (json.JSONDecodeError, IOError) as e:
            print(f"  WARNING: corrupt config at {path}: {e}")
            print("  using defaults")

    sc.sticky["stick_nest_config"] = cfg
    sc.sticky["stick_nest_config_mtime"] = file_mtime
    return cfg


def save_config(cfg):
    """Write config to JSON and update session cache."""
    path = config_path()
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    sc.sticky["stick_nest_config"] = cfg
    sc.sticky["stick_nest_config_mtime"] = os.path.getmtime(path)


def get_stock_length(cfg, profile):
    """Look up stock length for a profile, falling back to default."""
    entry = cfg["profiles"].get(profile)
    if entry and "stock_length" in entry:
        return entry["stock_length"]
    return cfg["default_stock_length"]


def prompt_unknown_profiles(cfg, discovered_profiles):
    """Prompt for stock lengths of profiles not yet in config. Returns False on cancel."""
    unknown = [p for p in sorted(discovered_profiles) if p not in cfg["profiles"]]
    if not unknown:
        return True

    print(f"  {len(unknown)} new profile(s) found -- need stock lengths:")
    for profile in unknown:
        stock = rs.GetReal(
            f"Stock length for '{profile}' (inches)",
            cfg["default_stock_length"], 12.0, 480.0
        )
        if stock is None:
            return False
        cfg["profiles"][profile] = {"stock_length": stock}
        print(f"    {profile}: {fmt_fraction(stock)}\"")

    save_config(cfg)
    return True


def manage_config():
    """Interactive config editor using ListBox dialogs."""
    cfg = load_config()
    changed = False

    while True:
        items = []
        items.append(f">> Default stock length: {fmt_fraction(cfg['default_stock_length'])}\"")
        items.append(f">> Kerf: {fmt_fraction(cfg['kerf'])}\"")
        items.append("---")
        for p in sorted(cfg["profiles"].keys()):
            sl = cfg["profiles"][p]["stock_length"]
            items.append(f"{p}  |  {fmt_fraction(sl)}\"")
        if not cfg["profiles"]:
            items.append("(no profiles configured)")
        items.append("---")
        items.append("[Add profile]")
        items.append("[Set default stock length]")
        items.append("[Set kerf]")
        items.append("[Done]")

        choice = rs.ListBox(items, "Manage stock lengths (select to edit)", "Stick Nest Config")
        if choice is None or choice == "[Done]":
            break
        elif choice == "[Add profile]":
            name = rs.GetString("Profile name")
            if name:
                sl = rs.GetReal(f"Stock length for '{name.strip()}'",
                                cfg["default_stock_length"], 12.0, 480.0)
                if sl is not None:
                    cfg["profiles"][name.strip()] = {"stock_length": sl}
                    changed = True
        elif choice == "[Set default stock length]":
            sl = rs.GetReal("Default stock length", cfg["default_stock_length"], 12.0, 480.0)
            if sl is not None:
                cfg["default_stock_length"] = sl
                changed = True
        elif choice == "[Set kerf]":
            k = rs.GetReal("Saw kerf", cfg["kerf"], 0.0, 1.0)
            if k is not None:
                cfg["kerf"] = k
                changed = True
        elif choice and not choice.startswith(">>") and choice != "---" and not choice.startswith("("):
            profile_name = choice.split("|")[0].strip()
            action = rs.ListBox(
                ["Edit stock length", "Remove (use default)", "Cancel"],
                f"Profile: {profile_name}",
                "Edit Profile"
            )
            if action == "Edit stock length":
                current = get_stock_length(cfg, profile_name)
                sl = rs.GetReal(f"Stock length for '{profile_name}'", current, 12.0, 480.0)
                if sl is not None:
                    cfg["profiles"][profile_name]["stock_length"] = sl
                    changed = True
            elif action == "Remove (use default)":
                cfg["profiles"].pop(profile_name, None)
                changed = True

    if changed:
        save_config(cfg)
        print("  config saved.")
    else:
        print("  no changes.")


# ---- Report builders ---------------------------------------------------------

def cut_marks(cuts, kerf):
    """Compute cumulative cut-mark positions from left end of stick.
    cuts: list of (length, location) sorted longest first (layout order).
    Returns list of mark positions (inches). N-1 marks for N pieces.
    Fabricator marks the line at the piece end and cuts to the right of it;
    kerf falls entirely to the right of the mark."""
    sorted_cuts = sorted(cuts, key=lambda c: -c[0])
    marks = []
    pos = 0.0
    for i, (length, location) in enumerate(sorted_cuts):
        pos += length
        marks.append(pos)
        if i < len(sorted_cuts) - 1:
            pos += kerf
    return marks


def build_report(profile_results, kerf):
    """Build monospaced report as list of strings."""
    lines = []
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")
    doc_name = sc.doc.Name if sc.doc.Name else "Unsaved"

    has_oversize = any(pr["oversize"]["cuts"] for pr in profile_results.values())

    lines.append("")
    lines.append("=" * 56)
    lines.append("  STICK NESTING - CUT RECIPE")
    lines.append("  Kerf: {}\"".format(fmt_fraction(kerf)))
    lines.append("  {}  |  {}".format(doc_name, timestamp))
    lines.append("=" * 56)
    lines.append("")
    lines.append("  INSTRUCTIONS")
    lines.append("  - (xN) = make N identical sticks with this layout")
    lines.append("  - Cumulative = measure from left end of stock,")
    lines.append("    mark the line, cut to the RIGHT of the mark")
    lines.append("  - Kerf ({}\" blade) is accounted for in cumulative".format(
        fmt_fraction(kerf)))
    if has_oversize:
        lines.append("  - OVERSIZE: piece longer than stock. needs")
        lines.append("    multiple sticks butt-welded. partials are")
        lines.append("    bin-packed into layouts below")

    grand_sticks = 0
    grand_layouts = 0
    grand_cut_length = 0.0
    grand_waste = 0.0
    grand_stock = 0.0
    profile_summaries = []

    for profile in sorted(profile_results.keys()):
        pr = profile_results[profile]
        layouts = pr["layouts"]
        oversize = pr["oversize"]
        stock_length = pr["stock_length"]

        profile_sticks = sum(g["count"] for g in layouts)
        profile_cut_length = 0.0
        profile_waste = 0.0

        lines.append("")
        lines.append("  PROFILE: {}  |  Stock: {}\"".format(profile, fmt_fraction(stock_length)))
        lines.append("  " + "-" * 52)

        os_cuts = oversize["cuts"]
        os_full = oversize["full_sticks"]
        if os_cuts:
            lines.append("  ** OVERSIZE (each needs {} full + partial):".format(
                os_cuts[0][2]  # full sticks per piece (same for identical cuts)
            ))
            for length, location, full, partial in os_cuts:
                lines.append("     {}  {}  ({} full + {} partial)".format(
                    fmt_fraction(length).ljust(14), location,
                    full, fmt_fraction(floor_to_sixteenth(partial))
                ))
                profile_cut_length += length
            profile_sticks += os_full
            lines.append("     {} full stick(s) consumed, partials bin-packed below".format(os_full))
            lines.append("")

        for g in layouts:
            lines.append("  Layout {}  (x{})".format(g["id"], g["count"]))
            lines.append("    {}  {}  {}".format(
                "Length".ljust(14), "Cumulative".ljust(14), "Location"
            ))

            sorted_cuts = sorted(g["cuts"], key=lambda c: -c[0])
            pos = 0.0
            for i, (length, location) in enumerate(sorted_cuts):
                pos += length
                cumul_str = fmt_fraction(floor_to_sixteenth(pos)).ljust(14)
                if i < len(sorted_cuts) - 1:
                    pos += kerf
                length_str = fmt_fraction(length).ljust(14)
                lines.append("    {}  {}  {}".format(length_str, cumul_str, location))

                profile_cut_length += length * g["count"]

            # remnant at the bottom of the cut list
            lines.append("    {}  {}  remnant".format(
                fmt_fraction(g["remnant"]).ljust(14), "".ljust(14)
            ))
            profile_waste += g["remnant"] * g["count"]
            lines.append("")

        p_stock = profile_sticks * stock_length
        p_waste_pct = (profile_waste / p_stock * 100) if p_stock > 0 else 0
        lines.append("  >> {}: {} stick(s), {} layout(s)".format(
            profile, profile_sticks, len(layouts)
        ))
        lines.append("     Cut: {}\"  |  Waste: {}\" ({:.1f}%)".format(
            fmt_fraction(floor_to_sixteenth(profile_cut_length)),
            fmt_fraction(floor_to_sixteenth(profile_waste)),
            p_waste_pct,
        ))
        lines.append("  " + "-" * 52)

        grand_sticks += profile_sticks
        grand_cut_length += profile_cut_length
        grand_waste += profile_waste
        grand_stock += p_stock
        profile_summaries.append((profile, stock_length, profile_sticks,
                                  profile_cut_length, profile_waste, p_waste_pct))

    lines.append("")
    lines.append("=" * 64)
    lines.append("  TOTALS")
    lines.append("  " + "-" * 60)
    lines.append("  {:20s} {:>6s} {:>5s} {:>10s} {:>10s} {:>5s}".format(
        "Profile", "Stock", "Stks", "Used", "Waste", "%"
    ))
    lines.append("  " + "-" * 60)
    for pname, psl, psticks, pcut, pwaste, ppct in profile_summaries:
        lines.append("  {:20s} {:>6s} {:>5} {:>10s} {:>10s} {:>4.1f}%".format(
            pname, fmt_fraction(psl) + '"', psticks,
            fmt_fraction(floor_to_sixteenth(pcut)) + '"',
            fmt_fraction(floor_to_sixteenth(pwaste)) + '"', ppct
        ))
    lines.append("  " + "-" * 60)
    waste_pct = (grand_waste / grand_stock * 100) if grand_stock > 0 else 0
    lines.append("  {:20s} {:>6s} {:>5} {:>10s} {:>10s} {:>4.1f}%".format(
        "TOTAL", "", grand_sticks,
        fmt_fraction(floor_to_sixteenth(grand_cut_length)) + '"',
        fmt_fraction(floor_to_sixteenth(grand_waste)) + '"', waste_pct
    ))
    lines.append("=" * 64)
    lines.append("")

    return lines


def build_tsv(profile_results):
    """Build tab-separated text for clipboard paste into Excel/Sheets."""
    rows = ["Profile\tLayout\tQty\tStock\t#\tCut Length\tLocation\tRemnant"]

    for profile in sorted(profile_results.keys()):
        pr = profile_results[profile]
        stock_length = pr["stock_length"]
        for g in pr["layouts"]:
            first = True
            for idx, (length, location) in enumerate(
                sorted(g["cuts"], key=lambda c: -c[0])
            ):
                remnant_str = fmt_fraction(g["remnant"]) if first else ""
                rows.append("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
                    profile if first else "",
                    g["id"] if first else "",
                    g["count"] if first else "",
                    fmt_fraction(stock_length) if first else "",
                    idx + 1,
                    fmt_fraction(length),
                    location,
                    remnant_str,
                ))
                first = False

    return "\r\n".join(rows)


def stick_diagram_html(cuts, remnant, stock_length, kerf):
    """Build HTML for a proportional stick bar diagram."""
    sorted_cuts = sorted(cuts, key=lambda c: -c[0])
    segments = []
    for i, (length, location) in enumerate(sorted_cuts):
        pct = length / stock_length * 100
        segments.append(
            f'<div class="cut" style="width:{pct:.2f}%">'
            f'<span>{fmt_fraction(length)}</span></div>'
        )
        if i < len(sorted_cuts) - 1:
            kpct = max(kerf / stock_length * 100, 0.15)
            segments.append(f'<div class="kerf" style="width:{kpct:.2f}%"></div>')
    rem_pct = remnant / stock_length * 100
    if rem_pct > 0.3:
        segments.append(
            f'<div class="remnant" style="width:{rem_pct:.2f}%">'
            f'<span>{fmt_fraction(remnant)}</span></div>'
        )
    return f'<div class="stick">{"".join(segments)}</div>'


def build_location_index(profile_results):
    """Build reverse index: {location: [{"profile", "length", "qty"}, ...]}."""
    raw = {}  # {location: [(profile, length), ...]}

    for profile, pr in profile_results.items():
        # normal layouts: iterate ALL bins for accurate locations
        for layout in pr["layouts"]:
            for bin_obj in layout["all_bins"]:
                for length, location in bin_obj.cuts:
                    if location.endswith("(oversize partial)"):
                        continue
                    raw.setdefault(location, []).append((profile, length))

        # oversize cuts: each is one piece
        for length, location, full, partial in pr["oversize"]["cuts"]:
            raw.setdefault(location, []).append((profile, length))

    # aggregate by (profile, length) per location
    result = {}
    for location, pieces in raw.items():
        counts = {}
        for profile, length in pieces:
            key = (profile, length)
            counts[key] = counts.get(key, 0) + 1
        rows = [{"profile": p, "length": l, "qty": q}
                for (p, l), q in sorted(counts.items())]
        result[location] = rows

    return result


def export_html(profile_results, kerf):
    """Build and save an HTML cut recipe with stick diagrams, open in browser."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")
    doc_name = sc.doc.Name if sc.doc.Name else "Unsaved"

    css = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: Arial, Helvetica, sans-serif; font-size: 10pt;
           max-width: 7.5in; margin: 0 auto; padding: 0.5in 0; color: #222; }
    @media print {
      body { max-width: none; padding: 0; }
      .no-print { display: none; }
      @page { size: letter portrait; margin: 0.5in; }
      .layout-block { break-inside: avoid; }
      .profile-section { break-inside: avoid-if-possible; }
      .profile-hdr, .profile-stock { break-after: avoid; }
      .totals-section { break-inside: avoid; }
      hr.light { break-after: auto; }
      .tab-content { display: none !important; }
      .tab-content.tab-active { display: block !important; }
    }
    h1 { font-size: 16pt; margin-bottom: 2pt; }
    .meta { color: #555; margin-bottom: 4pt; }
    hr.heavy { border: none; border-top: 2px solid #222; margin: 6pt 0 10pt; }
    hr.light { border: none; border-top: 1px solid #999; margin: 4pt 0 8pt; }
    .profile-hdr { font-size: 12pt; font-weight: bold; margin-top: 14pt; }
    .profile-stock { font-size: 9pt; color: #555; margin: 0 0 2pt 4pt; }
    .layout-block { margin-bottom: 2pt; }
    .layout-hdr { font-size: 10pt; font-weight: bold; margin: 8pt 0 3pt; }
    .oversize { color: #b40000; font-weight: bold; font-size: 9pt; margin: 2pt 0 2pt 8pt; }
    .stick { display: flex; border: 1px solid #555; height: 26px;
             margin: 2pt 0 4pt; overflow: hidden; }
    .cut { background: #d2e3f3; display: flex; align-items: center;
           justify-content: center; font-size: 8pt; border-right: 1px solid #fff;
           overflow: hidden; white-space: nowrap; min-width: 0; }
    .kerf { background: #b44; min-width: 1px; flex-shrink: 0; }
    .remnant { background: #ddd; display: flex; align-items: center;
               justify-content: center; font-size: 7pt; color: #777;
               overflow: hidden; white-space: nowrap; min-width: 0; }
    .cut-list { font-size: 9pt; margin: 0 0 6pt 10pt; }
    .cut-list div { margin: 1pt 0; }
    .cut-list-hdr { font-size: 8pt; color: #888; text-transform: uppercase;
                    letter-spacing: 0.5pt; margin-bottom: 1pt; }
    .cut-len { display: inline-block; width: 90px; }
    .cut-cumul { display: inline-block; width: 90px; color: #b44; }
    .remnant-line { color: #888; font-style: italic; }
    .ruler { position: relative; height: 20px; margin: 0 0 4pt; border-left: 1px solid #999; }
    .ruler-tick { position: absolute; top: 0; border-left: 1px solid #b44;
                  height: 10px; transform: translateX(-0.5px); }
    .ruler-label { position: absolute; top: 10px; font-size: 7pt; color: #b44;
                   transform: translateX(-50%); white-space: nowrap; }
    .subtotal { font-style: italic; font-size: 9pt; margin: 4pt 0 2pt; }
    .totals { font-size: 10pt; margin-top: 4pt; }
    .totals div { margin: 2pt 0; }
    .totals-table { border-collapse: collapse; font-size: 9pt; width: 100%; margin: 4pt 0; }
    .totals-table th { text-align: left; border-bottom: 1px solid #999; padding: 2pt 6pt;
                       font-weight: bold; }
    .totals-table td { padding: 2pt 6pt; }
    .totals-table tr.grand { border-top: 1px solid #222; font-weight: bold; }
    .instructions { font-size: 9pt; color: #444; margin: 0 0 6pt;
                    padding: 6pt 10pt; background: #f8f8f8; border: 1px solid #ddd; }
    .instructions div { margin: 2pt 0; }
    .instructions strong { font-size: 10pt; }
    .tab-bar { margin: 10pt 0 0; border-bottom: 2px solid #222; display: flex; gap: 0; }
    .tab-btn { padding: 6pt 16pt; font-size: 10pt; cursor: pointer;
               border: 1px solid #999; border-bottom: none; background: #eee;
               border-radius: 4px 4px 0 0; margin-bottom: -2px; }
    .tab-btn-active { background: #fff; border-color: #222; border-bottom: 2px solid #fff;
                      font-weight: bold; }
    .loc-table { border-collapse: collapse; font-size: 9pt; width: 100%; margin: 4pt 0 10pt; }
    .loc-table th { text-align: left; border-bottom: 1px solid #999; padding: 2pt 6pt;
                    font-weight: bold; }
    .loc-table td { padding: 2pt 6pt; }
    .loc-table tr.loc-sub { border-top: 1px solid #222; font-weight: bold; }
    .print-btn { margin: 12pt 0; padding: 6pt 16pt; font-size: 10pt; cursor: pointer; }
    """

    has_oversize = any(pr["oversize"]["cuts"] for pr in profile_results.values())

    parts = []
    parts.append(f"<h1>STICK NESTING - CUT RECIPE</h1>")
    parts.append(f'<div class="meta">Kerf: {fmt_fraction(kerf)}"</div>')
    parts.append(f'<div class="meta">{doc_name} &nbsp;|&nbsp; {timestamp}</div>')
    parts.append('<hr class="heavy">')
    parts.append('<div class="instructions">')
    parts.append('<strong>Instructions</strong>')
    parts.append(f'<div><b>(xN)</b> = make N identical sets of this layout</div>')
    parts.append(f'<div><b>Cumulative</b> = measure from left end of stock, '
                 f'mark the line, cut to the <b>right</b> of the mark</div>')
    parts.append(f'<div>Kerf ({fmt_fraction(kerf)}" blade) is accounted for '
                 f'in cumulative measurements</div>')
    if has_oversize:
        parts.append('<div><b style="color:#b40000">OVERSIZE</b> = piece longer '
                     'than stock, needs multiple sticks butt-welded. '
                     'Partial offcuts are bin-packed into layouts below</div>')
    parts.append('</div>')
    parts.append('<div class="tab-bar no-print">')
    parts.append('<button class="tab-btn tab-btn-active" onclick="switchTab(\'profile\')" '
                 'id="btn-profile">By Profile</button>')
    parts.append('<button class="tab-btn" onclick="switchTab(\'location\')" '
                 'id="btn-location">By Location</button>')
    parts.append('</div>')

    parts.append('<div id="tab-profile" class="tab-content tab-active">')

    grand_sticks = 0
    grand_layouts = 0
    grand_cut_length = 0.0
    grand_waste = 0.0
    grand_stock = 0.0
    profile_summaries = []

    for profile in sorted(profile_results.keys()):
        pr = profile_results[profile]
        layouts = pr["layouts"]
        oversize = pr["oversize"]
        stock_length = pr["stock_length"]
        profile_sticks = sum(g["count"] for g in layouts)
        profile_cut_length = 0.0
        profile_waste = 0.0

        parts.append(f'<div class="profile-hdr">PROFILE: {profile}</div>')
        parts.append(f'<div class="profile-stock">Stock: {fmt_fraction(stock_length)}"</div>')
        parts.append('<hr class="light">')

        os_cuts = oversize["cuts"]
        os_full = oversize["full_sticks"]
        if os_cuts:
            for length, loc, full, partial in os_cuts:
                parts.append(f'<div class="oversize">OVERSIZE: '
                             f'{fmt_fraction(length)}" &nbsp; {loc} '
                             f'&mdash; {full} full + '
                             f'{fmt_fraction(floor_to_sixteenth(partial))}" partial</div>')
                profile_cut_length += length
            profile_sticks += os_full
            parts.append(f'<div class="oversize" style="font-weight:normal">'
                         f'{os_full} full stick(s) consumed, '
                         f'partials bin-packed below</div>')

        for g in layouts:
            parts.append('<div class="layout-block">')
            parts.append(f'<div class="layout-hdr">Layout {g["id"]} &nbsp;(x{g["count"]})</div>')

            parts.append(stick_diagram_html(g["cuts"], g["remnant"], stock_length, kerf))

            # cumulative cut-mark ruler
            marks = cut_marks(g["cuts"], kerf)
            if marks:
                ticks = []
                for m in marks:
                    pct = m / stock_length * 100
                    label = fmt_fraction(floor_to_sixteenth(m))
                    ticks.append(
                        f'<div class="ruler-tick" style="left:{pct:.2f}%"></div>'
                        f'<div class="ruler-label" style="left:{pct:.2f}%">{label}</div>'
                    )
                parts.append(f'<div class="ruler">{"".join(ticks)}</div>')

            sorted_cuts = sorted(g["cuts"], key=lambda c: -c[0])
            parts.append('<div class="cut-list">')
            parts.append('<div class="cut-list-hdr">'
                         '<span class="cut-len">Length</span>'
                         '<span class="cut-cumul">Cumulative</span>'
                         'Location</div>')
            pos = 0.0
            for i, (length, location) in enumerate(sorted_cuts):
                pos += length
                cumul_str = f'{fmt_fraction(floor_to_sixteenth(pos))}"'
                if i < len(sorted_cuts) - 1:
                    pos += kerf
                parts.append(f'<div><span class="cut-len">{fmt_fraction(length)}"</span>'
                             f'<span class="cut-cumul">{cumul_str}</span>'
                             f'{location}</div>')
                profile_cut_length += length * g["count"]
            parts.append(f'<div class="remnant-line"><span class="cut-len">'
                         f'{fmt_fraction(g["remnant"])}"</span>'
                         f'<span class="cut-cumul"></span>remnant</div>')
            parts.append('</div>')
            parts.append('</div>')  # layout-block

            profile_waste += g["remnant"] * g["count"]

        p_stock = profile_sticks * stock_length
        p_waste_pct = (profile_waste / p_stock * 100) if p_stock > 0 else 0
        parts.append(f'<div class="subtotal">&gt;&gt; {profile}: '
                     f'{profile_sticks} stick(s), {len(layouts)} layout(s) '
                     f'&mdash; waste: {fmt_fraction(floor_to_sixteenth(profile_waste))}" '
                     f'({p_waste_pct:.1f}%)</div>')
        parts.append('<hr class="light">')

        grand_sticks += profile_sticks
        grand_cut_length += profile_cut_length
        grand_waste += profile_waste
        grand_stock += p_stock
        profile_summaries.append((profile, stock_length, profile_sticks,
                                  profile_cut_length, profile_waste, p_waste_pct))

    parts.append('</div>')  # tab-profile

    # ---- by-location tab ----
    location_data = build_location_index(profile_results)

    parts.append('<div id="tab-location" class="tab-content" style="display:none">')
    for location in sorted(location_data.keys()):
        rows = location_data[location]
        total_pieces = sum(r["qty"] for r in rows)
        total_length = sum(r["length"] * r["qty"] for r in rows)

        parts.append(f'<div class="profile-hdr">LOCATION: {location}</div>')
        parts.append('<hr class="light">')
        parts.append('<table class="loc-table">')
        parts.append('<tr><th>Profile</th><th>Length</th><th>Qty</th><th>Subtotal</th></tr>')
        for r in rows:
            sub = r["length"] * r["qty"]
            parts.append(f'<tr><td>{r["profile"]}</td>'
                         f'<td>{fmt_fraction(r["length"])}"</td>'
                         f'<td>{r["qty"]}</td>'
                         f'<td>{fmt_fraction(floor_to_sixteenth(sub))}"</td></tr>')
        parts.append(f'<tr class="loc-sub"><td>Subtotal</td><td></td>'
                     f'<td>{total_pieces}</td>'
                     f'<td>{fmt_fraction(floor_to_sixteenth(total_length))}"</td></tr>')
        parts.append('</table>')
    parts.append('</div>')  # tab-location

    waste_pct = (grand_waste / grand_stock * 100) if grand_stock > 0 else 0

    parts.append('<div class="totals-section">')
    parts.append('<hr class="heavy">')
    parts.append('<div class="totals"><strong>TOTALS</strong></div>')
    parts.append('<table class="totals-table">')
    parts.append('<tr><th>Profile</th><th>Stock</th><th>Sticks</th>'
                 '<th>Used</th><th>Waste</th><th>%</th></tr>')
    for pname, psl, psticks, pcut, pwaste, ppct in profile_summaries:
        parts.append(f'<tr><td>{pname}</td>'
                     f'<td>{fmt_fraction(psl)}"</td><td>{psticks}</td>'
                     f'<td>{fmt_fraction(floor_to_sixteenth(pcut))}"</td>'
                     f'<td>{fmt_fraction(floor_to_sixteenth(pwaste))}"</td>'
                     f'<td>{ppct:.1f}%</td></tr>')
    parts.append(f'<tr class="grand"><td>TOTAL</td>'
                 f'<td></td><td>{grand_sticks}</td>'
                 f'<td>{fmt_fraction(floor_to_sixteenth(grand_cut_length))}"</td>'
                 f'<td>{fmt_fraction(floor_to_sixteenth(grand_waste))}"</td>'
                 f'<td>{waste_pct:.1f}%</td></tr>')
    parts.append('</table>')
    parts.append('<hr class="heavy">')
    parts.append('</div>')  # totals-section
    parts.append('<button class="print-btn no-print" onclick="window.print()">'
                 'Print / Save PDF</button>')

    body = "\n".join(parts)
    js = """
function switchTab(which) {
  document.querySelectorAll('.tab-content').forEach(function(el) {
    el.style.display = 'none';
    el.classList.remove('tab-active');
  });
  document.querySelectorAll('.tab-btn').forEach(function(el) {
    el.classList.remove('tab-btn-active');
  });
  document.getElementById('tab-' + which).style.display = 'block';
  document.getElementById('tab-' + which).classList.add('tab-active');
  document.getElementById('btn-' + which).classList.add('tab-btn-active');
}
"""
    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Stick Nesting - {doc_name}</title>
<style>{css}</style>
</head><body>
{body}
<script>{js}</script>
</body></html>"""

    # save and open
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    base = doc_name.replace(".3dm", "")
    default_name = f"{base}_stick-nest.html"

    path = rs.SaveFileName(
        "Save cut recipe HTML",
        "HTML Files (*.html)|*.html",
        desktop,
        default_name,
    )
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  saved: {path}")
        os.startfile(path)


def export_csv(profile_results):
    """Prompt for save location and write CSV."""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    doc_name = sc.doc.Name if sc.doc.Name else "untitled"
    base = doc_name.replace(".3dm", "")
    default_name = "{}_stick-nest.csv".format(base)

    path = rs.SaveFileName(
        "Save cut recipe CSV",
        "CSV Files (*.csv)|*.csv",
        desktop,
        default_name,
    )
    if not path:
        return

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Profile", "Layout", "Qty", "Stock", "#", "Cut Length", "Location", "Remnant"])

        for profile in sorted(profile_results.keys()):
            pr = profile_results[profile]
            stock_length = pr["stock_length"]
            for g in pr["layouts"]:
                first = True
                for idx, (length, location) in enumerate(
                    sorted(g["cuts"], key=lambda c: -c[0])
                ):
                    w.writerow([
                        profile if first else "",
                        g["id"] if first else "",
                        g["count"] if first else "",
                        fmt_fraction(stock_length) if first else "",
                        idx + 1,
                        fmt_fraction(length),
                        location,
                        fmt_fraction(g["remnant"]) if first else "",
                    ])
                    first = False

    print("  saved: {}".format(path))


# ---- Popup window ------------------------------------------------------------

def show_popup(lines, tsv_text, profile_results, kerf):
    """Display report in a scrollable popup with copy, CSV, HTML export buttons."""
    form = WinForms.Form()
    form.Text = "Stick Nesting - Cut Recipe"
    form.Width = 600
    form.Height = 700
    form.MinimumSize = Drawing.Size(400, 300)
    form.StartPosition = WinForms.FormStartPosition.CenterScreen

    textbox = WinForms.TextBox()
    textbox.Multiline = True
    textbox.ScrollBars = WinForms.ScrollBars.Vertical
    textbox.ReadOnly = True
    textbox.Font = Drawing.Font("Courier New", 9)
    textbox.Dock = WinForms.DockStyle.Fill
    textbox.Text = "\r\n".join(lines)

    btn_panel = WinForms.Panel()
    btn_panel.Dock = WinForms.DockStyle.Bottom
    btn_panel.Height = 36

    copy_btn = WinForms.Button()
    copy_btn.Text = "Copy to Sheets"
    copy_btn.Width = 130
    copy_btn.Height = 30
    copy_btn.Left = 4
    copy_btn.Top = 3

    csv_btn = WinForms.Button()
    csv_btn.Text = "Save CSV..."
    csv_btn.Width = 100
    csv_btn.Height = 30
    csv_btn.Left = 140
    csv_btn.Top = 3

    btn_x = 246
    html_btn = WinForms.Button()
    html_btn.Text = "Save HTML..."
    html_btn.Width = 100
    html_btn.Height = 30
    html_btn.Left = btn_x
    html_btn.Top = 3

    def on_html(s, e):
        export_html(profile_results, kerf)

    html_btn.Click += on_html
    btn_x += 106

    close_btn = WinForms.Button()
    close_btn.Text = "Close"
    close_btn.Width = 80
    close_btn.Height = 30
    close_btn.Left = btn_x
    close_btn.Top = 3

    def on_copy(s, e):
        WinForms.Clipboard.SetText(tsv_text)
        copy_btn.Text = "Copied!"

    def on_csv(s, e):
        export_csv(profile_results)

    copy_btn.Click += on_copy
    csv_btn.Click += on_csv
    close_btn.Click += lambda s, e: form.Close()

    btn_panel.Controls.Add(copy_btn)
    btn_panel.Controls.Add(csv_btn)
    btn_panel.Controls.Add(html_btn)
    btn_panel.Controls.Add(close_btn)

    form.Controls.Add(textbox)
    form.Controls.Add(btn_panel)

    form.ShowDialog()


# ---- Main --------------------------------------------------------------------

def main():
    obj_ids = get_objects()
    if not obj_ids:
        choice = rs.ListBox(
            ["Manage stock lengths", "Cancel"],
            "No objects found. What would you like to do?",
            "Stick Nesting"
        )
        if choice == "Manage stock lengths":
            manage_config()
        return

    profile_cuts, errors = collect_cuts(obj_ids)
    if not report_errors(errors, len(obj_ids)):
        return

    if not profile_cuts:
        print("  no valid profile/length data found.")
        return

    cfg = load_config()
    kerf = cfg["kerf"]

    # prompt for any new profiles not yet in config
    if not prompt_unknown_profiles(cfg, set(profile_cuts.keys())):
        return

    # show what we're using
    print("")
    for profile in sorted(profile_cuts.keys()):
        sl = get_stock_length(cfg, profile)
        print(f"  {profile}: stock {fmt_fraction(sl)}\", kerf {fmt_fraction(kerf)}\"")

    # run bin packing per profile
    profile_results = {}
    for profile, cuts in profile_cuts.items():
        sl = get_stock_length(cfg, profile)
        bins, oversize_info = best_fit_decreasing(cuts, sl, kerf)
        layouts = group_identical_layouts(bins)
        profile_results[profile] = {
            "layouts": layouts,
            "oversize": oversize_info,
            "stock_length": sl,
        }

    lines = build_report(profile_results, kerf)
    tsv_text = build_tsv(profile_results)

    for line in lines:
        print(line)

    show_popup(lines, tsv_text, profile_results, kerf)


if __name__ == "__main__":
    main()
