"""ComfyUI node + server glue for Layout Sort.

Two ways to trigger a sort:
  * Run the workflow: the LayoutSort node reads the serialized workflow
    from the hidden EXTRA_PNGINFO input, computes the layout on the
    backend, and pushes new positions to the browser over the websocket.
  * Press the "Sort now" button on the node (added by web/layoutSort.js):
    the frontend POSTs the current graph to /layout_sort/compute and
    applies the returned positions immediately, no queue needed.

The layout itself is fully deterministic (layout_core); nothing here
talks to any external service.
"""

import asyncio

from .layout_core import (
    GROUP_SIDE_PADDING,
    GROUP_TITLE_PADDING,
    TITLE_HEIGHT,
    _center,
    _group_contains,
    _normalize_groups,
    _normalize_links,
    _normalize_nodes,
    compute_layout,
)

WS_EVENT = "layout_sort_apply"

# The style dropdown expands to engine options; explicit per-option keys
# in the request still win over the preset. Node sizes are never touched.
STYLE_PRESETS = {
    # Centered columns — fewer crossings on big graphs (the default).
    "flow": {"align": "center"},
    # Top-left aligned columns snapped to the canvas grid — the tidy look
    # for small graphs.
    "grid": {"align": "top"},
}

def _scoped_workflow(workflow, scope_ids):
    """A copy of the workflow reduced to the selected nodes.

    Keeps links whose both endpoints are selected, and group frames whose
    every member (by the engine's center-containment rule) is selected —
    a partially selected group's frame stays untouched and its selected
    members sort as loose nodes. Returns (scoped_workflow, index_map)
    where index_map translates scoped group indices back to the original
    workflow's group indices.
    """
    ids = {str(v) for v in scope_ids}
    all_nodes = _normalize_nodes(workflow)
    kept_nodes = [
        raw for raw in workflow.get("nodes") or []
        if isinstance(raw, dict) and str(raw.get("id")) in ids
    ]
    kept_ids = {str(raw.get("id")) for raw in kept_nodes}

    kept_links = []
    for raw in workflow.get("links") or []:
        if isinstance(raw, (list, tuple)) and len(raw) >= 5:
            origin, target = raw[1], raw[3]
        elif isinstance(raw, dict):
            origin, target = raw.get("origin_id"), raw.get("target_id")
        else:
            continue
        if str(origin) in kept_ids and str(target) in kept_ids:
            kept_links.append(raw)

    kept_groups, index_map = [], {}
    raw_groups = workflow.get("groups") or []
    for group in _normalize_groups(workflow):
        members = [
            nid for nid, node in all_nodes.items()
            if _group_contains(group, *_center(node))
        ]
        if members and all(str(nid) in kept_ids for nid in members):
            index_map[len(kept_groups)] = group["index"]
            kept_groups.append(raw_groups[group["index"]])

    scoped = dict(workflow)
    scoped["nodes"] = kept_nodes
    scoped["links"] = kept_links
    scoped["groups"] = kept_groups
    return scoped, index_map


ZONE_MATCH_TOLERANCE = 3.0


def _shift_result(positions, group_updates, reroutes, target_x, target_y):
    """Translate a compute result so its visual top-left lands on the
    target point (snapped delta, so grid alignment survives). Mutates in
    place; returns (dx, dy)."""
    if not positions and not group_updates:
        return 0.0, 0.0
    min_x = min([p[0] for p in positions.values()]
                + [u["bounding"][0] for u in group_updates])
    min_y = min([p[1] - TITLE_HEIGHT for p in positions.values()]
                + [u["bounding"][1] for u in group_updates])
    dx = round((target_x - min_x) / 10.0) * 10.0
    dy = round((target_y - min_y) / 10.0) * 10.0
    if dx or dy:
        for p in positions.values():
            p[0] += dx
            p[1] += dy
        for u in group_updates:
            u["bounding"][0] += dx
            u["bounding"][1] += dy
        for p in (reroutes or {}).values():
            p[0] += dx
            p[1] += dy
    return dx, dy


