import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const WS_EVENT = "layout_sort_apply";
const NODE_NAME = "LayoutSort";
const SORT_BUTTON = "✨ Sort now";
function findNode(id) {
    // Serialized ids are numeric at the top level, but tolerate strings.
    return app.graph.getNodeById(Number(id)) ?? app.graph.getNodeById(id);
}

function collectMoves(positions) {
    const moves = [];
    for (const [id, pos] of Object.entries(positions ?? {})) {
        const node = findNode(id);
        if (!node || !Array.isArray(pos)) continue;
        moves.push({ node, from: [node.pos[0], node.pos[1]], to: pos });
    }
    return moves;
}

function graphGroups() {
    return app.graph._groups ?? app.graph.groups ?? [];
}

function applyGroups(groups) {
    const existing = graphGroups();
    for (const update of groups ?? []) {
        const group = existing[update.index];
        const b = update.bounding;
        if (!group || !Array.isArray(b) || b.length < 4) continue;
        group.pos = [b[0], b[1]];
        group.size = [b[2], b[3]];
    }
}

function applyReroutes(reroutes) {
    // Native reroute points (graph.reroutes Map). reroute.move() also
    // syncs the Vue layout store used for hit-testing; fall back to a
    // plain pos assignment on frontends without it.
    const map = app.graph.reroutes;
    if (!map?.get) return;
    for (const [id, pos] of Object.entries(reroutes ?? {})) {
        const reroute = map.get(Number(id));
        if (!reroute || !Array.isArray(pos)) continue;
        try {
            if (typeof reroute.move === "function") {
                reroute.move(pos[0] - reroute.pos[0], pos[1] - reroute.pos[1]);
            } else {
                reroute.pos = [pos[0], pos[1]];
            }
        } catch (err) {
            console.warn("[LayoutSort] could not move reroute", id, err);
        }
    }
}

function applyLayout({ positions, groups, reroutes, animate, group_count }) {
    const moves = collectMoves(positions);
    if (!moves.length) return 0;
    // A broadcast event may reach a tab showing a different workflow;
    // only apply (or toast) when the ids clearly belong to this graph.
    const total = Object.keys(positions ?? {}).length;
    if (total > 0 && moves.length / total < 0.9) {
        console.warn(`[LayoutSort] ignoring layout for a different graph (${moves.length}/${total} ids matched)`);
        return 0;
    }
    const displaced = moves.filter((m) => Math.abs(m.to[0] - m.from[0]) > 0.5
        || Math.abs(m.to[1] - m.from[1]) > 0.5).length;
    // One sort = one undo step: the frontend's change tracker snapshots
    // around beforeChange/afterChange, so Ctrl+Z restores the pre-sort
    // layout in a single stroke.
    app.graph.beforeChange?.();

    const finish = () => {
        for (const m of moves) {
            m.node.pos[0] = m.to[0];
            m.node.pos[1] = m.to[1];
        }
        // Frame updates target groups by array index, so they are only
        // safe while the group list still matches the graph that was
        // sorted (the user can add/remove frames during the animation).
        if (typeof group_count !== "number"
                || graphGroups().length === group_count) {
            applyGroups(groups);
        } else {
            console.warn("[LayoutSort] group frames changed during the "
                + "sort; skipping frame updates");
        }
        applyReroutes(reroutes);
        app.graph.afterChange?.();
        app.graph.setDirtyCanvas(true, true);
    };

    if (!animate) {
        finish();
        return displaced;
    }

    const duration = 350;
    const start = performance.now();
    const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
    const frame = (now) => {
        const t = Math.min(1, (now - start) / duration);
        const k = easeOutCubic(t);
        for (const m of moves) {
            m.node.pos[0] = m.from[0] + (m.to[0] - m.from[0]) * k;
            m.node.pos[1] = m.from[1] + (m.to[1] - m.from[1]) * k;
        }
        app.graph.setDirtyCanvas(true, true);
        if (t < 1) {
            requestAnimationFrame(frame);
        } else {
            finish();
        }
    };
    requestAnimationFrame(frame);
    return displaced;
}

function widgetValue(node, name, fallback) {
    return node?.widgets?.find((w) => w.name === name)?.value ?? fallback;
}

