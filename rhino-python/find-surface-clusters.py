#! python3
# -*- coding: utf-8 -*-
"""
find-surface-clusters.py
------------------------
groups loose surfaces by edge adjacency. useful for isolating one of N
overlapping copies of an imported part (STEP/IGES) where topology was lost
on import and you end up with thousands of disconnected surfaces, with the
same model represented multiple times at different orientations.

two surfaces are considered adjacent if they share an edge -- specifically:
their edge MIDPOINTS coincide within model tolerance AND their endpoints
match in either order. midpoint matching avoids the corner-touch false
positive that pure endpoint matching gives you for diagonal-quad neighbors.

then it BFS connected-components the adjacency graph. each component is
one "thing" -- e.g. one motor instance.

actions after clustering:
  - select largest cluster
  - select cluster by index (1 = largest)
  - isolate each cluster to its own layer (recommended: turn layers on/off
    to find the one you want, delete the rest)
  - delete all but the largest cluster

alias: find-clusters -> _-RunPythonScript "C:\\Projects\\scripts\\rhino-python\\find-surface-clusters.py"
"""

import Rhino
import scriptcontext as sc
import rhinoscriptsyntax as rs
from Rhino.Geometry import BoundingBox, RTree


# ---- adjacency ------------------------------------------------------------

def collect_edges(breps):
    """flatten all edges across all breps. returns list of tuples:
    (surf_idx, midpoint, start, end)."""
    edges = []
    for i, brep in enumerate(breps):
        if brep is None:
            continue
        for e in brep.Edges:
            try:
                mid = e.PointAt(e.Domain.Mid)
            except Exception:
                # degenerate edge -- fall back to start
                mid = e.PointAtStart
            edges.append((i, mid, e.PointAtStart, e.PointAtEnd))
    return edges


def build_adjacency(breps, tol):
    """{i: set(j)} of surfaces sharing at least one full edge within tol."""
    edges = collect_edges(breps)
    if not edges:
        return {i: set() for i in range(len(breps))}

    # RTree of edge midpoints, tagged by index into `edges`
    tree = RTree()
    pad = tol * 2.0
    for k, (_si, mid, _s, _e) in enumerate(edges):
        bb = BoundingBox(mid, mid)
        bb.Inflate(pad)
        tree.Insert(bb, k)

    adj = {i: set() for i in range(len(breps))}

    for k, (si, mid, s, e) in enumerate(edges):
        bb = BoundingBox(mid, mid)
        bb.Inflate(pad)
        hits = []

        def cb(sender, args, hits=hits):
            hits.append(args.Id)

        tree.Search(bb, cb)
        for h in hits:
            if h <= k:
                continue  # only process each unordered pair once
            sj, mid_j, s_j, e_j = edges[h]
            if sj == si:
                continue
            if mid.DistanceTo(mid_j) > tol:
                continue
            # verify endpoints match in either order
            d_ss = s.DistanceTo(s_j)
            d_ee = e.DistanceTo(e_j)
            if d_ss <= tol and d_ee <= tol:
                adj[si].add(sj)
                adj[sj].add(si)
                continue
            d_se = s.DistanceTo(e_j)
            d_es = e.DistanceTo(s_j)
            if d_se <= tol and d_es <= tol:
                adj[si].add(sj)
                adj[sj].add(si)

    return adj


def connected_components(adj):
    """list of sets of node indices, sorted largest-first."""
    visited = set()
    comps = []
    for start in adj:
        if start in visited:
            continue
        stack = [start]
        comp = set()
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            comp.add(n)
            for m in adj[n]:
                if m not in visited:
                    stack.append(m)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


# ---- actions --------------------------------------------------------------

def action_select(ids, comps, idx):
    rs.UnselectAllObjects()
    rs.SelectObjects([ids[i] for i in comps[idx]])


def action_isolate_to_layers(ids, comps):
    parent = "clusters"
    if not rs.IsLayer(parent):
        rs.AddLayer(parent)
    width = max(3, len(str(len(comps))))
    for i, c in enumerate(comps):
        name = "{}::cluster-{:0{w}d}-n{}".format(parent, i + 1, len(c), w=width)
        if not rs.IsLayer(name):
            rs.AddLayer(name)
        for idx in c:
            rs.ObjectLayer(ids[idx], name)