FIT_SPACING_SCALES = (1.0, 0.75, 0.5, 0.3)


def _content_overflow(positions, frame_updates, nodes, rect):
    """(overflow_w, overflow_h) of placed content past the frame's
    right/bottom edges (content is anchored at the padded top-left)."""
    right = [p[0] + nodes[_nid_key(nodes, k)]["w"]
             for k, p in positions.items()]
    bottom = [p[1] - TITLE_HEIGHT + nodes[_nid_key(nodes, k)]["h"]
              for k, p in positions.items()]
    right += [u["bounding"][0] + u["bounding"][2] for u in frame_updates]
    bottom += [u["bounding"][1] + u["bounding"][3] for u in frame_updates]
    over_w = max([0.0] + [r - (rect[0] + rect[2]) for r in right])
    over_h = max([0.0] + [b - (rect[1] + rect[3]) for b in bottom])
    return over_w, over_h


def _fits_originally(nodes, rect):
    """Did every member already sit fully inside the frame?"""
    return all(
        n["x"] >= rect[0] - 1.0 and n["y"] >= rect[1] - 1.0
        and n["x"] + n["w"] <= rect[0] + rect[2] + 1.0
        and n["y"] + n["h"] <= rect[1] + rect[3] + 1.0
        for n in nodes.values())


def _flow_order(nodes, edges):
    """Topological (data-flow) order, ties broken by the user's reading
    order (top-to-bottom, left-to-right); cycles fall back gracefully."""
    indeg = {nid: 0 for nid in nodes}
    succ = {nid: [] for nid in nodes}
    for o, t in edges:
        if o in nodes and t in nodes and o != t:
            succ[o].append(t)
            indeg[t] += 1
    key = lambda nid: (nodes[nid]["y"], nodes[nid]["x"])
    ready = sorted([n for n, d in indeg.items() if d == 0], key=key)
    order, seen = [], set()
    while ready or len(order) < len(nodes):
        if not ready:  # cycle: take the earliest remaining node
            ready = [min((n for n in nodes if n not in seen), key=key)]
        nid = ready.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        order.append(nid)
        for t in succ[nid]:
            indeg[t] -= 1
            if indeg[t] <= 0 and t not in seen:
                ready.append(t)
        ready.sort(key=key)
    return order


def _compact_pack(nodes, order, width, height, gap, column_major):
    """Pack nodes in flow order into columns (or rows) inside a
    width x height box — how people tidy a small group by hand. Returns
    visual top-left positions relative to the box, or None if it can't
    fit."""
    positions = {}
    if column_major:
        x = y = col_w = 0.0
        for nid in order:
            n = nodes[nid]
            if y > 0 and y + n["h"] > height:
                x += col_w + gap
                y = col_w = 0.0
            positions[nid] = [x, y]
            y += n["h"] + gap
            col_w = max(col_w, n["w"])
        used_w, used_h = x + col_w, max(
            p[1] + nodes[i]["h"] for i, p in positions.items())
    else:
        x = y = row_h = 0.0
        for nid in order:
            n = nodes[nid]
            if x > 0 and x + n["w"] > width:
                y += row_h + gap
                x = row_h = 0.0
            positions[nid] = [x, y]
            x += n["w"] + gap
            row_h = max(row_h, n["h"])
        used_w = max(p[0] + nodes[i]["w"] for i, p in positions.items())
        used_h = y + row_h
    if used_w > width + 1.0 or used_h > height + 1.0:
        return None
    return positions


