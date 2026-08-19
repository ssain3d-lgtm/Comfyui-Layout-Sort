"""Pure-Python layout engine for ComfyUI workflows.

Takes a serialized workflow (the same JSON the frontend saves / embeds in
EXTRA_PNGINFO) and computes new node positions using a layered
(Sugiyama-style) algorithm:

  1. Normalize nodes / links (both array and object link formats).
  2. Assign each linked item to a layer via longest-path topological
     layering, so data flows strictly in one direction.
  3. Order items inside each layer with barycenter sweeps to reduce
     link crossings.
  4. Emit coordinates: layers become columns (or rows), centered on a
     shared axis. Items with no links (notes, disconnected leftovers)
     are shelf-packed below the main flow.

Group handling (option "group_mode"):
  * "cluster" (default) — recursive compound layout. Every group (nested
    ones included) becomes a cluster: its direct member nodes and child
    clusters are laid out with the same layered algorithm, then each parent
    container arranges those blocks. Sibling frames can therefore never
    overlap, at any nesting depth. Each link influences exactly one level:
    the lowest common ancestor of its endpoints.
  * "refit" — single-level layout that ignores groups, then re-fits each
    group frame around the nodes it contained before the sort.

This module has no ComfyUI imports so it can be run and tested standalone.
All internal math happens in "visual" coordinates (top-left of what is
drawn, title bar included); conversion to LiteGraph `pos` semantics
(top of the node body) happens only in compute_layout's final step.
"""

import math

# LiteGraph draws the title bar above node.pos, so a node's visual top is
# pos[1] - TITLE_HEIGHT and its visual height is size[1] + TITLE_HEIGHT.
TITLE_HEIGHT = 30.0

# LiteGraph renders collapsed nodes at roughly title width, not full width.
COLLAPSED_MAX_WIDTH = 160.0

DEFAULT_OPTIONS = {
    "direction": "left_to_right",  # or "top_to_bottom"
    "h_spacing": 80,   # gap between layers (columns in left_to_right)
    "v_spacing": 40,   # gap between items inside a layer
    "group_mode": "cluster",  # or "refit"
    "barycenter_sweeps": 4,
    # Node types whose links are ignored for layout topology. The sorter
    # node itself belongs here: a trigger wired into it is execution-order
    # plumbing and must not warp the layout it produces.
    "detach_types": ("LayoutSort",),
    # A layer taller (in left_to_right) than this is split across several
    # adjacent columns. Same-layer items never link to each other, so the
    # split cannot create backward links — it just stops huge graphs from
    # becoming one enormously tall column.
    "wrap_breadth": 2600,
    # "center": columns center on a shared axis, which shortens diagonal
    # links when column heights differ a lot (best default for big
    # graphs); "top": columns share a top edge and group interiors pack
    # from their top-left corner.
    "align": "center",
    # Treat Set/Get wireless pairs (KJNodes SetNode/GetNode and lookalikes)
    # as layout-only virtual links so the logical flow they carry shapes
    # the layout even though no cable exists in the JSON.
    "link_set_get": True,
    # Round the final coordinates to this grid. Node sizes are never
    # touched — the sorter only moves things.
    "snap_grid": 10,
    # Target canvas proportions: "auto" (natural), "square" (1:1),
    # "wide" (2:1) or "tall" (1:2). Applies to the whole layout AND to
    # every group interior in cluster/inner mode.
    "shape": "auto",
}

GROUP_SIDE_PADDING = 24.0
GROUP_TITLE_PADDING = 40.0


def _finite(value, default=0.0):
    """float() that never lets NaN/inf (a known ComfyUI corruption mode)
    poison the layout — one NaN would otherwise spread through min()/sums
    into every coordinate and produce JSON the browser cannot parse."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _xy(value, default=(0.0, 0.0)):
    """Read an [x, y]-like value that may be a list or a {"0":..,"1":..} dict."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [_finite(value[0], default[0]), _finite(value[1], default[1])]
    if isinstance(value, dict):
        return [_finite(value.get("0", default[0]), default[0]),
                _finite(value.get("1", default[1]), default[1])]
    return [float(default[0]), float(default[1])]


def _normalize_nodes(workflow):
    """Return {id: {x, y, w, h}} visual rects (title bar included)."""
    nodes = {}
    for raw in workflow.get("nodes") or []:
        try:
            node_id = raw["id"]
        except (KeyError, TypeError):
            continue
        pos = _xy(raw.get("pos"))
        size = _xy(raw.get("size"), (140.0, 60.0))
        flags = raw.get("flags") or {}
        collapsed = bool(flags.get("collapsed"))
        width = max(size[0], 1.0)
        if collapsed:
            width = min(width, COLLAPSED_MAX_WIDTH)
        nodes[node_id] = {
            "x": pos[0],
            "y": pos[1] - TITLE_HEIGHT,
            "w": width,
            "h": (0.0 if collapsed else max(size[1], 1.0)) + TITLE_HEIGHT,
            "type": str(raw.get("type") or ""),
        }
    return nodes


def _normalize_links(workflow, nodes, detach_types=()):
    """Return [(origin_id, target_id, target_slot)] for every valid link.
    Endpoints match by string form too, so int/str id mixes still connect.
    Links touching a node whose type is in `detach_types` are dropped, so
    those nodes lay out as islands instead of warping the flow."""
    by_str = {str(nid): nid for nid in nodes}
    detach = set(detach_types or ())
    edges = []
    for raw in workflow.get("links") or []:
        if isinstance(raw, (list, tuple)) and len(raw) >= 5:
            origin, target, slot = raw[1], raw[3], raw[4]
        elif isinstance(raw, dict):
            origin = raw.get("origin_id")
            target = raw.get("target_id")
            slot = raw.get("target_slot", 0)
        else:
            continue
        origin = by_str.get(str(origin))
        target = by_str.get(str(target))
        if origin is None or target is None or origin == target:
            continue
        if nodes[origin]["type"] in detach or nodes[target]["type"] in detach:
            continue
        try:
            edges.append((origin, target, int(slot or 0)))
        except (TypeError, ValueError):
            edges.append((origin, target, 0))
    return edges


