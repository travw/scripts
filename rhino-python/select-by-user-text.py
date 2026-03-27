#! python3
"""Select objects by user text attribute — SelName-style popup.

Semi-modal Eto dialog: pick a key from dropdown, then click values
in the listbox to live-highlight matching objects. Viewport stays
interactive (zoom/orbit/pan) while dialog is open.

alias: sut -> _-RunPythonScript "C:\Sites\scripts\rhino-python\select-by-user-text.py"
"""

import Rhino
import Rhino.UI
import rhinoscriptsyntax as rs
import scriptcontext as sc
import Eto.Forms as forms
import Eto.Drawing as drawing


def make_list_item(text, key):
    item = forms.ListItem()
    item.Text = text
    item.Key = key
    return item


class SelectByUserTextDialog(forms.Dialog[bool]):

    def __init__(self, key_val_map):
        super().__init__()
        self.key_val_map = key_val_map
        self.Title = "Select by User Text"
        self.Padding = drawing.Padding(8)
        self.Resizable = True
        self.ClientSize = drawing.Size(360, 480)
        self.result_key = None
        self.result_val = None
        self.original_selection = rs.SelectedObjects() or []

        # key dropdown
        self.key_dropdown = forms.DropDown()
        self.key_dropdown.Height = 28
        sorted_keys = sorted(key_val_map.keys())
        for k in sorted_keys:
            n = sum(len(v) for v in key_val_map[k].values())
            self.key_dropdown.Items.Add(make_list_item(f"{k}  ({n} objects)", k))
        self.key_dropdown.SelectedIndexChanged += self.on_key_changed

        # value listbox
        self.val_listbox = forms.ListBox()
        self.val_listbox.Height = 340
        self.val_listbox.SelectedIndexChanged += self.on_value_changed

        # buttons
        ok_btn = forms.Button()
        ok_btn.Text = "OK"
        ok_btn.Click += self.on_ok
        cancel_btn = forms.Button()
        cancel_btn.Text = "Cancel"
        cancel_btn.Click += self.on_cancel
        self.DefaultButton = ok_btn
        self.AbortButton = cancel_btn

        btn_layout = forms.StackLayout()
        btn_layout.Orientation = forms.Orientation.Horizontal
        btn_layout.Spacing = 8
        spacer = forms.StackLayoutItem(None, True)
        ok_item = forms.StackLayoutItem(ok_btn, False)
        cancel_item = forms.StackLayoutItem(cancel_btn, False)
        btn_layout.Items.Add(spacer)
        btn_layout.Items.Add(ok_item)
        btn_layout.Items.Add(cancel_item)

        layout = forms.DynamicLayout()
        layout.DefaultSpacing = drawing.Size(4, 6)
        layout.Padding = drawing.Padding(4)
        key_label = forms.Label()
        key_label.Text = "Attribute key:"
        layout.AddRow(key_label)
        layout.AddRow(self.key_dropdown)
        val_label = forms.Label()
        val_label.Text = "Values:"
        layout.AddRow(val_label)
        layout.AddRow(self.val_listbox)
        layout.AddRow(None)
        layout.AddRow(btn_layout)
        self.Content = layout

        # populate values for first key
        if self.key_dropdown.Items.Count > 0:
            self.key_dropdown.SelectedIndex = 0

    def on_key_changed(self, sender, e):
        self.val_listbox.Items.Clear()
        if self.key_dropdown.SelectedIndex < 0:
            return
        key = self.key_dropdown.Items[self.key_dropdown.SelectedIndex].Key
        val_map = self.key_val_map[key]
        for v in sorted(val_map.keys()):
            self.val_listbox.Items.Add(make_list_item(f"{v}  ({len(val_map[v])})", v))
        # clear highlight when switching keys
        rs.UnselectAllObjects()
        sc.doc.Views.Redraw()

    def on_value_changed(self, sender, e):
        if self.key_dropdown.SelectedIndex < 0 or self.val_listbox.SelectedIndex < 0:
            return
        key = self.key_dropdown.Items[self.key_dropdown.SelectedIndex].Key
        val = self.val_listbox.Items[self.val_listbox.SelectedIndex].Key
        matches = self.key_val_map[key][val]
        rs.UnselectAllObjects()
        rs.SelectObjects(matches)
        sc.doc.Views.Redraw()

    def on_ok(self, sender, e):
        if self.key_dropdown.SelectedIndex >= 0 and self.val_listbox.SelectedIndex >= 0:
            self.result_key = self.key_dropdown.Items[self.key_dropdown.SelectedIndex].Key
            self.result_val = self.val_listbox.Items[self.val_listbox.SelectedIndex].Key
        self.Close(True)

    def on_cancel(self, sender, e):
        # restore original selection
        rs.UnselectAllObjects()
        if self.original_selection:
            rs.SelectObjects(self.original_selection)
        sc.doc.Views.Redraw()
        self.Close(False)


def select_by_user_text():
    all_objects = rs.AllObjects()
    if not all_objects:
        print("No objects in document.")
        return

    # collect: key -> {value -> [obj_ids]}
    key_val_map = {}
    for obj in all_objects:
        keys = rs.GetUserText(obj)
        if not keys:
            continue
        for key in keys:
            val = rs.GetUserText(obj, key)
            if key not in key_val_map:
                key_val_map[key] = {}
            if val not in key_val_map[key]:
                key_val_map[key][val] = []
            key_val_map[key][val].append(obj)

    if not key_val_map:
        print("No objects have user text attributes.")
        return

    dialog = SelectByUserTextDialog(key_val_map)
    Rhino.UI.EtoExtensions.ShowSemiModal(dialog, sc.doc, Rhino.UI.RhinoEtoApp.MainWindow)

    if dialog.result_key and dialog.result_val:
        matches = key_val_map[dialog.result_key][dialog.result_val]
        rs.UnselectAllObjects()
        rs.SelectObjects(matches)
        print(f"Selected {len(matches)} object(s) where {dialog.result_key} = {dialog.result_val}.")


if __name__ == "__main__":
    select_by_user_text()