def _fit_frame_sorts(workflow, frames, options):
    """Sort each selected populated group INSIDE its own frame.

    The frame is the user's decision, so it is never moved or resized:
    its interior (minus the title/side padding) becomes the target box —
    members re-arrange to its proportions and land at its corner. Nested
    child frames still refit around their content.

    A tidy must never make things worse than the user's own arrangement,
    so candidates are tried in order of how little they deviate from the
    widget settings — the requested direction at full, 75%, 50% spacing,
    then the other direction, then 30% spacing — and the first that fits
    wins. If nothing fits: when the members already fit before, the group
    is left exactly as it was; otherwise the least-overflowing candidate
    is used and reported.

    Returns (positions, group_updates with live indices, reroutes,
    report) where report = {"overflow", "unchanged", "adjusted"} lists of
    frame titles."""
    positions, updates, reroutes = {}, [], {}
    report = {"overflow": [], "unchanged": [], "adjusted": []}
    raw_groups = workflow.get("groups") or []
    base_dir = options.get("direction") or "left_to_right"
    other_dir = ("top_to_bottom" if base_dir != "top_to_bottom"
                 else "left_to_right")
    base_h = float(options.get("h_spacing") or 80)
    base_v = float(options.get("v_spacing") or 40)
    candidates = (
        [(base_dir, s) for s in FIT_SPACING_SCALES[:3]]
        + [(other_dir, s) for s in FIT_SPACING_SCALES[:3]]
        + [(base_dir, FIT_SPACING_SCALES[3]), (other_dir, FIT_SPACING_SCALES[3])]
    )
    for frame in frames:
        rect = frame["rect"]
        title = str(frame.get("title") or "group")
        scoped, index_map = _scoped_workflow(workflow, frame["ids"])
        # Drop the outer frame itself from the copy (matched by rect):
        # it must be neither refit nor parked.
        inner_groups, chain = [], {}
        for scoped_idx, live_idx in sorted(index_map.items()):
            bounding = _normalize_groups({"groups":
                                          [raw_groups[live_idx]]})[0]
            if (abs(bounding["x"] - rect[0]) <= ZONE_MATCH_TOLERANCE
                    and abs(bounding["y"] - rect[1]) <= ZONE_MATCH_TOLERANCE
                    and abs(bounding["w"] - rect[2]) <= ZONE_MATCH_TOLERANCE
                    and abs(bounding["h"] - rect[3]) <= ZONE_MATCH_TOLERANCE):
                continue
            chain[len(inner_groups)] = live_idx
            inner_groups.append(scoped["groups"][scoped_idx])
        scoped = dict(scoped)
        scoped["groups"] = inner_groups
        nodes = _normalize_nodes(scoped)
        if not nodes:
            continue

        interior_w = max(rect[2] - GROUP_SIDE_PADDING * 2.0, 100.0)
        interior_h = max(rect[3] - GROUP_TITLE_PADDING - GROUP_SIDE_PADDING,
                         100.0)
        best = None
        for index, (direction, scale) in enumerate(candidates):
            opts = dict(options)
            opts["zone_size"] = [interior_w, interior_h]
            opts["direction"] = direction
            opts["h_spacing"] = max(10.0, round(base_h * scale))
            opts["v_spacing"] = max(10.0, round(base_v * scale))
            result = compute_layout(scoped, opts)
            frame_updates = [
                {**u, "index": chain[u["index"]]}
                for u in result.get("groups") or []
                if u["index"] in chain
            ]
            _shift_result(result["positions"], frame_updates,
                          result.get("reroutes") or {},
                          rect[0] + GROUP_SIDE_PADDING,
                          rect[1] + GROUP_TITLE_PADDING)
            over_w, over_h = _content_overflow(result["positions"],
                                               frame_updates, nodes, rect)
            badness = over_w * rect[3] + over_h * rect[2] + over_w * over_h
            if best is None or badness < best[0]:
                best = (badness, index, result, frame_updates)
            if badness <= 1.0:
                break

        badness, index, result, frame_updates = best
        if badness > 1.0 and not inner_groups:
            # Layered layouts can't fit: try a compact flow-ordered pack
            # (columns first, then rows) at shrinking gaps.
            edges = [(o, t) for o, t, _slot in
                     _normalize_links(scoped, nodes)]
            order = _flow_order(nodes, edges)
            for scale in (0.5, 0.3, 0.0):
                gap = max(10.0, round(base_v * scale))
                packed = None
                for column_major in (True, False):
                    packed = _compact_pack(nodes, order, interior_w,
                                           interior_h, gap, column_major)
                    if packed:
                        break
                if packed:
                    x0 = rect[0] + GROUP_SIDE_PADDING
                    y0 = rect[1] + GROUP_TITLE_PADDING
                    result = {"positions": {
                        str(nid): [round((x0 + p[0]) / 10.0) * 10.0,
                                   round((y0 + p[1]) / 10.0) * 10.0
                                   + TITLE_HEIGHT]
                        for nid, p in packed.items()}, "reroutes": {}}
                    over_w, over_h = _content_overflow(
                        result["positions"], [], nodes, rect)
                    if over_w <= 1.0 and over_h <= 1.0:
                        badness, index, frame_updates = 0.0, 1, []
                        break
        if badness > 1.0 and _fits_originally(nodes, rect):
            report["unchanged"].append(title)
            continue
        if badness > 1.0:
            report["overflow"].append(title)
        elif index > 0:
            report["adjusted"].append(title)
        positions.update(result["positions"])
        updates.extend(frame_updates)
        reroutes.update(result.get("reroutes") or {})
    return positions, updates, reroutes, report