def _wireless_edges(workflow, nodes):
    """Layout-only virtual edges for Set/Get wireless pairs.

    KJNodes' SetNode stores a value under a key and GetNode retrieves it;
    the JSON has no link between them, so without this the layout sees
    every GetNode as a source and every SetNode as a sink, scrambling the
    macro order (outputs drifting ahead of the loops that feed them). The
    key lives in widgets_values[0]; the Set_/Get_ title prefix is the
    fallback. No cable is created — these edges only inform the layout."""
    by_str = {str(nid): nid for nid in nodes}
    setters, getters = {}, {}
    for raw in workflow.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_type = str(raw.get("type") or "")
        is_set = node_type.endswith("SetNode") or node_type == "easy setNode"
        is_get = node_type.endswith("GetNode") or node_type == "easy getNode"
        if not (is_set or is_get):
            continue
        key = None
        widgets = raw.get("widgets_values")
        if isinstance(widgets, list) and widgets and isinstance(widgets[0], str):
            key = widgets[0].strip()
        if not key:
            title = str(raw.get("title") or "")
            for prefix in ("Set_", "Get_", "Set ", "Get "):
                if title.startswith(prefix):
                    key = title[len(prefix):].strip()
                    break
        nid = by_str.get(str(raw.get("id")))
        if not key or nid is None:
            continue
        (setters if is_set else getters).setdefault(key, []).append(nid)

    edges = []
    for key, sources in setters.items():
        for target in getters.get(key, []):
            for source in sources:
                if source != target:
                    edges.append((source, target, 0))
    return edges


def _normalize_groups(workflow):
    groups = []
    for index, raw in enumerate(workflow.get("groups") or []):
        if not isinstance(raw, dict):
            continue
        bounding = raw.get("bounding")
        if isinstance(bounding, dict):  # same tolerance as node pos/size
            bounding = [bounding.get(str(i)) for i in range(4)]
        if not isinstance(bounding, (list, tuple)) or len(bounding) < 4:
            continue
        try:
            x, y, w, h = (float(v) for v in bounding[:4])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            continue
        groups.append({"index": index, "x": x, "y": y, "w": max(w, 1.0), "h": max(h, 1.0)})
    return groups


def _group_contains(group, cx, cy):
    return (group["x"] <= cx <= group["x"] + group["w"]
            and group["y"] <= cy <= group["y"] + group["h"])


def _group_area(group):
    return group["w"] * group["h"]


def _center(item):
    return item["x"] + item["w"] / 2.0, item["y"] + item["h"] / 2.0


# ---------------------------------------------------------------------------
# Generic layered engine — works on any items ({id: rect}) + edges, so it is
# used both for nodes inside a group and for the coarse cluster graph.
# ---------------------------------------------------------------------------

def _feedback_edges(pool, preds, succs):
    """Greedy Eades-Lin-Smyth sequencing: order the nodes so that few
    edges point backward; those backward edges form the feedback set that
    layering ignores. In a graph with cycles (loop constructs, or cluster
    graphs of interlinked groups) only the true loop-back edges end up
    drawn right-to-left, instead of whole cycles collapsing into one
    layer."""
    remaining = set(pool)
    left, right = [], []
    while remaining:
        changed = True
        while changed:
            changed = False
            for iid in pool:  # pool is a list: deterministic order
                if iid in remaining and not (succs[iid] & remaining):
                    right.append(iid)
                    remaining.discard(iid)
                    changed = True
            for iid in pool:
                if iid in remaining and not (preds[iid] & remaining):
                    left.append(iid)
                    remaining.discard(iid)
                    changed = True
        if remaining:
            best = max(
                (iid for iid in pool if iid in remaining),
                key=lambda v: len(succs[v] & remaining) - len(preds[v] & remaining),
            )
            left.append(best)
            remaining.discard(best)
    position = {iid: i for i, iid in enumerate(left + list(reversed(right)))}
    return {(o, t) for o in position for t in succs[o]
            if t in position and position[o] >= position[t]}


def _assign_layers(items, edges):
    """Longest-path layering; cycles are broken via a greedy feedback set."""
    preds = {iid: set() for iid in items}
    succs = {iid: set() for iid in items}
    for origin, target, _slot in edges:
        preds[target].add(origin)
        succs[origin].add(target)

    linked = [iid for iid in items if preds[iid] or succs[iid]]
    linked_set = set(linked)

    def layering(feedback):
        eff_preds = {n: {p for p in preds[n] if (p, n) not in feedback}
                     for n in linked}
        eff_succs = {n: {s for s in succs[n] if (n, s) not in feedback}
                     for n in linked}
        indegree = {n: len(eff_preds[n]) for n in linked}
        frontier = [n for n in linked if indegree[n] == 0]
        layer = {n: 0 for n in frontier}
        visited = 0
        while frontier:
            iid = frontier.pop()
            visited += 1
            for nxt in eff_succs[iid]:
                layer[nxt] = max(layer.get(nxt, 0), layer[iid] + 1)
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    frontier.append(nxt)
        return layer, visited, eff_preds, eff_succs

    layer, visited, eff_preds, eff_succs = layering(frozenset())
    if visited < len(linked):
        feedback = _feedback_edges(linked, preds, succs)
        layer, visited, eff_preds, eff_succs = layering(feedback)

    # Pull pure sources next to their first consumer so loaders don't all
    # pile up in column 0 regardless of where they are used.
    for iid in linked:
        if not eff_preds[iid] and eff_succs[iid]:
            min_succ = min(layer[s] for s in eff_succs[iid])
            layer[iid] = max(layer[iid], min_succ - 1)

    islands = [iid for iid in items if iid not in linked_set]
    return layer, preds, succs, islands