// Defaults for sorts started without a LayoutSort node on the canvas
// (shortcut, right-click menu, selection toolbox). A node on the canvas
// still wins, so per-workflow settings keep working.
const SETTING_PREFIX = "LayoutSort.";
const SETTINGS = [
    { key: "direction", widget: "direction", name: "Flow direction",
      type: "combo", options: ["left_to_right", "top_to_bottom"],
      defaultValue: "left_to_right" },
    { key: "layer_spacing", widget: "layer_spacing",
      name: "Gap between columns (px)", type: "number",
      attrs: { min: 10, max: 500, step: 10 }, defaultValue: 80 },
    { key: "node_spacing", widget: "node_spacing",
      name: "Gap between nodes (px)", type: "number",
      attrs: { min: 10, max: 500, step: 10 }, defaultValue: 40 },
    { key: "group_mode", widget: "group_mode", name: "Group handling",
      type: "combo", options: ["cluster", "inner", "refit"],
      defaultValue: "cluster",
      tooltip: "cluster: groups become blocks · inner: groups stay where "
          + "they are, only their insides are tidied · refit: ignore groups" },
    { key: "style", widget: "style", name: "Column alignment",
      type: "combo", options: ["flow", "grid"], defaultValue: "flow" },
    { key: "shape", widget: "shape", name: "Overall shape",
      type: "combo", options: ["auto", "square", "wide", "tall"],
      defaultValue: "auto" },
    { key: "animate", widget: "animate", name: "Animate node moves",
      type: "boolean", defaultValue: true },
];

function settingValue(key, fallback) {
    try {
        const v = app.extensionManager?.setting?.get?.(SETTING_PREFIX + key);
        return v ?? fallback;
    } catch (err) {
        return fallback;
    }
}

function option(node, key, fallback) {
    const spec = SETTINGS.find((s) => s.key === key);
    const fromNode = node?.widgets?.find((w) => w.name === (spec?.widget ?? key));
    if (fromNode && fromNode.value !== undefined && fromNode.value !== null) {
        return fromNode.value;
    }
    return settingValue(key, spec?.defaultValue ?? fallback);
}

function renderedSizes(workflow) {
    // Nodes 2.0 (Vue) renders many nodes taller than their stored size;
    // lay out with what is actually on screen so nothing overlaps.
    // Only the compute copy is patched — real node sizes never change.
    const scale = app.canvas?.ds?.scale;
    if (!scale) return;
    for (const n of workflow.nodes ?? []) {
        const el = document.querySelector(`[data-node-id="${n.id}"]`);
        if (!el || !Array.isArray(n.size)) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const w = r.width / scale;
        const bodyH = r.height / scale - 30;
        if (w > n.size[0] + 2) n.size[0] = Math.ceil(w);
        if (!n.flags?.collapsed && bodyH > n.size[1] + 2) {
            n.size[1] = Math.ceil(bodyH);
        }
    }
}

function isGroupItem(item) {
    return !!item && (typeof item.recomputeInsideNodes === "function"
        || item.constructor?.name === "LGraphGroup");
}

function selectedNodes() {
    // Selected nodes across frontend generations: the legacy
    // selected_nodes map and the newer selectedItems set (which mixes
    // nodes and group frames).
    const canvas = app.canvas;
    const found = new Map();
    const sel = canvas?.selected_nodes;
    if (sel) {
        for (const key of Object.keys(sel)) {
            const n = sel[key];
            if (n?.id != null) found.set(n.id, n);
        }
    }
    canvas?.selectedItems?.forEach?.((item) => {
        if (!isGroupItem(item) && item?.id != null && item.pos) {
            found.set(item.id, item);
        }
    });
    return [...found.values()];
}

function selectedGroups() {
    const found = [];
    app.canvas?.selectedItems?.forEach?.((item) => {
        if (isGroupItem(item)) found.push(item);
    });
    const legacy = app.canvas?.selected_group;
    if (legacy && isGroupItem(legacy) && !found.includes(legacy)) {
        found.push(legacy);
    }
    return found;
}

function nodeVisualCenter(n) {
    // Mirrors the backend's membership rule exactly (visual rect incl.
    // the title bar; collapsed nodes render at roughly title width).
    const collapsed = !!n.flags?.collapsed;
    const w = collapsed ? Math.min(n.size?.[0] ?? 1, 160)
                        : Math.max(n.size?.[0] ?? 1, 1);
    const bodyH = collapsed ? 0 : Math.max(n.size?.[1] ?? 1, 1);
    return [n.pos[0] + w / 2, n.pos[1] - 30 + (bodyH + 30) / 2];
}