def _nid_key(nodes, key):
    """Map a stringified position key back onto the normalized-nodes key."""
    if key in nodes:
        return key
    try:
        as_int = int(key)
    except (TypeError, ValueError):
        return key
    return as_int if as_int in nodes else key


def _validated_frames(raw):
    frames = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        rect = entry.get("rect")
        ids = entry.get("ids")
        if not (isinstance(rect, (list, tuple)) and len(rect) >= 4
                and isinstance(ids, list) and ids):
            continue
        try:
            rect = [float(v) for v in rect[:4]]
        except (TypeError, ValueError):
            continue
        if rect[2] <= 0 or rect[3] <= 0:
            continue
        frames.append({"rect": rect, "ids": list(ids),
                       "title": entry.get("title")})
    return frames


def _resolve_zone_group(workflow, rect, index_hint):
    """Index of the EMPTY group frame matching the drawn rect.

    A frontend's group index can drift from the serialized order (Vue
    proxies make identity lookups unreliable), so the rectangle is the
    source of truth: the hinted index is only tried first. Returns
    (index, None) or (None, reason)."""
    groups = _normalize_groups(workflow)
    if not groups:
        return None, "the workflow has no group frames"
    nodes = _normalize_nodes(workflow)

    def matches(g):
        return (abs(g["x"] - rect[0]) <= ZONE_MATCH_TOLERANCE
                and abs(g["y"] - rect[1]) <= ZONE_MATCH_TOLERANCE
                and abs(g["w"] - rect[2]) <= ZONE_MATCH_TOLERANCE
                and abs(g["h"] - rect[3]) <= ZONE_MATCH_TOLERANCE)

    def empty(g):
        return not any(_group_contains(g, *_center(n))
                       for n in nodes.values())

    ordered = list(groups)
    if isinstance(index_hint, int):
        ordered.sort(key=lambda g: g["index"] != index_hint)
    for group in ordered:
        if not matches(group):
            continue
        if empty(group):
            return group["index"], None
        return None, ("the selected frame contains nodes — a zone must "
                      "be an empty frame")
    return None, "no group frame matches the drawn rectangle"


