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
import os
import System.Windows.Forms as WinForms
import System.Drawing as Drawing

import webbrowser


# ---- Constants ---------------------------------------------------------------

DEFAULT_STOCK_LENGTH = 240.0   # 20 ft
DEFAULT_KERF = 0.125           # 1/8" saw blade


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
    Returns (bins, oversize) where oversize is list of cuts that don't fit.
    """
    oversize = [(l, loc) for l, loc in cuts if l > stock_length]
    fittable = [(l, loc) for l, loc in cuts if l <= stock_length]

    # sort longest first
    fittable.sort(key=lambda c: c[0], reverse=True)

    bins = []
    for length, location in fittable:
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

    return bins, oversize


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

def prompt_config():
    """Prompt for stock length and kerf. Returns (stock, kerf) or None."""
    stock = rs.GetReal("Stock length (inches)", DEFAULT_STOCK_LENGTH, 12.0, 480.0)
    if stock is None:
        return None
    kerf = rs.GetReal("Saw kerf (inches)", DEFAULT_KERF, 0.0, 1.0)
    if kerf is None:
        return None
    return (stock, kerf)


# ---- Report builders ---------------------------------------------------------

def build_report(profile_results, stock_length, kerf):
    """Build monospaced report as list of strings."""
    lines = []
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")
    doc_name = sc.doc.Name if sc.doc.Name else "Unsaved"

    lines.append("")
    lines.append("=" * 56)
    lines.append("  STICK NESTING - CUT RECIPE")
    lines.append("  Stock: {}\"  |  Kerf: {}\"".format(
        fmt_fraction(stock_length), fmt_fraction(kerf)
    ))
    lines.append("  {}  |  {}".format(doc_name, timestamp))
    lines.append("=" * 56)

    grand_sticks = 0
    grand_layouts = 0
    grand_cut_length = 0.0
    grand_waste = 0.0

    for profile in sorted(profile_results.keys()):
        pr = profile_results[profile]
        layouts = pr["layouts"]
        oversize = pr["oversize"]

        profile_sticks = sum(g["count"] for g in layouts)
        grand_sticks += profile_sticks
        grand_layouts += len(layouts)

        lines.append("")
        lines.append("  PROFILE: {}".format(profile))
        lines.append("  " + "-" * 52)

        if oversize:
            lines.append("  ** OVERSIZE (won't fit in stock):")
            for length, location in oversize:
                lines.append("     {}  {}".format(
                    fmt_fraction(length).ljust(14), location
                ))
            lines.append("")

        for g in layouts:
            lines.append("  Layout {}  (x{})  |  Remnant: {}".format(
                g["id"], g["count"], fmt_fraction(g["remnant"])
            ))

            # group identical cuts within this layout
            cut_groups = {}
            for length, location in g["cuts"]:
                key = (length, location)
                cut_groups[key] = cut_groups.get(key, 0) + 1

            for (length, location), qty in sorted(cut_groups.items(), key=lambda x: -x[0][0]):
                length_str = fmt_fraction(length).ljust(14)
                if qty > 1:
                    lines.append("    {}  {}  (x{})".format(length_str, location, qty))
                else:
                    lines.append("    {}  {}".format(length_str, location))

                grand_cut_length += length * qty * g["count"]

            grand_waste += g["remnant"] * g["count"]
            lines.append("")

        lines.append("  >> {}: {} stick(s), {} layout(s)".format(
            profile, profile_sticks, len(layouts)
        ))
        lines.append("  " + "-" * 52)

    total_stock = grand_sticks * stock_length
    waste_pct = (grand_waste / total_stock * 100) if total_stock > 0 else 0

    lines.append("")
    lines.append("=" * 56)
    lines.append("  TOTALS")
    lines.append("  Sticks: {}  |  Layouts: {}".format(grand_sticks, grand_layouts))
    lines.append("  Total cut length: {}\"".format(fmt_fraction(floor_to_sixteenth(grand_cut_length))))
    lines.append("  Waste: {}\"  ({:.1f}%)".format(
        fmt_fraction(floor_to_sixteenth(grand_waste)), waste_pct
    ))
    lines.append("=" * 56)
    lines.append("")

    return lines


def build_tsv(profile_results, stock_length):
    """Build tab-separated text for clipboard paste into Excel/Sheets."""
    rows = ["Profile\tLayout\tQty\tStock\t#\tCut Length\tLocation\tRemnant"]

    for profile in sorted(profile_results.keys()):
        layouts = profile_results[profile]["layouts"]
        for g in layouts:
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


def export_html(profile_results, stock_length, kerf):
    """Build and save an HTML cut recipe with stick diagrams, open in browser."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")
    doc_name = sc.doc.Name if sc.doc.Name else "Unsaved"

    css = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: Arial, Helvetica, sans-serif; font-size: 10pt;
           max-width: 7.5in; margin: 0 auto; padding: 0.5in 0; color: #222; }
    @media print {
      body { max-width: none; padding: 0.4in; }
      .no-print { display: none; }
      @page { size: letter portrait; margin: 0.5in; }
    }
    h1 { font-size: 16pt; margin-bottom: 2pt; }
    .meta { color: #555; margin-bottom: 4pt; }
    hr.heavy { border: none; border-top: 2px solid #222; margin: 6pt 0 10pt; }
    hr.light { border: none; border-top: 1px solid #999; margin: 4pt 0 8pt; }
    .profile-hdr { font-size: 12pt; font-weight: bold; margin-top: 14pt; }
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
    .cut-len { display: inline-block; width: 90px; }
    .subtotal { font-style: italic; font-size: 9pt; margin: 4pt 0 2pt; }
    .totals { font-size: 10pt; margin-top: 4pt; }
    .totals div { margin: 2pt 0; }
    .print-btn { margin: 12pt 0; padding: 6pt 16pt; font-size: 10pt; cursor: pointer; }
    """

    parts = []
    parts.append(f"<h1>STICK NESTING - CUT RECIPE</h1>")
    parts.append(f'<div class="meta">Stock: {fmt_fraction(stock_length)}" &nbsp;|&nbsp; '
                 f'Kerf: {fmt_fraction(kerf)}"</div>')
    parts.append(f'<div class="meta">{doc_name} &nbsp;|&nbsp; {timestamp}</div>')
    parts.append('<hr class="heavy">')

    grand_sticks = 0
    grand_layouts = 0
    grand_cut_length = 0.0
    grand_waste = 0.0

    for profile in sorted(profile_results.keys()):
        pr = profile_results[profile]
        layouts = pr["layouts"]
        oversize = pr["oversize"]
        profile_sticks = sum(g["count"] for g in layouts)
        grand_sticks += profile_sticks
        grand_layouts += len(layouts)

        parts.append(f'<div class="profile-hdr">PROFILE: {profile}</div>')
        parts.append('<hr class="light">')

        if oversize:
            for length, loc in oversize:
                parts.append(f'<div class="oversize">OVERSIZE: '
                             f'{fmt_fraction(length)}" &nbsp; {loc}</div>')

        for g in layouts:
            parts.append(f'<div class="layout-hdr">Layout {g["id"]} &nbsp;(x{g["count"]}) '
                         f'&nbsp;|&nbsp; Remnant: {fmt_fraction(g["remnant"])}"</div>')

            parts.append(stick_diagram_html(g["cuts"], g["remnant"], stock_length, kerf))

            cut_groups = {}
            for length, location in g["cuts"]:
                key = (length, location)
                cut_groups[key] = cut_groups.get(key, 0) + 1

            parts.append('<div class="cut-list">')
            for (length, location), qty in sorted(cut_groups.items(), key=lambda x: -x[0][0]):
                qty_str = f" &nbsp;(x{qty})" if qty > 1 else ""
                parts.append(f'<div><span class="cut-len">{fmt_fraction(length)}"</span>'
                             f'{location}{qty_str}</div>')
                grand_cut_length += length * qty * g["count"]
            parts.append('</div>')

            grand_waste += g["remnant"] * g["count"]

        parts.append(f'<div class="subtotal">&gt;&gt; {profile}: '
                     f'{profile_sticks} stick(s), {len(layouts)} layout(s)</div>')
        parts.append('<hr class="light">')

    total_stock = grand_sticks * stock_length
    waste_pct = (grand_waste / total_stock * 100) if total_stock > 0 else 0

    parts.append('<hr class="heavy">')
    parts.append('<div class="totals"><strong>TOTALS</strong>')
    parts.append(f'<div>Sticks: {grand_sticks} &nbsp;|&nbsp; Layouts: {grand_layouts}</div>')
    parts.append(f'<div>Total cut length: '
                 f'{fmt_fraction(floor_to_sixteenth(grand_cut_length))}"</div>')
    parts.append(f'<div>Waste: {fmt_fraction(floor_to_sixteenth(grand_waste))}" '
                 f'&nbsp;({waste_pct:.1f}%)</div>')
    parts.append('</div>')
    parts.append('<hr class="heavy">')
    parts.append('<button class="print-btn no-print" onclick="window.print()">'
                 'Print / Save PDF</button>')

    body = "\n".join(parts)
    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Stick Nesting - {doc_name}</title>
<style>{css}</style>
</head><body>
{body}
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


def export_csv(profile_results, stock_length):
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
            layouts = profile_results[profile]["layouts"]
            for g in layouts:
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

def show_popup(lines, tsv_text, profile_results, stock_length, kerf):
    """Display report in a scrollable popup with copy, CSV, PDF export buttons."""
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
        export_html(profile_results, stock_length, kerf)

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
        export_csv(profile_results, stock_length)

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
        print("  nothing selected. aborting.")
        return

    profile_cuts, errors = collect_cuts(obj_ids)
    if not report_errors(errors, len(obj_ids)):
        return

    if not profile_cuts:
        print("  no valid profile/length data found.")
        return

    config = prompt_config()
    if config is None:
        return
    stock_length, kerf = config

    # run bin packing per profile
    profile_results = {}
    for profile, cuts in profile_cuts.items():
        bins, oversize = best_fit_decreasing(cuts, stock_length, kerf)
        layouts = group_identical_layouts(bins)
        profile_results[profile] = {
            "layouts": layouts,
            "oversize": oversize,
        }

    lines = build_report(profile_results, stock_length, kerf)
    tsv_text = build_tsv(profile_results, stock_length)

    for line in lines:
        print(line)

    show_popup(lines, tsv_text, profile_results, stock_length, kerf)


if __name__ == "__main__":
    main()