function groupRect(group) {
    return [group.pos[0], group.pos[1], group.size[0], group.size[1]];
}

function groupMembers(group) {
    // Geometric membership (center inside the frame) instead of
    // LiteGraph's _nodes cache — identical across frontend generations
    // and to the backend's rule.
    const [gx, gy, gw, gh] = groupRect(group);
    const members = [];
    for (const n of app.graph?._nodes ?? []) {
        if (n?.id == null || !n.pos) continue;
        const [cx, cy] = nodeVisualCenter(n);
        if (cx >= gx && cx <= gx + gw && cy >= gy && cy <= gy + gh) {
            members.push(n);
        }
    }
    return members;
}

function selectionScope() {
    // Node ids the sort should be limited to: every selected node plus
    // the members of every selected group frame.
    const ids = new Set(selectedNodes().map((n) => n.id));
    for (const group of selectedGroups()) {
        for (const n of groupMembers(group)) {
            if (n?.id != null) ids.add(n.id);
        }
    }
    return [...ids];
}

function selectedZone() {
    // A selected EMPTY group frame is a drawn target zone: the sort
    // shapes the layout to its proportions and lands it at its corner.
    // The index is a hint only — the backend re-resolves the frame by
    // its rectangle, so a proxy-broken indexOf (-1) still works.
    for (const group of selectedGroups()) {
        if (groupMembers(group).length) continue;
        return { rect: groupRect(group),
                 index: graphGroups().indexOf(group) };
    }
    return null;
}

// --- pure geometry for the align/distribute tools (extracted by tests) ---
// rects: [{id, x, y, w, h}] visual bounds. Returns {id: [dx, dy]}.
function computeAlignDeltas(rects, op) {
    if (rects.length < 2) return {};
    const minX = Math.min(...rects.map((r) => r.x));
    const maxR = Math.max(...rects.map((r) => r.x + r.w));
    const minY = Math.min(...rects.map((r) => r.y));
    const maxB = Math.max(...rects.map((r) => r.y + r.h));
    const deltas = {};
    for (const r of rects) {
        let dx = 0;
        let dy = 0;
        if (op === "left") dx = minX - r.x;
        else if (op === "right") dx = maxR - (r.x + r.w);
        else if (op === "top") dy = minY - r.y;
        else if (op === "bottom") dy = maxB - (r.y + r.h);
        else if (op === "center_h") dx = (minX + maxR) / 2 - (r.x + r.w / 2);
        else if (op === "center_v") dy = (minY + maxB) / 2 - (r.y + r.h / 2);
        deltas[r.id] = [Math.round(dx), Math.round(dy)];
    }
    return deltas;
}

function computeDistributeDeltas(rects, axis) {
    // Equal GAPS between visual rects (not equal centers); the outermost
    // two stay fixed. axis: "h" | "v".
    if (rects.length < 3) return {};
    const pos = axis === "h" ? "x" : "y";
    const len = axis === "h" ? "w" : "h";
    const ordered = [...rects].sort((a, b) => a[pos] - b[pos]);
    const first = ordered[0];
    const last = ordered[ordered.length - 1];
    const span = last[pos] + last[len] - first[pos];
    const total = ordered.reduce((sum, r) => sum + r[len], 0);
    const gap = (span - total) / (ordered.length - 1);
    const deltas = {};
    let cursor = first[pos];
    for (const r of ordered) {
        const d = Math.round(cursor - r[pos]);
        deltas[r.id] = axis === "h" ? [d, 0] : [0, d];
        cursor += r[len] + gap;
    }
    return deltas;
}
// --- end pure geometry ---

function visualBound(node) {
    if (typeof node.getBounding === "function") {
        const b = node.getBounding();
        return { id: node.id, x: b[0], y: b[1], w: b[2], h: b[3] };
    }
    return { id: node.id, x: node.pos[0], y: node.pos[1] - 30,
             w: node.size[0], h: node.size[1] + 30 };
}

function toolToast(detail, severity = "info") {
    app.extensionManager?.toast?.add?.({
        severity, summary: "Layout Sort", detail, life: 3000,
    });
}