def _drop_empty_group(workflow, index):
    """Remove group `index` when it is verifiably EMPTY (a drawn zone).

    Returns (workflow_copy, index_map new->old) or (workflow, None) when
    the group has members or the index is invalid — a populated group is
    content, never a zone specification."""
    raw_groups = workflow.get("groups") or []
    if not isinstance(index, int) or not (0 <= index < len(raw_groups)):
        return workflow, None
    target = next((g for g in _normalize_groups(workflow)
                   if g["index"] == index), None)
    if target is None:
        return workflow, None
    nodes = _normalize_nodes(workflow)
    if any(_group_contains(target, *_center(n)) for n in nodes.values()):
        return workflow, None
    out = dict(workflow)
    out["groups"] = [g for i, g in enumerate(raw_groups) if i != index]
    survivors = [i for i in range(len(raw_groups)) if i != index]
    return out, {new: old for new, old in enumerate(survivors)}


def run_layout(workflow, options):
    """Shared pipeline for the node and the HTTP route.

    options["scope_ids"] (node id list) restricts the sort to a
    selection: only those nodes move, anchored where the selection sits,
    and everything else — including partially selected group frames — is
    left exactly as it was."""
    if not workflow.get("nodes") and any(
        isinstance(v, dict) and "class_type" in v
        for v in workflow.values() if isinstance(v, dict)
    ):
        # "Save (API format)" exports carry no positions/links/groups —
        # there is nothing to lay out. Fail loudly instead of no-opping.
        raise ValueError(
            "this looks like an API-format workflow export (no layout "
            "data); load it into ComfyUI and sort the live graph instead"
        )
    options = dict(options or {})
    full_group_count = len(workflow.get("groups") or [])
    group_mode = str(options.get("group_mode") or "cluster")

    # A selected EMPTY group frame acts as a drawn zone: the layout is
    # shaped to its proportions and placed at its corner. The zone frame
    # itself is dropped from the compute copy (it is the specification,
    # not content) and stays exactly where the user drew it.
    zone_rect = None
    zone_status = None
    raw_zone = options.pop("zone", None)
    zone_index = options.pop("zone_index", None)
    group_index_map = None
    if isinstance(raw_zone, (list, tuple)) and len(raw_zone) >= 4:
        try:
            candidate = [float(v) for v in raw_zone[:4]]
        except (TypeError, ValueError):
            candidate = None
        if not candidate or candidate[2] <= 0 or candidate[3] <= 0:
            zone_status = {"applied": False,
                           "reason": "invalid zone rectangle"}
        elif group_mode == "inner":
            zone_status = {"applied": False,
                           "reason": 'zones need group_mode "cluster" or '
                                     '"refit" — inner keeps your macro '
                                     "layout in place"}
        else:
            resolved, reason = _resolve_zone_group(workflow, candidate,
                                                   zone_index)
            if resolved is not None:
                workflow, group_index_map = _drop_empty_group(workflow,
                                                              resolved)
            if resolved is None or group_index_map is None:
                zone_status = {"applied": False,
                               "reason": reason
                               or "zone frame could not be detached"}
            else:
                zone_rect = candidate
                options["zone_size"] = [candidate[2], candidate[3]]
                zone_status = {"applied": True, "reason": None}

    # Selected POPULATED group frames sort in place: their members are
    # fitted into the frame's own interior and the frame is never moved
    # or resized (it is the user's decision). Their ids leave the normal
    # scoped batch so nothing is laid out twice.
    frames = _validated_frames(options.pop("frames", None))
    frames_workflow = workflow
    scope_ids = options.pop("scope_ids", None)
    run_main = True
    if frames and scope_ids:
        frame_ids = {str(i) for f in frames for i in f["ids"]}
        scope_ids = [i for i in scope_ids if str(i) not in frame_ids]
        if not scope_ids:
            run_main = False  # pure frame job: nothing else may move
    if scope_ids:
        workflow, scope_map = _scoped_workflow(workflow, scope_ids)
        if group_index_map is None:
            group_index_map = scope_map
        else:
            group_index_map = {new: group_index_map[mid]
                               for new, mid in scope_map.items()}
    style = STYLE_PRESETS.get(str(options.pop("style", "") or "").lower())
    if style:
        options = {**style,
                   **{k: v for k, v in options.items() if v is not None}}
    if run_main:
        result = compute_layout(workflow, options)
    else:
        result = {"positions": {}, "groups": [], "new_groups": [],
                  "reroutes": {}}
    if group_index_map is not None:
        # Filtered copies renumber groups from 0; translate frame updates
        # back to the live graph's group indices.
        result["groups"] = [
            {**u, "index": group_index_map[u["index"]]}
            for u in result.get("groups") or []
            if u["index"] in group_index_map
        ]
    if zone_rect is not None and not result.get("positions"):
        # Report honestly instead of pretending the zone was used.
        zone_status = {"applied": False,
                       "reason": "nothing was placed into the zone"}
        zone_rect = None
    if zone_rect is not None:
        # Land the reshaped content at the drawn box's corner (snapped so
        # the grid alignment survives). Sizes are never scaled: content
        # larger than the box overflows right/down at the box's ratio.
        min_x = min(
            [p[0] for p in result["positions"].values()]
            + [u["bounding"][0] for u in result.get("groups") or []]
            + [g["bounding"][0] for g in result.get("new_groups") or []])
        min_y = min(
            [p[1] - TITLE_HEIGHT for p in result["positions"].values()]
            + [u["bounding"][1] for u in result.get("groups") or []]
            + [g["bounding"][1] for g in result.get("new_groups") or []])
        dx = round((zone_rect[0] - min_x) / 10.0) * 10.0
        dy = round((zone_rect[1] - min_y) / 10.0) * 10.0
        if dx or dy:
            for p in result["positions"].values():
                p[0] += dx
                p[1] += dy
            for u in (result.get("groups") or []) + (result.get("new_groups")
                                                     or []):
                u["bounding"][0] += dx
                u["bounding"][1] += dy
            for p in (result.get("reroutes") or {}).values():
                p[0] += dx
                p[1] += dy
    if zone_status is not None:
        result["zone"] = zone_status
    if frames:
        # Interiors are compound content: cluster is the mode that lays
        # them out inside a fixed box (inner would be a no-op for the
        # mostly-ungrouped members, refit would scatter them).
        fit_options = {k: v for k, v in options.items()
                       if k != "zone_size"}
        fit_options["group_mode"] = "cluster"
        f_positions, f_updates, f_reroutes, f_report = _fit_frame_sorts(
            frames_workflow, frames, fit_options)
        result["positions"].update(f_positions)
        result["groups"] = (result.get("groups") or []) + f_updates
        result["reroutes"] = {**(result.get("reroutes") or {}),
                              **f_reroutes}
        result["frames"] = {"count": len(frames), **f_report}
    # Frame updates are index-based; the frontend compares this against
    # the live graph so frames added/removed while an animation runs can
    # never receive another frame's geometry.
    result["group_count"] = full_group_count
    return result


