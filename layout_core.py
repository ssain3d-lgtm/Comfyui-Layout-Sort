"""Pure-Python layout engine for ComfyUI workflows.

Takes a serialized workflow (the same JSON the frontend saves / embeds in
EXTRA_PNGINFO) and computes new node positions using a layered
(Sugiyama-style) algorithm:

  1. Normalize nodes / links (both array and object link formats).
  2. Assign each linked node to a layer via longest-path topological
     layering, so data flows strictly in one direction.
  3. Order nodes inside each layer with barycenter sweeps to reduce
     link crossings.
  4. Emit coordinates: layers become columns (or rows), centered on a
     shared axis, anchored at the original graph's top-left corner.
  5. Nodes with no links (notes, disconnected leftovers) are shelf-packed
     below the main flow. Group frames are re-fitted around the nodes
     they contained before the sort.

This module has no ComfyUI imports so it can be run and tested standalone.
"""

# LiteGraph draws the title bar above node.pos, so a node's visual top is
# pos[1] - TITLE_HEIGHT and its visual height is size[1] + TITLE_HEIGHT.
TITLE_HEIGHT = 30.0

DEFAULT_OPTIONS = {
    "direction": "left_to_right",  # or "top_to_bottom"
    "h_spacing": 80,   # gap between layers (columns in left_to_right)
    "v_spacing": 40,   # gap between nodes inside a layer
    "barycenter_sweeps": 4,
}

GROUP_PADDING = 24.0
GROUP_TITLE_PADDING = 40.0