def _order_layers(items, layer, preds, succs, edges, sweeps):
    """Barycenter ordering to reduce crossings; stable on original y.

    The upstream (succ-driven) sweep weighs each anchor by the consumer's
    input slot, so the several producers feeding one node stack in the
    same top-to-bottom order as its input sockets — straighter links
    around samplers and conditioning."""
    layers = {}
    for iid, depth in layer.items():
        layers.setdefault(depth, []).append(iid)
    depths = sorted(layers)
    for depth in depths:
        layers[depth].sort(key=lambda iid: (items[iid]["y"], items[iid]["x"]))

    pred_anchors = {iid: [(p, 0.0) for p in preds[iid]] for iid in layer}
    succ_anchors = {}
    for origin, target, slot in edges:
        if origin in layer and target in layer:
            succ_anchors.setdefault(origin, []).append((target, slot / 32.0))

    def sweep(order_by, anchors_map):
        for depth in order_by:
            index = {}
            for other_depth in depths:
                if other_depth != depth:
                    for i, iid in enumerate(layers[other_depth]):
                        index[iid] = i
            current = {iid: i for i, iid in enumerate(layers[depth])}

            def key(iid):
                anchors = [index[n] + weight
                           for n, weight in anchors_map.get(iid, ())
                           if n in index]
                if not anchors:
                    return (float(current[iid]), items[iid]["y"])
                return (sum(anchors) / len(anchors), items[iid]["y"])

            layers[depth].sort(key=key)

    for _ in range(max(0, sweeps)):
        sweep(depths, pred_anchors)                  # top-down: follow inputs
        sweep(list(reversed(depths)), succ_anchors)  # bottom-up: follow outputs
    return [layers[d] for d in depths]


def _wrap_layers(items, ordered_layers, direction, v_spacing, wrap_breadth):
    """Split overly tall (or, in top_to_bottom, overly wide) layers into
    several adjacent columns. Items within one layer never link to each
    other, so a split can never create a backward link — it only keeps
    huge graphs from degenerating into one enormous column."""
    if not wrap_breadth or wrap_breadth <= 0:
        return ordered_layers
    dimension = "w" if direction == "top_to_bottom" else "h"
    wrapped = []
    for column in ordered_layers:
        chunk, breadth = [], 0.0
        for iid in column:
            extent = items[iid][dimension] + (v_spacing if chunk else 0.0)
            if chunk and breadth + extent > wrap_breadth:
                wrapped.append(chunk)
                chunk, breadth = [], 0.0
                extent = items[iid][dimension]
            chunk.append(iid)
            breadth += extent
        if chunk:
            wrapped.append(chunk)
    return wrapped


def _place_layers(items, ordered_layers, direction, h_spacing, v_spacing,
                  align="top"):
    """Assign visual top-left coordinates layer by layer.

    align="top": layers share a top (left_to_right) or left (top_to_bottom)
    edge; align="center": layers center on a shared axis."""
    positions = {}
    if not ordered_layers:
        return positions, (0.0, 0.0)

    if direction == "top_to_bottom":
        thickness = [max(items[i]["h"] for i in row) for row in ordered_layers]
        breadth = [
            sum(items[i]["w"] for i in row) + v_spacing * (len(row) - 1)
            for row in ordered_layers
        ]
        max_breadth = max(breadth)
        main = 0.0
        for row, thick, wide in zip(ordered_layers, thickness, breadth):
            cross = 0.0 if align == "top" else (max_breadth - wide) / 2.0
            for iid in row:
                positions[iid] = [cross, main]
                cross += items[iid]["w"] + v_spacing
            main += thick + h_spacing
        return positions, (max_breadth, main - h_spacing)

    # left_to_right
    thickness = [max(items[i]["w"] for i in col) for col in ordered_layers]
    breadth = [
        sum(items[i]["h"] for i in col) + v_spacing * (len(col) - 1)
        for col in ordered_layers
    ]
    max_breadth = max(breadth)
    main = 0.0
    for col, thick, tall in zip(ordered_layers, thickness, breadth):
        cross = 0.0 if align == "top" else (max_breadth - tall) / 2.0
        for iid in col:
            positions[iid] = [main + (thick - items[iid]["w"]) / 2.0, cross]
            cross += items[iid]["h"] + v_spacing
        main += thick + h_spacing
    return positions, (main - h_spacing, max_breadth)


def _shelf_pack(items, ordered, row_limit, v_spacing):
    """Row-based shelf packing anchored at (0, 0): (positions, w, h)."""
    positions = {}
    x = y = row_height = width = 0.0
    for iid in ordered:
        item = items[iid]
        if x > 0 and (x + item["w"]) > row_limit:
            x = 0.0
            y += row_height + v_spacing
            row_height = 0.0
        positions[iid] = [x, y]
        x += item["w"] + v_spacing
        width = max(width, x - v_spacing)
        row_height = max(row_height, item["h"])
    return positions, width, y + row_height


def _place_islands(items, islands, main_extent, h_spacing, v_spacing,
                   shape_ratio=None):
    """Shelf-pack unlinked items (notes etc.) next to the main flow.

    Without a shape request the shelf always goes underneath. With one,
    the shelf is packed both ways — underneath and to the right — and
    whichever aggregate lands closer to the target W:H wins (a wide
    target with a big island block below would otherwise always end up
    tall)."""
    if not islands:
        return {}
    ordered = sorted(islands, key=lambda iid: (items[iid]["y"], items[iid]["x"]))
    main_w, main_h = main_extent
    gap = max(h_spacing, v_spacing) * 2.0
    total_area = sum(items[iid]["w"] * items[iid]["h"] for iid in islands)
    widest = max(items[iid]["w"] for iid in islands)

    def below(row_limit):
        pos, w, h = _shelf_pack(items, ordered, row_limit, v_spacing)
        offset = main_h + gap if main_h > 0 else 0.0
        for p in pos.values():
            p[1] += offset
        return pos, max(main_w, w), (offset + h)

    if not shape_ratio:
        return below(max(main_w, 1200.0, 1.25 * math.sqrt(total_area)))[0]

    cand_below = below(max(main_w, math.sqrt(total_area * shape_ratio),
                           widest))
    pos_r, w_r, h_r = _shelf_pack(
        items, ordered,
        max(math.sqrt(total_area * shape_ratio), widest), v_spacing)
    offset = main_w + gap if main_w > 0 else 0.0
    for p in pos_r.values():
        p[0] += offset
    cand_right = (pos_r, offset + w_r, max(main_h, h_r))

    def badness(candidate):
        _pos, width, height = candidate
        return abs(math.log(max(width, 1.0) / max(height, 1.0)
                            / shape_ratio))

    return min(cand_below, cand_right, key=badness)[0]


