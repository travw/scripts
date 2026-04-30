#! python3
# -*- coding: utf-8 -*-
"""
find-replace-names.py
---------------------
bulk find-and-replace on object names. operates on the current selection
(or prompts you to pick), swaps a substring in each object's name.

precipitating use: mirroring port-side parts to stbd, then renaming
"port" -> "stbd" across the new copies. also useful for L/R, fwd/aft,
v1/v2, etc.

scope: object names only (rs.ObjectName). does NOT touch user text,
layer names, or block definition names.

modes:
  case-insensitive, preserve case (default)
    matches port / Port / PORT, replacement adopts the matched casing
    so they become stbd / Stbd / STBD respectively.
  exact match
    literal substring replace. case-sensitive.

alias: frn -> _-RunPythonScript "C:\\Projects\\scripts\\rhino-python\\find-replace-names.py"
"""

import re
import rhinoscriptsyntax as rs
import scriptcontext as sc


STICKY_FIND = "frn.find"
STICKY_REPLACE = "frn.replace"
STICKY_MODE = "frn.mode"

MODE_PRESERVE = "case-insensitive, preserve case"
MODE_EXACT = "exact match"
MODES = [MODE_PRESERVE, MODE_EXACT]


def get_objects():
    """pre-selected > manual prompt."""
    pre = rs.SelectedObjects()
    if pre:
        print("  using {} pre-selected object(s)".format(len(pre)))
        return list(pre)

    picked = rs.GetObjects("select objects to rename", preselect=True)
    return list(picked) if picked else None


def case_preserving_replace(text, find, replace):
    """case-insensitive replace; replacement adopts the casing of each match."""
    pattern = re.compile(re.escape(find), re.IGNORECASE)

    def emit(m):
        s = m.group(0)
        if s.isupper():
            return replace.upper()
        if s.islower():
            return replace.lower()
        if s[:1].isupper() and s[1:].islower():
            return replace.capitalize()
        return replace

    return pattern.sub(emit, text)


def exact_replace(text, find, replace):
    return text.replace(find, replace)


def compute_renames(obj_ids, find, replace, mode):
    """returns (renames, skipped, unchanged) where:
         renames = [(id, old, new), ...]
         skipped = count of objects with no name
         unchanged = count where new == old
    """
    fn = case_preserving_replace if mode == MODE_PRESERVE else exact_replace
    renames = []
    skipped = 0
    unchanged = 0

    for obj_id in obj_ids:
        old = rs.ObjectName(obj_id)
        if not old:
            skipped += 1
            continue
        new = fn(old, find, replace)
        if new == old:
            unchanged += 1
            continue
        renames.append((obj_id, old, new))

    return renames, skipped, unchanged


def confirm_renames(renames, skipped, unchanged):
    """preview + Yes/No dialog. returns True if user confirms."""
    n = len(renames)
    sample = renames[:10]
    lines = [
        "rename {} object(s)?".format(n),
        "(skipped {} unnamed, {} unchanged)".format(skipped, unchanged),
        "",
    ]
    lines.extend("  {} -> {}".format(old, new) for _, old, new in sample)
    if n > 10:
        lines.append("  ...and {} more".format(n - 10))

    YES_NO = 4
    QUESTION = 32
    rv = rs.MessageBox("\n".join(lines), YES_NO | QUESTION, "find-replace-names")
    return rv == 6  # 6 = Yes


def main():
    targets = get_objects()
    if not targets:
        print("no objects selected; aborting.")
        return

    default_find = sc.sticky.get(STICKY_FIND, "")
    default_replace = sc.sticky.get(STICKY_REPLACE, "")
    default_mode = sc.sticky.get(STICKY_MODE, MODE_PRESERVE)
    if default_mode not in MODES:
        default_mode = MODE_PRESERVE

    find = rs.StringBox(
        message="find this substring in object names:",
        default_value=default_find,
        title="find-replace-names - find",
    )
    if find is None:
        return
    if find == "":
        print("find string is empty; aborting.")
        return

    replace = rs.StringBox(
        message="replace with (empty string = delete substring):",
        default_value=default_replace,
        title="find-replace-names - replace",
    )
    if replace is None:
        return

    mode = rs.ListBox(
        MODES,
        message="match mode:",
        title="find-replace-names - mode",
        default=default_mode,
    )
    if mode is None:
        return

    renames, skipped, unchanged = compute_renames(targets, find, replace, mode)

    if not renames:
        rs.MessageBox(
            "no object names matched '{}' - nothing to rename.\n"
            "(checked {} object(s), {} unnamed, {} unchanged)".format(
                find, len(targets), skipped, unchanged),
            0 | 64,  # OK | info
            "find-replace-names",
        )
        return

    if not confirm_renames(renames, skipped, unchanged):
        print("aborted by user.")
        return

    for obj_id, _, new in renames:
        rs.ObjectName(obj_id, new)

    print("renamed {} object(s) ({} skipped, {} unchanged)".format(
        len(renames), skipped, unchanged))

    sc.sticky[STICKY_FIND] = find
    sc.sticky[STICKY_REPLACE] = replace
    sc.sticky[STICKY_MODE] = mode


if __name__ == "__main__":
    main()