def _xy(value, default=(0.0, 0.0)):
    """Read an [x, y]-like value that may be a list or a {"0":..,"1":..} dict."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [float(value[0]), float(value[1])]
    if isinstance(value, dict):
        return [float(value.get("0", default[0])), float(value.get("1", default[1]))]
    return [float(default[0]), float(default[1])]


def _normalize_nodes(workflow):
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
        body_height = 0.0 if collapsed else max(size[1], 1.0)
        nodes[node_id] = {
            "id": node_id,
            "x": pos[0],
            "y": pos[1],
            "width": width,
            # Visual footprint includes the title bar drawn above pos.
            "visual_height": body_height + TITLE_HEIGHT,
        }
    return nodes


def _normalize_links(workflow, nodes):
    """Yield (origin_id, target_id, target_slot) for every valid link."""
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
        if origin in nodes and target in nodes and origin != target:
            try:
                edges.append((origin, target, int(slot or 0)))
            except (TypeError, ValueError):
                edges.append((origin, target, 0))
    return edges


def _assign_layers(nodes, edges):
    """Longest-path layering over a (mostly) acyclic graph."""
    preds = {nid: set() for nid in nodes}
    succs = {nid: set() for nid in nodes}
    for origin, target, _slot in edges:
        preds[target].add(origin)
        succs[origin].add(target)

    linked = {nid for nid in nodes if preds[nid] or succs[nid]}

    indegree = {nid: len(preds[nid]) for nid in linked}
    frontier = [nid for nid in linked if indegree[nid] == 0]
    layer = {nid: 0 for nid in frontier}
    visited = []
    while frontier:
        nid = frontier.pop()
        visited.append(nid)
        for nxt in succs[nid]:
            layer[nxt] = max(layer.get(nxt, 0), layer[nid] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                frontier.append(nxt)

    # Cycle leftovers (shouldn't happen in executable graphs): park them
    # in one extra layer instead of failing.
    leftovers = linked - set(visited)
    if leftovers:
        overflow = (max(layer.values()) + 1) if layer else 0
        for nid in leftovers:
            layer[nid] = overflow

    # Pull pure sources next to their first consumer so loaders don't all
    # pile up in column 0 regardless of where they are used.
    for nid in linked:
        if not preds[nid] and succs[nid] and nid not in leftovers:
            min_succ = min(layer[s] for s in succs[nid])
            layer[nid] = max(layer[nid], min_succ - 1)

    islands = [nid for nid in nodes if nid not in linked]
    return layer, preds, succs, islands


def _order_layers(nodes, layer, preds, succs, sweeps):
    """Barycenter ordering to reduce crossings; stable on original y."""
    layers = {}
    for nid, depth in layer.items():
        layers.setdefault(depth, []).append(nid)
    depths = sorted(layers)
    for depth in depths:
        layers[depth].sort(key=lambda nid: (nodes[nid]["y"], nodes[nid]["x"]))

    def sweep(order_by, neighbors):
        for depth in order_by:
            index = {}
            for other_depth in depths:
                if other_depth != depth:
                    for i, nid in enumerate(layers[other_depth]):
                        index[nid] = i
            current = {nid: i for i, nid in enumerate(layers[depth])}

            def key(nid):
                anchors = [index[n] for n in neighbors[nid] if n in index]
                if not anchors:
                    return (float(current[nid]), nodes[nid]["y"])
                return (sum(anchors) / len(anchors), nodes[nid]["y"])

            layers[depth].sort(key=key)

    for _ in range(max(0, sweeps)):
        sweep(depths, preds)            # top-down: follow inputs
        sweep(list(reversed(depths)), succs)  # bottom-up: follow outputs
    return [layers[d] for d in depths]


def _place_main(nodes, ordered_layers, origin, direction, h_spacing, v_spacing):
    """Assign coordinates layer by layer, centered on a shared axis.

    Returns positions keyed by node id, as LiteGraph `pos` values
    (i.e. top of the node body; title bar sits above it).
    """
    positions = {}
    if not ordered_layers:
        return positions, (origin[0], origin[1], 0.0, 0.0)

    if direction == "top_to_bottom":
        thickness = [max(nodes[n]["visual_height"] for n in col) for col in ordered_layers]
        breadth = [
            sum(nodes[n]["width"] for n in col) + v_spacing * (len(col) - 1)
            for col in ordered_layers
        ]
        max_breadth = max(breadth)
        main_cursor = origin[1]
        for col, thick, wide in zip(ordered_layers, thickness, breadth):
            cross = origin[0] + (max_breadth - wide) / 2.0
            for nid in col:
                node = nodes[nid]
                positions[nid] = [cross, main_cursor + TITLE_HEIGHT]
                cross += node["width"] + v_spacing
            main_cursor += thick + h_spacing
        extent = (max_breadth, main_cursor - h_spacing - origin[1])
    else:  # left_to_right
        thickness = [max(nodes[n]["width"] for n in col) for col in ordered_layers]
        breadth = [
            sum(nodes[n]["visual_height"] for n in col) + v_spacing * (len(col) - 1)
            for col in ordered_layers
        ]
        max_breadth = max(breadth)
        main_cursor = origin[0]
        for col, thick, tall in zip(ordered_layers, thickness, breadth):
            cross = origin[1] + (max_breadth - tall) / 2.0
            for nid in col:
                node = nodes[nid]
                x = main_cursor + (thick - node["width"]) / 2.0
                positions[nid] = [x, cross + TITLE_HEIGHT]
                cross += node["visual_height"] + v_spacing
            main_cursor += thick + h_spacing
        extent = (main_cursor - h_spacing - origin[0], max_breadth)

    return positions, (origin[0], origin[1], extent[0], extent[1])


def _place_islands(nodes, islands, main_rect, h_spacing, v_spacing):
    """Shelf-pack unlinked nodes (notes etc.) in rows under the main flow."""
    positions = {}
    if not islands:
        return positions
    ordered = sorted(islands, key=lambda nid: (nodes[nid]["y"], nodes[nid]["x"]))
    ox, oy, main_w, main_h = main_rect
    row_limit = max(main_w, 1200.0)
    x = ox
    y = oy + main_h + max(h_spacing, v_spacing) * 2.0
    row_height = 0.0
    for nid in ordered:
        node = nodes[nid]
        if x > ox and (x + node["width"]) > (ox + row_limit):
            x = ox
            y += row_height + v_spacing
            row_height = 0.0
        positions[nid] = [x, y + TITLE_HEIGHT]
        x += node["width"] + v_spacing
        row_height = max(row_height, node["visual_height"])
    return positions


def _refit_groups(workflow, nodes, positions):
    """Re-fit each group frame around the nodes it visually contained
    before the sort, so groups keep their meaning after nodes move."""
    updates = []
    for index, group in enumerate(workflow.get("groups") or []):
        bounding = group.get("bounding")
        if not isinstance(bounding, (list, tuple)) or len(bounding) < 4:
            continue
        gx, gy, gw, gh = (float(v) for v in bounding[:4])
        members = []
        for nid, node in nodes.items():
            cx = node["x"] + node["width"] / 2.0
            cy = node["y"] - TITLE_HEIGHT + node["visual_height"] / 2.0
            if gx <= cx <= gx + gw and gy <= cy <= gy + gh:
                members.append(nid)
        placed = [nid for nid in members if nid in positions]
        if not placed:
            continue
        min_x = min(positions[n][0] for n in placed)
        min_y = min(positions[n][1] - TITLE_HEIGHT for n in placed)
        max_x = max(positions[n][0] + nodes[n]["width"] for n in placed)
        max_y = max(positions[n][1] - TITLE_HEIGHT + nodes[n]["visual_height"] for n in placed)
        updates.append({
            "index": index,
            "bounding": [
                min_x - GROUP_PADDING,
                min_y - GROUP_TITLE_PADDING,
                (max_x - min_x) + GROUP_PADDING * 2.0,
                (max_y - min_y) + GROUP_TITLE_PADDING + GROUP_PADDING,
            ],
        })
    return updates


def compute_layout(workflow, options=None):
    """Compute a tidy layout for a serialized ComfyUI workflow.

    Returns {"positions": {node_id: [x, y]}, "groups": [{"index", "bounding"}]}.
    Positions use LiteGraph semantics (top of the node body).
    """
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    direction = opts.get("direction") or "left_to_right"
    h_spacing = max(10.0, float(opts.get("h_spacing", 80)))
    v_spacing = max(10.0, float(opts.get("v_spacing", 40)))

    nodes = _normalize_nodes(workflow)
    if not nodes:
        return {"positions": {}, "groups": []}

    edges = _normalize_links(workflow, nodes)
    layer, preds, succs, islands = _assign_layers(nodes, edges)
    ordered_layers = _order_layers(nodes, layer, preds, succs, opts["barycenter_sweeps"])

    # Anchor the new layout at the old graph's visual top-left so the
    # canvas view doesn't jump to a different region.
    origin = (
        min(n["x"] for n in nodes.values()),
        min(n["y"] - TITLE_HEIGHT for n in nodes.values()),
    )

    positions, main_rect = _place_main(
        nodes, ordered_layers, origin, direction, h_spacing, v_spacing
    )
    positions.update(_place_islands(nodes, islands, main_rect, h_spacing, v_spacing))
    groups = _refit_groups(workflow, nodes, positions)

    return {
        "positions": {str(nid): pos for nid, pos in positions.items()},
        "groups": groups,
    }