try:
    from server import PromptServer
    from aiohttp import web
except ImportError:
    # Imported outside a running ComfyUI (e.g. unit tests): the node class
    # is still importable, only the live push/route are unavailable.
    PromptServer = None
else:
    def _reject_non_json(request):
        """CSRF hardening: browsers can fire cross-site POSTs without a
        preflight only for form/text content types; requiring the JSON
        content type (which our frontend always sends) forces CORS."""
        if request.content_type != "application/json":
            return web.json_response(
                {"error": "content-type must be application/json"},
                status=400)
        return None

    async def _layout_sort_compute(request):
        rejected = _reject_non_json(request)
        if rejected is not None:
            return rejected
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"},
                                     status=400)
        workflow = data.get("workflow") or {}
        options = data.get("options") or {}
        try:
            # Big graphs take a moment; keep the event loop free.
            result = await asyncio.get_running_loop().run_in_executor(
                None, run_layout, workflow, options
            )
        except Exception as exc:  # never take the server down over a sort
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    try:
        PromptServer.instance.routes.post("/layout_sort/compute")(
            _layout_sort_compute
        )
    except Exception as exc:  # keep the node usable even if the routes fail
        import logging
        logging.getLogger("ComfyUI-Layout-Sort").warning(
            "could not register /layout_sort routes: %s", exc
        )


