import scriptcontext as sc

def collapselayers():

    for layer in sc.doc.Layers:
        layer.IsExpanded=False

collapselayers()