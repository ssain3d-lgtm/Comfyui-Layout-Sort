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

# LiteGraph draws the title bar above node.pos, so a node's visual top is
# pos[1] - TITLE_HEIGHT and its visual height is size[1] + TITLE_HEIGHT.
TITLE_HEIGHT = 30.0

DEFAULT_OPTIONS = {
    "direction": "left_to_right",  # or "top_to_bottom"
    "h_spacing": 80,   # gap between layers (columns in left_to_right)
    "v_spacing": 40,   # gap between items inside a layer
    "group_mode": "cluster",  # or "refit"
    "barycenter_sweeps": 4,
}

GROUP_SIDE_PADDING = 24.0
GROUP_TITLE_PADDING = 40.0


def _xy(value, default=(0.0, 0.0)):
    """Read an [x, y]-like value that may be a list or a {"0":..,"1":..} dict."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [float(value[0]), float(value[1])]
    if isinstance(value, dict):
        return [float(value.get("0", default[0])), float(value.get("1", default[1]))]
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
        body_height = 0.0 if flags.get("collapsed") else max(size[1], 1.0)
        nodes[node_id] = {
            "x": pos[0],
            "y": pos[1] - TITLE_HEIGHT,
            "w": max(size[0], 1.0),
            "h": body_height + TITLE_HEIGHT,
        }
    return nodes


def _normalize_links(workflow, nodes):
    """Return [(origin_id, target_id, target_slot)] for every valid link."""
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


def _normalize_groups(workflow):
    groups = []
    for index, raw in enumerate(workflow.get("groups") or []):
        if not isinstance(raw, dict):
            continue
        bounding = raw.get("bounding")
        if not isinstance(bounding, (list, tuple)) or len(bounding) < 4:
            continue
        try:
            x, y, w, h = (float(v) for v in bounding[:4])
        except (TypeError, ValueError):
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

def _assign_layers(items, edges):
    """Longest-path layering over a (mostly) acyclic graph."""
    preds = {iid: set() for iid in items}
    succs = {iid: set() for iid in items}
    for origin, target, _slot in edges:
        preds[target].add(origin)
        succs[origin].add(target)

    linked = {iid for iid in items if preds[iid] or succs[iid]}

    indegree = {iid: len(preds[iid]) for iid in linked}
    frontier = [iid for iid in linked if indegree[iid] == 0]
    layer = {iid: 0 for iid in frontier}
    visited = []
    while frontier:
        iid = frontier.pop()
        visited.append(iid)
        for nxt in succs[iid]:
            layer[nxt] = max(layer.get(nxt, 0), layer[iid] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                frontier.append(nxt)

    # Cycle leftovers (possible in the cluster graph even when the node
    # graph is a DAG): park them in one extra layer instead of failing.
    leftovers = linked - set(visited)
    if leftovers:
        overflow = (max(layer.values()) + 1) if layer else 0
        for iid in leftovers:
            layer[iid] = overflow

    # Pull pure sources next to their first consumer so loaders don't all
    # pile up in column 0 regardless of where they are used.
    for iid in linked:
        if not preds[iid] and succs[iid] and iid not in leftovers:
            min_succ = min(layer[s] for s in succs[iid])
            layer[iid] = max(layer[iid], min_succ - 1)

    islands = [iid for iid in items if iid not in linked]
    return layer, preds, succs, islands


def _order_layers(items, layer, preds, succs, sweeps):
    """Barycenter ordering to reduce crossings; stable on original y."""
    layers = {}
    for iid, depth in layer.items():
        layers.setdefault(depth, []).append(iid)
    depths = sorted(layers)
    for depth in depths:
        layers[depth].sort(key=lambda iid: (items[iid]["y"], items[iid]["x"]))

    def sweep(order_by, neighbors):
        for depth in order_by:
            index = {}
            for other_depth in depths:
                if other_depth != depth:
                    for i, iid in enumerate(layers[other_depth]):
                        index[iid] = i
            current = {iid: i for i, iid in enumerate(layers[depth])}

            def key(iid):
                anchors = [index[n] for n in neighbors[iid] if n in index]
                if not anchors:
                    return (float(current[iid]), items[iid]["y"])
                return (sum(anchors) / len(anchors), items[iid]["y"])

            layers[depth].sort(key=key)

    for _ in range(max(0, sweeps)):
        sweep(depths, preds)                  # top-down: follow inputs
        sweep(list(reversed(depths)), succs)  # bottom-up: follow outputs
    return [layers[d] for d in depths]


def _place_layers(items, ordered_layers, direction, h_spacing, v_spacing):
    """Assign visual top-left coordinates layer by layer, centered on a
    shared axis. Returns (positions, (main_width, main_height))."""
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
            cross = (max_breadth - wide) / 2.0
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
        cross = (max_breadth - tall) / 2.0
        for iid in col:
            positions[iid] = [main + (thick - items[iid]["w"]) / 2.0, cross]
            cross += items[iid]["h"] + v_spacing
        main += thick + h_spacing
    return positions, (main - h_spacing, max_breadth)


def _place_islands(items, islands, main_extent, h_spacing, v_spacing):
    """Shelf-pack unlinked items (notes etc.) in rows under the main flow."""
    positions = {}
    if not islands:
        return positions
    ordered = sorted(islands, key=lambda iid: (items[iid]["y"], items[iid]["x"]))
    main_w, main_h = main_extent
    row_limit = max(main_w, 1200.0)
    x = 0.0
    y = (main_h + max(h_spacing, v_spacing) * 2.0) if main_h > 0 else 0.0
    row_height = 0.0
    for iid in ordered:
        item = items[iid]
        if x > 0 and (x + item["w"]) > row_limit:
            x = 0.0
            y += row_height + v_spacing
            row_height = 0.0
        positions[iid] = [x, y]
        x += item["w"] + v_spacing
        row_height = max(row_height, item["h"])
    return positions


def _layered_layout(items, edges, direction, h_spacing, v_spacing, sweeps):
    """Run the full layered pipeline. Returns (positions, (width, height))
    with positions normalized to a tight box anchored at (0, 0)."""
    if not items:
        return {}, (0.0, 0.0)
    layer, preds, succs, islands = _assign_layers(items, edges)
    ordered_layers = _order_layers(items, layer, preds, succs, sweeps)
    positions, main_extent = _place_layers(
        items, ordered_layers, direction, h_spacing, v_spacing
    )
    positions.update(_place_islands(items, islands, main_extent, h_spacing, v_spacing))

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
                    sweeps, extra_clusters=None, synthetic_start=0):
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
            items, container_edges, direction, h_spacing, v_spacing, sweeps
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
             "new_groups": [{"title", "bounding"}]}.
    Positions use LiteGraph semantics (top of the node body).
    """
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    direction = opts.get("direction") or "left_to_right"
    group_mode = opts.get("group_mode") or "cluster"
    h_spacing = max(10.0, float(opts.get("h_spacing", 80)))
    v_spacing = max(10.0, float(opts.get("v_spacing", 40)))
    sweeps = int(opts.get("barycenter_sweeps", 4))

    nodes = _normalize_nodes(workflow)
    if not nodes:
        return {"positions": {}, "groups": [], "new_groups": []}
    edges = _normalize_links(workflow, nodes)
    groups = _normalize_groups(workflow)
    synthetic_start = len(workflow.get("groups") or [])

    if group_mode == "cluster" and (groups or extra_clusters):
        positions, group_updates = _cluster_layout(
            nodes, edges, groups, direction, h_spacing, v_spacing, sweeps,
            extra_clusters, synthetic_start,
        )
    else:
        positions, _extent = _layered_layout(
            nodes, edges, direction, h_spacing, v_spacing, sweeps
        )
        group_updates = _refit_member_groups(groups, nodes, positions)

    # Anchor the new layout at the old graph's visual top-left so the
    # canvas view doesn't jump to a different region, and convert visual
    # tops back to LiteGraph pos (top of the node body).
    origin_x = min(n["x"] for n in nodes.values())
    origin_y = min(n["y"] for n in nodes.values())

    def shifted(bounding):
        return [origin_x + bounding[0], origin_y + bounding[1],
                bounding[2], bounding[3]]

    cluster_names = [c.get("name") or "Cluster" for c in extra_clusters or []]
    return {
        "positions": {
            str(nid): [origin_x + p[0], origin_y + p[1] + TITLE_HEIGHT]
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
    }