def action_delete_all_but_largest(ids, comps):
    keep = comps[0]
    kill = [ids[i] for i in range(len(ids)) if i not in keep]
    rs.DeleteObjects(kill)


def action_join_clusters(ids, breps, comps, tol):
    """JoinBreps within each cluster. replaces originals with the joined
    polysurface(s). single-surface clusters are left alone."""
    joined_count = 0
    kept_alone = 0
    for c in comps:
        if len(c) < 2:
            kept_alone += 1
            continue
        cluster_breps = [breps[i] for i in c]
        cluster_ids = [ids[i] for i in c]
        # inherit layer/color from the first member
        attrs = sc.doc.Objects.Find(cluster_ids[0]).Attributes.Duplicate()
        joined = Rhino.Geometry.Brep.JoinBreps(cluster_breps, tol)
        if joined is None or len(joined) == 0:
            print("  cluster of {} failed to join, leaving as-is".format(len(c)))
            continue
        for jb in joined:
            sc.doc.Objects.AddBrep(jb, attrs)
        rs.DeleteObjects(cluster_ids)
        joined_count += 1
    print("joined {} clusters, left {} singleton(s) untouched".format(
        joined_count, kept_alone))


# ---- main -----------------------------------------------------------------

def main():
    tol = sc.doc.ModelAbsoluteTolerance

    ids = rs.GetObjects(
        "select surfaces to cluster (esc to use all surfaces in doc)",
        rs.filter.surface | rs.filter.polysurface,
        preselect=True,
        select=False,
    )
    if not ids:
        # fall back to every surface/polysurface in the doc
        ids = []
        for obj in sc.doc.Objects:
            if obj.IsDeleted:
                continue
            if obj.ObjectType in (
                Rhino.DocObjects.ObjectType.Surface,
                Rhino.DocObjects.ObjectType.Brep,
            ):
                ids.append(obj.Id)
        if not ids:
            print("no surfaces found")
            return
        print("no selection -- using all {} surfaces in doc".format(len(ids)))

    breps = [rs.coercebrep(i) for i in ids]
    # drop any that didn't coerce, keeping ids in sync
    keep = [(i, b) for i, b in zip(ids, breps) if b is not None]
    if not keep:
        print("no breps could be coerced from selection")
        return
    ids = [i for i, _ in keep]
    breps = [b for _, b in keep]

    print("analyzing {} surfaces at tol {}...".format(len(breps), tol))

    adj = build_adjacency(breps, tol)
    comps = connected_components(adj)

    print("found {} clusters:".format(len(comps)))
    for i, c in enumerate(comps[:20]):
        print("  cluster {}: {} surfaces".format(i + 1, len(c)))
    if len(comps) > 20:
        remaining = sum(len(c) for c in comps[20:])
        print("  ... and {} smaller clusters ({} surfaces total)".format(
            len(comps) - 20, remaining))

    actions = [
        "select largest cluster",
        "isolate each cluster to its own layer",
        "delete all but largest cluster",
        "just join surfaces into clusters",
        "cancel",
    ]
    choice = rs.ListBox(actions, "what to do?", "find-surface-clusters")
    if not choice or choice == "cancel":
        return

    if choice == "select largest cluster":
        action_select(ids, comps, 0)
    elif choice == "isolate each cluster to its own layer":
        action_isolate_to_layers(ids, comps)
        print("moved each cluster to clusters::cluster-NNN-nM. "
              "toggle layers in the panel to find the one you want.")
    elif choice == "delete all but largest cluster":
        if rs.MessageBox(
            "delete {} surfaces (keeping {} in the largest cluster)?".format(
                len(ids) - len(comps[0]), len(comps[0])),
            4 | 32, "confirm",
        ) == 6:
            action_delete_all_but_largest(ids, comps)
    elif choice == "just join surfaces into clusters":
        action_join_clusters(ids, breps, comps, tol)

    sc.doc.Views.Redraw()


if __name__ == "__main__":
    main()
