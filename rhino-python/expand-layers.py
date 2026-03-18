import scriptcontext as sc

def expandlayers():

    for layer in sc.doc.Layers:
        layer.IsExpanded=True

expandlayers()