# Target width:height ratios for the "shape" option. The engine reshapes
# toward the ratio with whichever mechanism fits: graphs taller than the
# target get their breadth capped (layer wrapping), graphs longer than
# the target get their layer sequence folded into serpentine bands the
# way a human folds a long pipeline. Only one mechanism ever applies, so
# they cannot compound.
SHAPE_RATIOS = {"square": 1.0, "wide": 2.0, "tall": 0.5}
SHAPE_PACK_FACTOR = 1.7   # content area -> canvas area (spacing overhead)
SHAPE_SLACK = 1.25        # don't fold for less than 25% overshoot


def _shape_targets(items, linked_ids, shape_ratio, direction):
    """(breadth_cap, flow_cap) canvas targets for the requested ratio,
    computed over the LINKED content only — islands are shelf-packed
    separately (with their own ratio-aware row limit) and would otherwise
    inflate the targets of the flow they are not part of."""
    area = sum(items[i]["w"] * items[i]["h"]
               for i in linked_ids) * SHAPE_PACK_FACTOR
    if area <= 0:
        return float("inf"), float("inf")
    width = math.sqrt(area * shape_ratio)
    height = math.sqrt(area / shape_ratio)
    if direction == "top_to_bottom":
        return width, height   # breadth is horizontal, flow is vertical
    return height, width


def _partition_bands(items, ordered_layers, direction, h_spacing, v_spacing,
                     band_gap, shape_ratio, flow_cap=None):
    """Fold the layer sequence into k serpentine bands.

    With an explicit flow_cap (fit-into-a-drawn-zone), bands simply fill
    up to that width. Otherwise k comes from solving flow'/breadth' =
    target for the band count with the inter-band gap included: with
    flow total F, mean layer breadth b and gap g, breadth after k bands
    is k*b + (k-1)*g and flow is F/k, so (b+g)*k^2 - g*k - F/q = 0
    (q = W:H for left_to_right, its inverse for top_to_bottom)."""
    if len(ordered_layers) < 2:
        return [ordered_layers]
    flow_dim = "h" if direction == "top_to_bottom" else "w"
    breadth_dim = "w" if direction == "top_to_bottom" else "h"
    extents = [max(items[i][flow_dim] for i in col) for col in ordered_layers]
    flow_total = sum(extents) + h_spacing * (len(extents) - 1)
    if flow_cap is not None:
        if flow_total <= flow_cap * 1.05:
            return [ordered_layers]
    else:
        breadths = [
            sum(items[i][breadth_dim] for i in col)
            + v_spacing * (len(col) - 1)
            for col in ordered_layers
        ]
        mean_breadth = sum(breadths) / len(breadths)
        q = shape_ratio if direction != "top_to_bottom" else 1.0 / shape_ratio
        unit = mean_breadth + band_gap
        k = (band_gap + math.sqrt(band_gap * band_gap
                                  + 4.0 * unit * flow_total / q)) / (2.0 * unit)
        k = max(1, int(round(k)))
        if k <= 1:
            return [ordered_layers]
        flow_cap = flow_total / k * 1.02  # slack so rounding still fits k
    bands, current, used = [], [], 0.0
    for column, extent in zip(ordered_layers, extents):
        step = extent + (h_spacing if current else 0.0)
        if current and used + step > flow_cap:
            bands.append(current)
            current, used = [], 0.0
            step = extent
        current.append(column)
        used += step
    if current:
        bands.append(current)
    return bands


def _layered_layout(items, edges, direction, h_spacing, v_spacing, sweeps,
                    wrap_breadth=0, align="top", shape_ratio=None,
                    zone_size=None):
    """Run the full layered pipeline. Returns (positions, (width, height))
    with positions normalized to a tight box anchored at (0, 0).

    `zone_size` (w, h) fits the layout into a user-drawn box: its actual
    dimensions become the wrap/band caps instead of area estimates."""
    if not items:
        return {}, (0.0, 0.0)
    layer, preds, succs, islands = _assign_layers(items, edges)
    ordered_layers = _order_layers(items, layer, preds, succs, edges, sweeps)

    bands = None
    band_gap = 2.0 * max(h_spacing, v_spacing)
    if (shape_ratio or zone_size) and ordered_layers:
        if zone_size:
            zone_w, zone_h = zone_size
            if direction == "top_to_bottom":
                breadth_cap, flow_cap = zone_w, zone_h
            else:
                breadth_cap, flow_cap = zone_h, zone_w
        else:
            linked_ids = [iid for col in ordered_layers for iid in col]
            breadth_cap, flow_cap = _shape_targets(items, linked_ids,
                                                   shape_ratio, direction)
        dim = "w" if direction == "top_to_bottom" else "h"
        breadth0 = max(
            sum(items[i][dim] for i in col) + v_spacing * (len(col) - 1)
            for col in ordered_layers
        )
        if breadth0 > breadth_cap * SHAPE_SLACK:
            # Too tall for the shape: cap the breadth; the flow extent
            # then lands near the target on its own (area is conserved).
            biggest = max(items[i][dim] for i in items)
            wrap_breadth = max(breadth_cap, biggest + 1.0)
        else:
            # Not tall — maybe too long: fold the layer sequence.
            bands = _partition_bands(
                items, ordered_layers, direction, h_spacing, v_spacing,
                band_gap, shape_ratio,
                flow_cap if zone_size else None)
            if len(bands) == 1:
                bands = None

    ordered_layers = _wrap_layers(items, ordered_layers, direction,
                                  v_spacing, wrap_breadth)

    if bands is None:
        positions, main_extent = _place_layers(
            items, ordered_layers, direction, h_spacing, v_spacing, align
        )
    else:
        # Serpentine: each band is placed independently (so alignment
        # centers within its own band) and offset along the breadth axis
        # by the previous bands' measured extent.
        positions = {}
        cursor = 0.0
        flow_extent = 0.0
        for band in bands:
            band_pos, (bw, bh) = _place_layers(
                items, band, direction, h_spacing, v_spacing, align
            )
            if direction == "top_to_bottom":
                for iid, p in band_pos.items():
                    positions[iid] = [p[0] + cursor, p[1]]
                cursor += bw + band_gap
                flow_extent = max(flow_extent, bh)
            else:
                for iid, p in band_pos.items():
                    positions[iid] = [p[0], p[1] + cursor]
                cursor += bh + band_gap
                flow_extent = max(flow_extent, bw)
        breadth_extent = cursor - band_gap
        main_extent = ((breadth_extent, flow_extent)
                       if direction == "top_to_bottom"
                       else (flow_extent, breadth_extent))
    positions.update(_place_islands(items, islands, main_extent, h_spacing,
                                    v_spacing, shape_ratio))

    min_x = min(p[0] for p in positions.values())
    min_y = min(p[1] for p in positions.values())
    for p in positions.values():
        p[0] -= min_x
        p[1] -= min_y
    width = max(p[0] + items[iid]["w"] for iid, p in positions.items())
    height = max(p[1] + items[iid]["h"] for iid, p in positions.items())
    return positions, (width, height)


