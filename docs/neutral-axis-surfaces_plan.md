# rewrite _build_nas_boundary: use ref_side face loops directly

## context

`_build_nas_boundary` in `rhino-python/unfold-to-2d.py` intersects the
NAP (neutral axis plane) with the original 3D brep to discover boundary
curves, then filters/joins/trims/snaps them (~265 lines). this is fragile:
windshield-panel fails with a 4.9" gap because the BrepPlane intersection
+ PP snapping pipeline has too many failure modes.

but we ALREADY HAVE the correct boundary: the ref_side face's own outer
loop and inner loops. for planar sheet metal, the face shape at the NAS
elevation is identical to the ref_side surface. we just need to translate
the loops to the offset plane and trim at PP lines.

## new algorithm for _build_nas_boundary

1. get the face's outer loop as a 3D curve (`face.OuterLoop.To3dCurve()`)
2. translate it to the NAP (offset by t/2 in the inward direction)
3. for each adjacent face at a bend:
   a. compute PP line (intersection of the two offset planes)
   b. build a trim plane perpendicular to the NAP containing the PP line
   c. split the outer loop at the trim plane
   d. keep the piece containing the face centroid (projected to NAP)
4. return the trimmed boundary

inner loops (holes) are already handled separately in
`construct_neutral_axis` by translating them to NAP. no changes needed.

## what this replaces

the entire `_build_nas_boundary` function (~265 lines) gets replaced
with ~50-60 lines of simpler code:
- no BrepPlane intersection
- no curve filtering/joining/loop picking
- no polyline snapping with material checks
- just: translate face loop -> trim at PP lines -> return

## edge cases

- face has no adjacent faces at bends: outer loop is untrimmed (full
  face boundary). correct for perimeter-only faces.
- multiple bends: trim at each PP line sequentially. order shouldn't
  matter since PP lines don't intersect within a face.
- curved trim edges on the face: preserved by the translation.
  the BrepPlane approach would lose them.
- non-perpendicular face edges: the translated loop might slightly
  differ from the true NAS boundary at a steep angle. for aluminum
  sheet metal (t=0.125", typical edge deviation < 0.001"), well
  within fabrication tolerance.

## verification

- windshield-panel: should produce 2 NAS faces with window cutouts,
  1 bend, complete flat pattern
- all other test parts: should produce identical or very similar
  results to current approach
- console-face (complex, 6 faces): verify NAS face count and angles
- laz-top (simple, 3 faces): verify basic operation
