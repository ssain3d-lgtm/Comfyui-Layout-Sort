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
    # Aim for a roughly square shelf: with many/large islands (e.g. a
    # container full of unconnected sub-groups) a fixed narrow row limit
    # would stack them into one enormously tall column.
    total_area = sum(items[iid]["w"] * items[iid]["h"] for iid in islands)
    row_limit = max(main_w, 1200.0, 1.25 * math.sqrt(total_area))
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


def _layered_layout(items, edges, direction, h_spacing, v_spacing, sweeps,
                    wrap_breadth=0):
    """Run the full layered pipeline. Returns (positions, (width, height))
    with positions normalized to a tight box anchored at (0, 0)."""
    if not items:
        return {}, (0.0, 0.0)
    layer, preds, succs, islands = _assign_layers(items, edges)
    ordered_layers = _order_layers(items, layer, preds, succs, edges, sweeps)
    ordered_layers = _wrap_layers(items, ordered_layers, direction,
                                  v_spacing, wrap_breadth)
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
                    sweeps, extra_clusters=None, synthetic_start=0,
                    wrap_breadth=0):
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
            wrap_breadth,
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

    nodes = _normalize_nodes(workflow)
    if not nodes:
        return {"positions": {}, "groups": [], "new_groups": [], "reroutes": {}}
    edges = _normalize_links(workflow, nodes, opts.get("detach_types") or ())
    groups = _normalize_groups(workflow)
    synthetic_start = len(workflow.get("groups") or [])

    if group_mode == "cluster" and (groups or extra_clusters):
        positions, group_updates = _cluster_layout(
            nodes, edges, groups, direction, h_spacing, v_spacing, sweeps,
            extra_clusters, synthetic_start, wrap_breadth,
        )
    else:
        positions, _extent = _layered_layout(
            nodes, edges, direction, h_spacing, v_spacing, sweeps,
            wrap_breadth,
        )
        group_updates = _refit_member_groups(groups, nodes, positions)

    occupied = [(p[0], p[1], nodes[nid]["w"], nodes[nid]["h"])
                for nid, p in positions.items()]
    occupied += [tuple(u["bounding"]) for u in group_updates]
    group_updates += _park_stale_groups(
        groups, {u["index"] for u in group_updates}, occupied, v_spacing
    )
    reroutes = _compute_reroutes(workflow, nodes, positions)

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
        "reroutes": {
            str(rid): [origin_x + p[0], origin_y + p[1]]
            for rid, p in reroutes.items()
        },
    }