# ---------------------------------------------------------------------------
# Group handling
# ---------------------------------------------------------------------------

def _build_hierarchy(nodes, groups, extra_clusters=None, synthetic_start=0):
    """Containment forest over groups plus deepest-group node membership.

    A group's parent is the smallest group with a strictly larger area whose
    bounds contain its center (strict area ordering keeps this acyclic).
    Each node belongs to the smallest group containing its center.

    `extra_clusters` ([{"name", "node_ids"}], e.g. LLM suggestions) become
    synthetic root-level groups indexed from `synthetic_start`. They only
    claim nodes that no real group contains, so user groups always win.
    """
    by_index = {g["index"]: g for g in groups}
    parent = {}
    for group in groups:
        enclosing = [
            other for other in groups
            if other is not group
            and _group_area(other) > _group_area(group)
            and _group_contains(other, *_center(group))
        ]
        parent[group["index"]] = (
            min(enclosing, key=_group_area)["index"] if enclosing else None
        )

    children = {index: [] for index in by_index}
    roots = []
    for index in sorted(by_index):
        if parent[index] is None:
            roots.append(index)
        else:
            children[parent[index]].append(index)

    node_group = {}
    for nid, node in nodes.items():
        containing = [g for g in groups if _group_contains(g, *_center(node))]
        if containing:
            node_group[nid] = min(containing, key=_group_area)["index"]

    synthetic = {}
    for offset, cluster in enumerate(extra_clusters or []):
        members = [
            nid for nid in cluster.get("node_ids") or []
            if nid in nodes and node_group.get(nid) is None
        ]
        if len(members) < 2:
            continue
        index = synthetic_start + offset
        for nid in members:
            node_group[nid] = index
        parent[index] = None
        children[index] = []
        roots.append(index)
        synthetic[index] = {
            "index": index,
            # Old top-left of the members keeps ordering stable, like a
            # real group's old bounding does.
            "x": min(nodes[n]["x"] for n in members),
            "y": min(nodes[n]["y"] for n in members),
            "w": 1.0,
            "h": 1.0,
        }

    # Chain of containers from a node's deepest group up to the root (None).
    chains = {}
    for nid in nodes:
        chain = []
        cursor = node_group.get(nid)
        while cursor is not None:
            chain.append(cursor)
            cursor = parent[cursor]
        chain.append(None)
        chains[nid] = chain
    return children, roots, node_group, chains, synthetic


def _cluster_layout(nodes, edges, groups, direction, h_spacing, v_spacing,
                    sweeps, extra_clusters=None, synthetic_start=0,
                    wrap_breadth=0, align="top", shape_ratio=None,
                    zone_size=None):
    """Recursive compound layout: every group (nested ones included) is laid
    out as its own cluster, then each container arranges its direct child
    clusters and loose nodes with the same layered algorithm. Sibling frames
    can therefore never overlap, at any nesting depth."""
    children, root_groups, node_group, chains, synthetic = _build_hierarchy(
        nodes, groups, extra_clusters, synthetic_start
    )
    by_index = {g["index"]: g for g in groups}
    by_index.update(synthetic)

    direct_nodes = {index: [] for index in by_index}
    direct_nodes[None] = []
    for nid in nodes:
        direct_nodes[node_group.get(nid)].append(nid)

    def has_content(gindex):
        return bool(direct_nodes[gindex]) or any(has_content(c) for c in children[gindex])

    def representative(nid, container):
        """The direct child of `container` that transitively holds `nid`,
        or None when the node is not inside `container` at all."""
        chain = chains[nid]
        if container not in chain:
            return None
        i = chain.index(container)
        if i == 0:
            return ("node", nid)
        return ("group", chain[i - 1])

    layouts = {}      # container -> relative item positions
    frame_size = {}   # group index -> (w, h) including frame padding

    def layout_container(container):
        items = {}
        child_groups = children[container] if container is not None else root_groups
        for gindex in child_groups:
            if not has_content(gindex):
                continue
            layout_container(gindex)
            group = by_index[gindex]
            width, height = frame_size[gindex]
            items[("group", gindex)] = {
                "x": group["x"], "y": group["y"], "w": width, "h": height,
            }
        for nid in direct_nodes[container]:
            items[("node", nid)] = nodes[nid]

        # Each link surfaces exactly once: at the container that is the
        # lowest common ancestor of its two endpoints.
        container_edges = []
        for origin, target, slot in edges:
            ro = representative(origin, container)
            rt = representative(target, container)
            if ro is not None and rt is not None and ro != rt:
                container_edges.append((ro, rt, slot))

        positions, (width, height) = _layered_layout(
            items, container_edges, direction, h_spacing, v_spacing, sweeps,
            wrap_breadth, align, shape_ratio,
            # A drawn zone bounds the overall canvas; interiors follow
            # its RATIO only (their own size is theirs to determine).
            zone_size if container is None else None,
        )
        layouts[container] = positions
        if container is not None:
            frame_size[container] = (
                width + GROUP_SIDE_PADDING * 2.0,
                height + GROUP_TITLE_PADDING + GROUP_SIDE_PADDING,
            )

    layout_container(None)

    positions = {}
    frames = {}

    def emit(container, offset_x, offset_y):
        for key, rel in layouts[container].items():
            kind, ident = key
            x, y = offset_x + rel[0], offset_y + rel[1]
            if kind == "node":
                positions[ident] = [x, y]
            else:
                width, height = frame_size[ident]
                frames[ident] = [x, y, width, height]
                emit(ident, x + GROUP_SIDE_PADDING, y + GROUP_TITLE_PADDING)

    emit(None, 0.0, 0.0)
    updates = [{"index": index, "bounding": bounding}
               for index, bounding in sorted(frames.items())]
    return positions, updates