function applyDeltas(nodes, deltas) {
    if (!Object.keys(deltas).length) return false;
    app.graph.beforeChange?.();
    for (const n of nodes) {
        const d = deltas[n.id];
        if (!d) continue;
        n.pos[0] += d[0];
        n.pos[1] += d[1];
    }
    app.graph.afterChange?.();
    app.graph.setDirtyCanvas(true, true);
    return true;
}

function alignSelected(op) {
    const nodes = selectedNodes();
    if (nodes.length < 2) {
        toolToast("Select 2+ nodes to align.");
        return;
    }
    applyDeltas(nodes, computeAlignDeltas(nodes.map(visualBound), op));
}

function distributeSelected(axis) {
    const nodes = selectedNodes();
    if (nodes.length < 3) {
        toolToast("Select 3+ nodes to distribute.");
        return;
    }
    applyDeltas(nodes, computeDistributeDeltas(nodes.map(visualBound), axis));
}

function findSortNode() {
    return app.graph?._nodes?.find((n) => n.type === NODE_NAME) ?? null;
}

async function sortNow(node, mode = {}) {
    // Also runs from the shortcut, the right-click menu and the selection
    // toolbox with no LayoutSort node on the canvas (node = null): options
    // then come from Settings → Layout Sort.
    //   mode.whole  — ignore the selection, sort the whole workflow
    //   mode.group  — tidy inside this one group frame (frame kept)
    const busyHolder = node ?? sortNow;
    if (busyHolder.__layoutSortBusy) return;
    busyHolder.__layoutSortBusy = true;
    const workflow = app.graph.serialize();
    renderedSizes(workflow);
    const options = {
        direction: option(node, "direction", "left_to_right"),
        h_spacing: option(node, "layer_spacing", 80),
        v_spacing: option(node, "node_spacing", 40),
        group_mode: option(node, "group_mode", "cluster"),
        style: option(node, "style", "flow"),
        shape: option(node, "shape", "auto"),
    };
    const groups = mode.group ? [mode.group]
        : mode.whole ? [] : selectedGroups();
    if (!mode.whole && !mode.group) {
        // 2+ selected nodes (or selected group frames) = sort only
        // those, anchored where the selection sits; the rest stays put.
        const scope = selectionScope();
        if (scope.length >= 2) options.scope_ids = scope;
        // A selected EMPTY group frame = drawn zone to fit into.
        const zone = selectedZone();
        if (zone) {
            options.zone = zone.rect;
            options.zone_index = zone.index;
        }
    }
    // Selected POPULATED group frames sort in place: members re-arrange
    // inside the frame, the frame itself keeps its exact size/position.
    const frames = [];
    for (const group of groups) {
        const members = groupMembers(group);
        if (!members.length) continue;
        frames.push({ rect: groupRect(group), title: group.title,
                      ids: members.map((n) => n.id) });
    }
    if (frames.length) {
        options.frames = frames;
        if (mode.group) options.scope_ids = frames[0].ids;
    } else if (mode.group) {
        toolToast("This group frame is empty — nothing to tidy.");
        busyHolder.__layoutSortBusy = false;
        return;
    }
    try {
        const res = await api.fetchApi("/layout_sort/compute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow, options }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const result = await res.json();
        applyLayout({
            positions: result.positions,
            groups: result.groups,
            reroutes: result.reroutes,
            animate: option(node, "animate", true),
            group_count: result.group_count,
        });
        const moved = Object.keys(result.positions ?? {}).length;
        if (options.frames) {
            const rep = result.frames ?? {};
            const done = options.frames.length
                - (rep.unchanged?.length ?? 0);
            if (done > 0) {
                toolToast(`Tidied ${done} group(s) inside their frames `
                    + "(frame size kept) — Ctrl+Z to undo.", "success");
            }
            if (rep.adjusted?.length) {
                toolToast(`Spacing/direction adjusted so it fits: `
                    + rep.adjusted.join(", "));
            }
            if (rep.unchanged?.length) {
                toolToast(`Left as is (a tidy layout can't fit the frame): `
                    + `${rep.unchanged.join(", ")} — enlarge the frame `
                    + "to tidy it.", "warn");
            }
            if (rep.overflow?.length) {
                toolToast("Content overflows: "
                    + `${rep.overflow.join(", ")} — enlarge the frame `
                    + "or reduce spacing.", "warn");
            }
        } else if (options.scope_ids) {
            toolToast(`Sorted ${moved} selected node(s); the rest stayed `
                + "put — Ctrl+Z to undo.", "success");
        } else if (!options.zone) {
            const g = result.groups?.length ?? 0;
            toolToast(`Sorted ${moved} node(s)`
                + (g ? ` and ${g} group frame(s)` : "")
                + " — Ctrl+Z to undo.", "success");
        }
        if (options.zone) {
            // Truthful feedback: the backend reports whether the zone
            // was actually matched and used.
            if (result.zone?.applied) {
                toolToast("Arranged into the drawn zone (frame kept as is).");
            } else {
                toolToast(`Zone not used: ${result.zone?.reason
                    ?? "the drawn frame could not be matched"}`, "warn");
            }
        }
    } catch (err) {
        console.error("[LayoutSort] sort request failed:", err);
        app.extensionManager?.toast?.add?.({
            severity: "error",
            summary: "Layout Sort",
            detail: `Sort failed: ${err}`,
            life: 6000,
        });
    } finally {
        busyHolder.__layoutSortBusy = false;
    }
}

const COMBO_VALUES = {
    direction: ["left_to_right", "top_to_bottom"],
    group_mode: ["cluster", "inner", "refit"],
    style: ["flow", "grid"],
    shape: ["auto", "square", "wide", "tall"],
};

function sanitizeWidgets(node) {
    // Workflows saved by older node versions restore widget values
    // positionally, so a value can land in the wrong slot. Repair
    // anything implausible; leftover values of removed widgets (the old
    // LLM options) are simply ignored.
    const get = (name) => node.widgets?.find((w) => w.name === name);
    for (const [name, values] of Object.entries(COMBO_VALUES)) {
        const widget = get(name);
        if (widget && !values.includes(widget.value)) widget.value = values[0];
    }
    for (const [name, fallback] of [["layer_spacing", 80], ["node_spacing", 40]]) {
        const widget = get(name);
        if (widget && !(typeof widget.value === "number"
                && widget.value >= 10)) {
            widget.value = fallback;
        }
    }
    const animateWidget = get("animate");
    if (animateWidget && typeof animateWidget.value !== "boolean") {
        animateWidget.value = true;
    }
    // Buttons carry no value; an old save can drop a removed widget's
    // value (e.g. an LLM prompt) onto one positionally.
    for (const w of node.widgets ?? []) {
        if (w.type === "button") w.value = null;
    }
}

function sortWhole() {
    sortNow(findSortNode(), { whole: true });
}

function sortSelectionOrWhole() {
    sortNow(findSortNode());
}

function layoutMenuItems(canvas) {
    // Right-click menu entries: the discoverable way in, no node or
    // shortcut knowledge needed.
    const items = [null];
    const graph = canvas?.graph ?? app.graph;
    let group = null;
    try {
        group = graph?.getGroupOnPos?.(canvas.graph_mouse[0],
                                       canvas.graph_mouse[1]) ?? null;
    } catch (err) { group = null; }
    if (group && groupMembers(group).length) {
        items.push({
            content: "📐 Tidy inside this group (keep frame size)",
            callback: () => sortNow(findSortNode(), { group }),
        });
    }
    const selected = selectionScope().length;
    const sub = [
        { content: "Sort whole workflow", callback: sortWhole },
    ];
    if (selected >= 2) {
        sub.push({ content: `Sort selected only (${selected} nodes)`,
                   callback: sortSelectionOrWhole });
    }
    if (selectedNodes().length >= 2) {
        sub.push(null,
            { content: "Align left", callback: () => alignSelected("left") },
            { content: "Align right", callback: () => alignSelected("right") },
            { content: "Align top", callback: () => alignSelected("top") },
            { content: "Align bottom", callback: () => alignSelected("bottom") },
            { content: "Center horizontally", callback: () => alignSelected("center_h") },
            { content: "Center vertically", callback: () => alignSelected("center_v") });
    }
    if (selectedNodes().length >= 3) {
        sub.push(
            { content: "Distribute horizontally", callback: () => distributeSelected("h") },
            { content: "Distribute vertically", callback: () => distributeSelected("v") });
    }
    items.push({ content: "🧹 Layout Sort", has_submenu: true,
                 submenu: { options: sub } });
    return items;
}

app.registerExtension({
    name: "comfyui.layout.sort",
    settings: SETTINGS.map((spec) => ({
        id: SETTING_PREFIX + spec.key,
        category: ["Layout Sort", "Defaults", spec.name],
        name: spec.name,
        type: spec.type,
        defaultValue: spec.defaultValue,
        options: spec.options,
        attrs: spec.attrs,
        tooltip: (spec.tooltip ? spec.tooltip + " — " : "")
            + "Used by the shortcut and right-click menu when the "
            + "workflow has no Layout Sort node (a node's own widgets win).",
    })),
    // All ops live in the command palette and are rebindable in ComfyUI's
    // keybinding settings; three ship with defaults that avoid the stock
    // shortcuts.
    commands: [
        { id: "layoutSort.sort", icon: "pi pi-sitemap",
          label: "Layout Sort: sort selection (or whole graph)",
          function: sortSelectionOrWhole },
        { id: "layoutSort.sortWhole", icon: "pi pi-sitemap",
          label: "Layout Sort: sort whole workflow (ignore selection)",
          function: sortWhole },
        { id: "layoutSort.alignLeft", icon: "pi pi-align-left",
          label: "Layout Sort: align left",
          function: () => alignSelected("left") },
        { id: "layoutSort.alignRight", icon: "pi pi-align-right",
          label: "Layout Sort: align right",
          function: () => alignSelected("right") },
        { id: "layoutSort.alignTop", icon: "pi pi-arrow-up",
          label: "Layout Sort: align top",
          function: () => alignSelected("top") },
        { id: "layoutSort.alignBottom", icon: "pi pi-arrow-down",
          label: "Layout Sort: align bottom",
          function: () => alignSelected("bottom") },
        { id: "layoutSort.centerHorizontal", icon: "pi pi-align-center",
          label: "Layout Sort: center on vertical axis",
          function: () => alignSelected("center_h") },
        { id: "layoutSort.centerVertical", icon: "pi pi-align-justify",
          label: "Layout Sort: center on horizontal axis",
          function: () => alignSelected("center_v") },
        { id: "layoutSort.distributeHorizontal", icon: "pi pi-arrows-h",
          label: "Layout Sort: distribute horizontally (equal gaps)",
          function: () => distributeSelected("h") },
        { id: "layoutSort.distributeVertical", icon: "pi pi-arrows-v",
          label: "Layout Sort: distribute vertically (equal gaps)",
          function: () => distributeSelected("v") },
    ],
    keybindings: [
        { combo: { key: "s", alt: true, shift: true },
          commandId: "layoutSort.sort" },
        { combo: { key: "h", alt: true, shift: true },
          commandId: "layoutSort.distributeHorizontal" },
        { combo: { key: "v", alt: true, shift: true },
          commandId: "layoutSort.distributeVertical" },
    ],
    getCanvasMenuItems(canvas) {
        return layoutMenuItems(canvas);
    },
    getSelectionToolboxCommands() {
        // One-click sort in the floating toolbox shown over a selection.
        return ["layoutSort.sort"];
    },
    setup() {
        api.addEventListener(WS_EVENT, ({ detail }) => {
            // Sorts triggered by running the workflow (the node executed).
            const moved = applyLayout(detail ?? {});
            if (moved > 0) {
                toolToast(`Layout Sort node ran: ${moved} node(s) moved — `
                    + "Ctrl+Z to undo.", "success");
            }
        });
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_NAME) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            // Instant sort without queueing the workflow.
            this.addWidget("button", SORT_BUTTON, null, () => sortNow(this));
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            // Runs after saved widget values are applied — repair values
            // shifted by older saves before they reach the backend.
            sanitizeWidgets(this);
            // Saves from the LLM era are much taller than the node is
            // now; drop the blank space the removed widgets left behind.
            if (this.properties) delete this.properties["Show LLM options"];
            try {
                const fit = this.computeSize?.();
                if (fit && this.size[1] > fit[1] + 20) {
                    this.setSize?.([this.size[0], fit[1]]);
                }
            } catch (err) { /* keep the saved size */ }
        };
    },
});
