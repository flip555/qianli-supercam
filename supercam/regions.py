"""Regions of interest on the thermal frame, and their statistics.

Coordinates are in thermal pixels (0..W-1, 0..H-1) - the same grid as the 16-bit
values - so the arithmetic happens on the relay, where the real data is. The page
only ever sees a rendered image and cannot measure anything itself.
"""

MAX_REGIONS = 32


def _inside(region, x, y):
    kind = region.get("kind")
    pts = region.get("pts") or []
    if kind == "point":
        return bool(pts) and int(pts[0][0]) == x and int(pts[0][1]) == y
    if kind == "rect":
        if len(pts) < 2:
            return False
        (x0, y0), (x1, y1) = pts[0], pts[1]
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        return x0 <= x <= x1 and y0 <= y <= y1
    if kind == "poly":
        if len(pts) < 3:
            return False
        inside = False
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = int(pts[i][0]), int(pts[i][1])
            xj, yj = int(pts[j][0]), int(pts[j][1])
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / float(yj - yi) + xi:
                inside = not inside
            j = i
        return inside
    return False


def _bbox(region, W, H):
    pts = region.get("pts") or []
    if not pts:
        return None
    xs = [int(p[0]) for p in pts]
    ys = [int(p[1]) for p in pts]
    return (max(0, min(xs)), max(0, min(ys)), min(W - 1, max(xs)), min(H - 1, max(ys)))


def stats_for(vals, W, H, regions, scale, offset, ref_id=None):
    """min/max/average per region, plus the average's difference from a reference."""
    results = []
    for region in regions or []:
        box = _bbox(region, W, H)
        if not box:
            continue
        x0, y0, x1, y1 = box
        picked = []
        for y in range(y0, y1 + 1):
            row = y * W
            for x in range(x0, x1 + 1):
                if _inside(region, x, y):
                    picked.append(vals[row + x])
        if not picked:
            continue
        results.append({
            "id": region.get("id"),
            "kind": region.get("kind"),
            "name": region.get("name") or "",
            "n": len(picked),
            "min_c": round(min(picked) * scale + offset, 2),
            "max_c": round(max(picked) * scale + offset, 2),
            "avg_c": round(sum(picked) / len(picked) * scale + offset, 2),
        })
    if ref_id:
        ref = next((r for r in results if r["id"] == ref_id), None)
        if ref:
            for r in results:
                r["delta_c"] = round(r["avg_c"] - ref["avg_c"], 2)
    return results