def _inner_group_layout(nodes, edges, groups, direction, h_spacing,
                        v_spacing, sweeps, wrap_breadth, align,
                        shape_ratio=None):
    """group_mode="inner": preserve the user's macro arrangement.

    Each top-level group keeps its current top-left corner; only its
    subtree (member nodes and nested child groups) is re-laid-out, with
    the frame resized to fit. Ungrouped nodes and empty groups are not
    touched at all. Returns absolute visual positions for exactly the
    nodes that moved."""
    children, roots, node_group, _chains, _synthetic = _build_hierarchy(
        nodes, groups)
    by_index = {g["index"]: g for g in groups}

    positions = {}
    updates = []
    bodies = []  # rigid pieces for overlap separation
    for root_index in roots:
        subtree = set()
        stack = [root_index]
        while stack:
            gindex = stack.pop()
            subtree.add(gindex)
            stack.extend(children[gindex])
        members = {nid: nodes[nid] for nid in nodes
                   if node_group.get(nid) in subtree}
        if not members:
            continue
        sub_edges = [e for e in edges if e[0] in members and e[1] in members]
        sub_groups = [by_index[gindex] for gindex in sorted(subtree)]
        rel_positions, rel_updates = _cluster_layout(
            members, sub_edges, sub_groups, direction, h_spacing,
            v_spacing, sweeps, None, 0, wrap_breadth, align, shape_ratio,
        )
        anchor = by_index[root_index]
        root_frame = next(
            (u for u in rel_updates if u["index"] == root_index), None)
        dx = anchor["x"] - (root_frame["bounding"][0] if root_frame else 0.0)
        dy = anchor["y"] - (root_frame["bounding"][1] if root_frame else 0.0)
        body_updates = []
        for nid, rel in rel_positions.items():
            positions[nid] = [rel[0] + dx, rel[1] + dy]
        for u in rel_updates:
            bounding = u["bounding"]
            entry = {"index": u["index"],
                     "bounding": [bounding[0] + dx, bounding[1] + dy,
                                  bounding[2], bounding[3]]}
            updates.append(entry)
            body_updates.append(entry)
        root_entry = next((u for u in body_updates
                           if u["index"] == root_index), None)
        if root_entry:
            bodies.append({
                "rect": list(root_entry["bounding"]),
                "node_ids": list(rel_positions),
                "updates": body_updates,
                "group": True,
            })
    # Loose (ungrouped) nodes join as rigid bodies too: they normally stay
    # untouched, but a grown frame may need to nudge them aside.
    for nid, node in nodes.items():
        if node_group.get(nid) is None:
            bodies.append({
                "rect": [node["x"], node["y"], node["w"], node["h"]],
                "node_ids": [nid],
                "updates": [],
                "group": False,
            })
    _separate_inner_bodies(bodies)
    for body in bodies:
        dx, dy = body.get("dx", 0.0), body.get("dy", 0.0)
        if not dx and not dy:
            continue
        for nid in body["node_ids"]:
            if nid in positions:
                positions[nid][0] += dx
                positions[nid][1] += dy
            else:  # a nudged loose node starts moving from its old spot
                positions[nid] = [nodes[nid]["x"] + dx, nodes[nid]["y"] + dy]
        for entry in body["updates"]:
            entry["bounding"][0] += dx
            entry["bounding"][1] += dy
    return positions, updates


INNER_SEPARATION_MARGIN = 20.0


def _separate_inner_bodies(bodies):
    """Push overlapping rigid bodies apart after an inner-mode sort.

    Frames anchored at their old top-left can grow right/down into a
    neighbor. For every collision involving at least one group frame, the
    body later in reading order moves right or down (whichever resolves
    the overlap with the smaller push), so the user's arrangement shifts
    minimally and only where needed. Two untouched loose nodes are never
    separated — pre-existing overlaps outside groups are none of our
    business in this mode."""
    for body in bodies:
        body["dx"] = 0.0
        body["dy"] = 0.0

    def rect(body):
        x, y, w, h = body["rect"]
        return (x + body["dx"], y + body["dy"], w, h)

    for _ in range(200):
        moved = False
        for i, first in enumerate(bodies):
            for second in bodies[i + 1:]:
                # Two loose nodes that nothing has displaced keep whatever
                # overlap the user left them with; once either has been
                # nudged, it must keep clearing what it lands on.
                if not (first["group"] or second["group"]
                        or first["dx"] or first["dy"]
                        or second["dx"] or second["dy"]):
                    continue
                a, b = rect(first), rect(second)
                gap = INNER_SEPARATION_MARGIN
                overlap_x = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                overlap_y = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                if overlap_x <= -gap or overlap_y <= -gap:
                    continue
                # Later in reading order moves, away from the anchor side.
                mover = second if (b[1], b[0]) >= (a[1], a[0]) else first
                if overlap_x + gap <= overlap_y + gap:
                    mover["dx"] += overlap_x + gap
                else:
                    mover["dy"] += overlap_y + gap
                moved = True
        if not moved:
            return


