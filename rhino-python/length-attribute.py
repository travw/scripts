# -*- coding: utf-8 -*-

import rhinoscriptsyntax as rs
import Rhino


def get_lengths_grouped_by_parent():
    parent_lengths = {}

    go = Rhino.Input.Custom.GetObject()
    go.SetCommandPrompt("Select curves or sub-object edges")
    go.GeometryFilter = Rhino.DocObjects.ObjectType.Curve | Rhino.DocObjects.ObjectType.EdgeFilter
    go.SubObjectSelect = True
    go.GetMultiple(1, 0)

    if go.CommandResult() != Rhino.Commands.Result.Success:
        return None

    for i in range(go.ObjectCount):
        obj_ref = go.Object(i)
        parent_id = obj_ref.ObjectId

        length = 0.0

        # Sub-selected edge
        edge = obj_ref.Edge()
        if edge:
            length = edge.GetLength()

        # Regular curve
        else:
            curve = obj_ref.Curve()
            if curve:
                length = curve.GetLength()

        if length > 0:
            if parent_id in parent_lengths:
                parent_lengths[parent_id] += length
            else:
                parent_lengths[parent_id] = length

    return parent_lengths


def assign_lengths(parent_lengths):

    # Check if any object already has "length"
    existing_found = False

    for obj_id in parent_lengths.keys():
        if rs.GetUserText(obj_id, "length"):
            existing_found = True
            break

    if existing_found:
        result = rs.MessageBox(
            "One or more objects already contain a 'length' attribute.\n\n"
            "Override existing values?",
            4 | 32
        )

        if result != 6:
            print("Command cancelled.")
            return False

    # Assign per-object length
    for obj_id, length_value in parent_lengths.items():
        rs.SetUserText(obj_id, "length", str(round(length_value, 4)))

    return True


def main():

    parent_lengths = get_lengths_grouped_by_parent()

    if parent_lengths is None:
        print("No geometry selected.")
        return

    if not parent_lengths:
        print("No valid edges or curves selected.")
        return

    success = assign_lengths(parent_lengths)

    if success:
        print("Length assigned per object under key 'length'.")


if __name__ == "__main__":
    main()