class AnyType(str):
    """Wildcard type so the optional trigger input accepts any connection."""

    def __ne__(self, other):
        return False


ANY = AnyType("*")


class LayoutSort:
    """Arranges every node in the current workflow by data flow when executed."""

    CATEGORY = "utils/layout"
    FUNCTION = "sort"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "direction": (
                    ["left_to_right", "top_to_bottom"],
                    {"default": "left_to_right"},
                ),
                "layer_spacing": (
                    "INT",
                    {"default": 80, "min": 10, "max": 500, "step": 10,
                     "tooltip": "Gap between layers (columns) in pixels."},
                ),
                "node_spacing": (
                    "INT",
                    {"default": 40, "min": 10, "max": 500, "step": 10,
                     "tooltip": "Gap between nodes inside a layer in pixels."},
                ),
                "group_mode": (
                    ["cluster", "inner", "refit"],
                    {"default": "cluster",
                     "tooltip": "cluster: lay out each group as a block, then "
                                "arrange the blocks (frames never overlap). "
                                "inner: keep every group where you put it and "
                                "only tidy the nodes inside each one "
                                "(ungrouped nodes stay untouched). "
                                "refit: ignore groups while sorting, then "
                                "re-wrap each frame around its old members."},
                ),
                "style": (
                    ["flow", "grid"],
                    {"default": "flow",
                     "tooltip": "flow: centered columns — fewer crossings, "
                                "best for big graphs. grid: top-left aligned "
                                "columns snapped to the canvas grid. Node "
                                "sizes are never changed."},
                ),
                "shape": (
                    ["auto", "square", "wide", "tall"],
                    {"default": "auto",
                     "tooltip": "Target canvas proportions. square = 1:1, "
                                "wide = 2:1, tall = 1:2 — long pipelines "
                                "fold into serpentine bands, tall graphs "
                                "spread into extra columns; group interiors "
                                "follow the same ratio. auto keeps the "
                                "natural flow. Tip: select an EMPTY group "
                                "frame before sorting to fit the layout "
                                "into that drawn box instead."},
                ),
                "animate": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "trigger": (
                    ANY,
                    {"tooltip": "Optional. Connect any output here to control "
                                "when the sort runs during execution."},
                ),
            },
            "hidden": {
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Re-run on every queue: sorting is a side effect, never cached.
        return float("nan")


    def sort(self, direction, layer_spacing, node_spacing, group_mode, style,
             shape, animate, trigger=None, extra_pnginfo=None,
             unique_id=None):
        workflow = (extra_pnginfo or {}).get("workflow")
        server = getattr(PromptServer, "instance", None) if PromptServer else None
        if not workflow or server is None:
            return {}
        result = run_layout(
            workflow,
            {
                "direction": direction,
                "h_spacing": layer_spacing,
                "v_spacing": node_spacing,
                "group_mode": group_mode,
                "style": style,
                "shape": shape,
            },
        )
        # Target the client that queued this prompt; fall back to broadcast.
        sid = getattr(server, "client_id", None)
        server.send_sync(WS_EVENT, {
            "positions": result["positions"],
            "groups": result["groups"],
            "reroutes": result.get("reroutes") or {},
            "animate": bool(animate),
            "source_node": unique_id,
            "group_count": result.get("group_count"),
        }, sid)
        return {}


NODE_CLASS_MAPPINGS = {
    "LayoutSort": LayoutSort,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LayoutSort": "Layout Sort (Auto Arrange Workflow)",
}