def _refit_member_groups(groups, nodes, positions):
    """group_mode="refit": re-fit every group frame around the nodes it
    visually contained before the sort, so groups keep their meaning."""
    updates = []
    for group in groups:
        placed = [
            nid for nid, node in nodes.items()
            if nid in positions and _group_contains(group, *_center(node))
        ]
        if not placed:
            continue
        min_x = min(positions[n][0] for n in placed) - GROUP_SIDE_PADDING
        min_y = min(positions[n][1] for n in placed) - GROUP_TITLE_PADDING
        max_x = max(positions[n][0] + nodes[n]["w"] for n in placed) + GROUP_SIDE_PADDING
        max_y = max(positions[n][1] + nodes[n]["h"] for n in placed) + GROUP_SIDE_PADDING
        updates.append({
            "index": group["index"],
            "bounding": [min_x, min_y, max_x - min_x, max_y - min_y],
        })
    return updates


def _compute_reroutes(workflow, nodes, positions):
    """Reposition native reroute points along their link's new path.

    ComfyUI serializes reroutes under extra.reroutes (0.4 format, with
    link parents in extra.linkExtensions) or top-level reroutes (schema
    v1, parentId on the link object). A link's parentId names the reroute
    closest to the TARGET; each reroute's parentId walks toward the
    ORIGIN. Reroutes in a chain are spread evenly along the straight
    segment between the two moved nodes; a reroute shared by several
    links (fan-out) keeps the position given by its first link.

    Returns {reroute_id: [x, y]} in the same relative visual space as
    `positions`. Unresolvable reroutes are left out (and thus untouched).
    """
    extra = workflow.get("extra") or {}
    raw_reroutes = workflow.get("reroutes") or extra.get("reroutes") or []
    if not isinstance(raw_reroutes, list) or not raw_reroutes:
        return {}
    reroutes = {}
    for raw in raw_reroutes:
        if isinstance(raw, dict) and raw.get("id") is not None:
            reroutes[raw["id"]] = raw

    by_str = {str(nid): nid for nid in nodes}
    link_ends = {}
    link_parent = {}
    for raw in workflow.get("links") or []:
        if isinstance(raw, (list, tuple)) and len(raw) >= 5:
            link_id, origin, target = raw[0], raw[1], raw[3]
        elif isinstance(raw, dict):
            link_id = raw.get("id")
            origin, target = raw.get("origin_id"), raw.get("target_id")
            if raw.get("parentId") is not None:
                link_parent[link_id] = raw["parentId"]
        else:
            continue
        origin = by_str.get(str(origin))
        target = by_str.get(str(target))
        if link_id is not None and origin is not None and target is not None:
            link_ends[link_id] = (origin, target)
    for ext in extra.get("linkExtensions") or []:
        if (isinstance(ext, dict) and ext.get("id") is not None
                and ext.get("parentId") is not None):
            link_parent[ext["id"]] = ext["parentId"]

    def port_points(link_id):
        origin, target = link_ends[link_id]
        if origin not in positions or target not in positions:
            return None
        o_node, t_node = nodes[origin], nodes[target]
        return (
            (positions[origin][0] + o_node["w"],
             positions[origin][1] + o_node["h"] / 2.0),
            (positions[target][0],
             positions[target][1] + t_node["h"] / 2.0),
        )

    def chain_toward_origin(terminal_id):
        chain = []
        cursor = terminal_id
        while cursor is not None and cursor in reroutes and len(chain) < 128:
            chain.append(cursor)
            cursor = reroutes[cursor].get("parentId")
        chain.reverse()  # origin -> target order
        return chain

    placed = {}

    def place_chain(chain, link_id):
        points = port_points(link_id)
        if not points:
            return
        (ox, oy), (tx, ty) = points
        count = len(chain)
        for i, rid in enumerate(chain):
            if rid in placed:
                continue  # shared trunk: first link's placement wins
            fraction = (i + 1.0) / (count + 1.0)
            placed[rid] = [ox + (tx - ox) * fraction,
                           oy + (ty - oy) * fraction]

    for link_id in sorted(link_parent, key=str):
        if link_id in link_ends:
            place_chain(chain_toward_origin(link_parent[link_id]), link_id)
    # Fallback for reroutes with no linkExtensions entry: place each along
    # the first of its own linkIds.
    for rid, raw in reroutes.items():
        if rid in placed:
            continue
        for link_id in raw.get("linkIds") or []:
            if link_id in link_ends:
                place_chain([rid], link_id)
                break
    return placed


def _park_stale_groups(groups, framed_indices, occupied_rects, v_spacing):
    """Groups that ended up with no content get no computed frame; left in
    place they could sit on top of the re-packed layout (and dragging them
    would grab unrelated nodes, since LiteGraph membership is geometric).
    Park them, at their original size, in a row below everything."""
    stale = [g for g in groups if g["index"] not in framed_indices]
    if not stale or not occupied_rects:
        return []
    x = min(r[0] for r in occupied_rects)
    y = max(r[1] + r[3] for r in occupied_rects) + GROUP_TITLE_PADDING * 2.0
    parked = []
    for group in sorted(stale, key=lambda g: (g["y"], g["x"])):
        parked.append({"index": group["index"],
                       "bounding": [x, y, group["w"], group["h"]]})
        x += group["w"] + v_spacing
    return parked


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def compute_layout(workflow, options=None, extra_clusters=None):
    """Compute a tidy layout for a serialized ComfyUI workflow.

    `extra_clusters` ([{"name", "node_ids"}], e.g. LLM suggestions) are laid
    out as synthetic groups around nodes no real group contains; they come
    back under "new_groups" so the frontend can create named frames.

    Returns {"positions": {node_id: [x, y]},
             "groups": [{"index", "bounding"}],
             "new_groups": [{"title", "bounding"}],
             "reroutes": {reroute_id: [x, y]}}.
    Positions use LiteGraph semantics (top of the node body); reroute
    positions are plain canvas points.
    """
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    direction = opts.get("direction") or "left_to_right"
    group_mode = opts.get("group_mode") or "cluster"
    h_spacing = max(10.0, _finite(opts.get("h_spacing", 80), 80))
    v_spacing = max(10.0, _finite(opts.get("v_spacing", 40), 40))
    sweeps = int(_finite(opts.get("barycenter_sweeps", 4), 4))
    wrap_breadth = max(0.0, _finite(opts.get("wrap_breadth", 2600), 2600))
    align = "top" if str(opts.get("align") or "center") == "top" else "center"
    snap = max(0.0, _finite(opts.get("snap_grid", 10), 10))
    shape_ratio = SHAPE_RATIOS.get(str(opts.get("shape") or "auto").lower())
    zone_size = None
    raw_zone = opts.get("zone_size")
    if isinstance(raw_zone, (list, tuple)) and len(raw_zone) >= 2:
        zone_w = _finite(raw_zone[0], 0.0)
        zone_h = _finite(raw_zone[1], 0.0)
        if zone_w > 0 and zone_h > 0:
            zone_size = (zone_w, zone_h)
            # The drawn box's own proportions drive every level.
            shape_ratio = max(0.2, min(5.0, zone_w / zone_h))

    nodes = _normalize_nodes(workflow)
    if not nodes:
        return {"positions": {}, "groups": [],
                "new_groups": [], "reroutes": {}}
    edges = _normalize_links(workflow, nodes, opts.get("detach_types") or ())
    if opts.get("link_set_get", True):
        edges += _wireless_edges(workflow, nodes)
    groups = _normalize_groups(workflow)
    synthetic_start = len(workflow.get("groups") or [])

    if group_mode == "inner":  # no groups -> nothing to sort inside
        positions, group_updates = _inner_group_layout(
            nodes, edges, groups, direction, h_spacing, v_spacing, sweeps,
            wrap_breadth, align, shape_ratio,
        )
    elif group_mode == "cluster" and (groups or extra_clusters):
        positions, group_updates = _cluster_layout(
            nodes, edges, groups, direction, h_spacing, v_spacing, sweeps,
            extra_clusters, synthetic_start, wrap_breadth, align,
            shape_ratio, zone_size,
        )
    else:
        positions, _extent = _layered_layout(
            nodes, edges, direction, h_spacing, v_spacing, sweeps,
            wrap_breadth, align, shape_ratio, zone_size,
        )
        group_updates = _refit_member_groups(groups, nodes, positions)


    refitted = list(group_updates)
    if group_mode != "inner":
        # In inner mode untouched groups stay exactly where the user put
        # them — parking would defeat the point of preserving the macro
        # arrangement.
        occupied = [(p[0], p[1], nodes[nid]["w"], nodes[nid]["h"])
                    for nid, p in positions.items()]
        occupied += [tuple(u["bounding"]) for u in group_updates]
        group_updates += _park_stale_groups(
            groups, {u["index"] for u in group_updates}, occupied, v_spacing
        )
    reroutes = _compute_reroutes(workflow, nodes, positions)

    # Anchor the new layout at the old graph's visual top-left so the
    # canvas view doesn't jump to a different region, and convert visual
    # tops back to LiteGraph pos (top of the node body). Both sides of the
    # anchor cover nodes AND live frames — a frame's padding sits before
    # its first node, so a nodes-only anchor would walk the whole graph by
    # one padding per re-sort instead of being a fixed point. Parked stale
    # frames are relocated anyway and stay out of it. Inner mode is
    # already absolute — every subtree is anchored at its group's corner.
    if group_mode == "inner":
        origin_x = origin_y = 0.0
    else:
        by_index = {g["index"]: g for g in groups}
        old_frames = [by_index[u["index"]] for u in refitted
                      if u["index"] in by_index]
        origin_x = (min([n["x"] for n in nodes.values()]
                        + [g["x"] for g in old_frames])
                    - min([p[0] for p in positions.values()]
                          + [u["bounding"][0] for u in refitted]))
        origin_y = (min([n["y"] for n in nodes.values()]
                        + [g["y"] for g in old_frames])
                    - min([p[1] for p in positions.values()]
                          + [u["bounding"][1] for u in refitted]))

    def rounded(value):
        return round(value / snap) * snap if snap else value

    def shifted(bounding):
        x = origin_x + bounding[0]
        y = origin_y + bounding[1]
        if not snap:
            return [x, y, bounding[2], bounding[3]]
        # Frames round outward (floor origin, ceil far edge) so snapping
        # can never clip the content they enclose.
        gx = math.floor(x / snap) * snap
        gy = math.floor(y / snap) * snap
        gw = math.ceil((x + bounding[2]) / snap) * snap - gx
        gh = math.ceil((y + bounding[3]) / snap) * snap - gy
        return [gx, gy, gw, gh]

    cluster_names = [c.get("name") or "Cluster" for c in extra_clusters or []]
    return {
        "positions": {
            str(nid): [rounded(origin_x + p[0]),
                       rounded(origin_y + p[1] + TITLE_HEIGHT)]
            for nid, p in positions.items()
        },
        "groups": [
            {"index": u["index"], "bounding": shifted(u["bounding"])}
            for u in group_updates if u["index"] < synthetic_start
        ],
        "new_groups": [
            {
                "title": cluster_names[u["index"] - synthetic_start],
                "bounding": shifted(u["bounding"]),
            }
            for u in group_updates if u["index"] >= synthetic_start
        ],
        "reroutes": {
            str(rid): [rounded(origin_x + p[0]), rounded(origin_y + p[1])]
            for rid, p in reroutes.items()
        },
    }
