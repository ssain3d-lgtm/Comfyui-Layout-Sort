import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const WS_EVENT = "layout_sort_apply";
const PROGRESS_EVENT = "layout_sort_progress";
const NODE_NAME = "LayoutSort";
const SORT_BUTTON = "✨ Sort now";
const PROGRESS_STAGES = {
    llm_request: "Asking the LLM",
    llm_done: "Plan received",
    layout: "Computing layout",
};
const GROUP_COLORS = ["#3f789e", "#88677a", "#6b8a5e", "#8a7a4e", "#7a6b9e", "#5e8a8a"];

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

function createGroups(newGroups) {
    const LGraphGroup = window.LiteGraph?.LGraphGroup;
    if (!newGroups?.length || !LGraphGroup) return;
    const existing = graphGroups();
    let colorIndex = existing.length;
    for (const g of newGroups) {
        const b = g.bounding;
        if (!Array.isArray(b) || b.length < 4) continue;
        const title = g.title || "Cluster";
        // Rapid re-runs can deliver the same suggestions twice before the
        // first frames are part of the serialized graph — don't duplicate.
        if (existing.some((e) => e.title === title
                && Math.abs(e.pos[0] - b[0]) < 1 && Math.abs(e.pos[1] - b[1]) < 1)) {
            continue;
        }
        const group = new LGraphGroup();
        group.title = title;
        group.color = GROUP_COLORS[colorIndex++ % GROUP_COLORS.length];
        group.pos = [b[0], b[1]];
        group.size = [b[2], b[3]];
        app.graph.add(group);
    }
}

function notifyLlm(llm) {
    // Only present when llm_prompt was non-empty; an empty prompt runs a
    // plain deterministic sort with no LLM involved and stays silent.
    const toast = app.extensionManager?.toast;
    if (!llm) return;
    if (llm.error) {
        console.warn("[LayoutSort] LLM prompt ignored:", llm.error);
        toast?.add?.({
            severity: "warn",
            summary: "Layout Sort",
            detail: `Prompt ignored (plain sort used): ${llm.error}`,
            life: 6000,
        });
        return;
    }
    if (llm.used) {
        let detail = llm.note || "Prompt applied.";
        if (llm.clusters > 0) {
            detail += ` — ${llm.clusters} frame(s): ${(llm.names ?? []).join(", ")}`;
        }
        toast?.add?.({
            severity: "success",
            summary: "Layout Sort",
            detail,
            life: 5000,
        });
        if (llm.unsupported?.length) {
            toast?.add?.({
                severity: "warn",
                summary: "Layout Sort",
                detail: `Not possible: ${llm.unsupported.join("; ")}`,
                life: 6000,
            });
        }
    }
}

function applyLayout({ positions, groups, new_groups, reroutes, llm, animate,
                       group_count }) {
    const moves = collectMoves(positions);
    if (!moves.length) return;
    // A broadcast event may reach a tab showing a different workflow;
    // only apply (or toast) when the ids clearly belong to this graph.
    const total = Object.keys(positions ?? {}).length;
    if (total > 0 && moves.length / total < 0.9) {
        console.warn(`[LayoutSort] ignoring layout for a different graph (${moves.length}/${total} ids matched)`);
        return;
    }
    notifyLlm(llm);

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
        // sorted (the user can add/remove frames during a slow LLM
        // round-trip or the animation).
        if (typeof group_count !== "number"
                || graphGroups().length === group_count) {
            applyGroups(groups);
        } else {
            console.warn("[LayoutSort] group frames changed during the "
                + "sort; skipping frame updates");
        }
        applyReroutes(reroutes);
        createGroups(new_groups);
        app.graph.afterChange?.();
        app.graph.setDirtyCanvas(true, true);
    };

    if (!animate) {
        finish();
        return;
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
}

function widgetValue(node, name, fallback) {
    return node?.widgets?.find((w) => w.name === name)?.value ?? fallback;
}

function isGroupItem(item) {
    return typeof item?.recomputeInsideNodes === "function";
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

function selectionScope() {
    // Node ids the sort should be limited to: every selected node plus
    // the members of every selected group frame.
    const ids = new Set(selectedNodes().map((n) => n.id));
    app.canvas?.selectedItems?.forEach?.((item) => {
        if (!isGroupItem(item)) return;
        try {
            item.recomputeInsideNodes();
        } catch (err) { /* membership stays as last computed */ }
        for (const n of item._nodes ?? item.nodes ?? []) {
            if (n?.id != null) ids.add(n.id);
        }
    });
    return [...ids];
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

// A sort in flight turns the node's Sort button into a live progress
// label — stage + elapsed seconds — so a slow LLM round-trip (up to
// 2 min) never looks stuck. True percentages are impossible (an LLM's
// remaining time is unknowable), but stage + elapsed is honest.
let progressState = null;

function renderProgress() {
    if (!progressState?.button) return;
    const seconds =
        Math.floor((performance.now() - progressState.startedAt) / 1000);
    progressState.button.name = `⏳ ${progressState.stage}… ${seconds}s`;
    progressState.node?.setDirtyCanvas?.(true, false);
}

function beginProgress(node, hasPrompt) {
    const button =
        node?.widgets?.find((w) => w.name === SORT_BUTTON) ?? null;
    progressState = {
        node,
        button,
        stage: hasPrompt ? PROGRESS_STAGES.llm_request : "Sorting",
        startedAt: performance.now(),
        timer: button ? setInterval(renderProgress, 500) : null,
    };
    if (button) renderProgress();
    else if (hasPrompt) {
        // Command-palette run with no LayoutSort node to write on.
        toolToast("Asking the LLM… this can take a while.");
    }
}

function endProgress() {
    if (!progressState) return;
    if (progressState.timer) clearInterval(progressState.timer);
    if (progressState.button) {
        progressState.button.name = SORT_BUTTON;
        progressState.node?.setDirtyCanvas?.(true, false);
    }
    progressState = null;
}

function onProgressEvent(stage) {
    if (!progressState) return; // another tab's sort — not ours
    const label = PROGRESS_STAGES[stage];
    if (label) {
        progressState.stage = label;
        renderProgress();
    }
}

async function sortNow(node) {
    // Also runs from the command palette with no LayoutSort node on the
    // canvas (node = null): widget reads fall back to defaults.
    const busyHolder = node ?? sortNow;
    if (busyHolder.__layoutSortBusy) return;
    busyHolder.__layoutSortBusy = true;
    beginProgress(node,
        String(widgetValue(node, "llm_prompt", "")).trim().length > 0);
    const workflow = app.graph.serialize();
    const options = {
        direction: widgetValue(node, "direction", "left_to_right"),
        h_spacing: widgetValue(node, "layer_spacing", 80),
        v_spacing: widgetValue(node, "node_spacing", 40),
        group_mode: widgetValue(node, "group_mode", "cluster"),
        style: widgetValue(node, "style", "flow"),
    };
    // 2+ selected nodes (or selected group frames) = sort only those,
    // anchored where the selection sits; everything else stays put.
    const scope = selectionScope();
    if (scope.length >= 2) options.scope_ids = scope;
    // The API key is deliberately absent here: it lives server-side only
    // (key dialog / env var) and must never enter the graph or this payload.
    const llm = {
        prompt: widgetValue(node, "llm_prompt", ""),
        provider: widgetValue(node, "llm_provider", "lmstudio"),
        base_url: widgetValue(node, "llm_base_url", ""),
        model: widgetValue(node, "llm_model", ""),
        max_tokens: widgetValue(node, "llm_max_tokens", 4096),
    };
    try {
        const res = await api.fetchApi("/layout_sort/compute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow, options, llm }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const result = await res.json();
        applyLayout({
            positions: result.positions,
            groups: result.groups,
            new_groups: result.new_groups,
            reroutes: result.reroutes,
            llm: result.llm,
            animate: widgetValue(node, "animate", true),
            group_count: result.group_count,
        });
        if (options.scope_ids) {
            const moved = Object.keys(result.positions ?? {}).length;
            toolToast(`Sorted ${moved} selected node(s); the rest stayed put.`);
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
        endProgress();
        busyHolder.__layoutSortBusy = false;
    }
}

const KEY_BUTTON_UNSET = "🔑 LLM API key";
const KEY_BUTTON_SET = "🔑 LLM API key ✓";

function promptApiKey() {
    // Masked input rendered locally; the value is sent once to the backend
    // and never stored client-side (no widget, no localStorage, no graph).
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.style.cssText =
            "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;" +
            "display:flex;align-items:center;justify-content:center";
        const box = document.createElement("div");
        box.style.cssText =
            "background:#353535;color:#ddd;padding:20px;border-radius:8px;" +
            "min-width:340px;font-family:sans-serif;font-size:13px";
        box.innerHTML = `
            <div style="margin-bottom:10px;font-weight:600">LLM API key</div>
            <input type="password" placeholder="LM Studio 기본 설정은 비워두세요"
                style="width:100%;padding:7px;box-sizing:border-box;background:#222;
                       color:#eee;border:1px solid #555;border-radius:4px">
            <div style="font-size:11px;margin-top:8px;opacity:.7;line-height:1.5">
                서버에만 저장됩니다 — 워크플로우 JSON·PNG 메타데이터에는
                절대 기록되지 않습니다.</div>
            <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">
                <button data-a="clear">Clear</button>
                <button data-a="cancel">Cancel</button>
                <button data-a="save">Save</button>
            </div>`;
        for (const btn of box.querySelectorAll("button")) {
            btn.style.cssText =
                "padding:5px 14px;border-radius:4px;border:1px solid #555;" +
                "background:#444;color:#eee;cursor:pointer";
        }
        const input = box.querySelector("input");
        const close = (result) => {
            overlay.remove();
            resolve(result);
        };
        box.addEventListener("click", (e) => {
            const action = e.target?.dataset?.a;
            if (action === "save") close({ key: input.value.trim() });
            else if (action === "clear") close({ key: "" });
            else if (action === "cancel") close(null);
        });
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") close({ key: input.value.trim() });
            else if (e.key === "Escape") close(null);
        });
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) close(null);
        });
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        input.focus();
    });
}

function keyButton(node) {
    return node.widgets?.find(
        (w) => w.name === KEY_BUTTON_UNSET || w.name === KEY_BUTTON_SET);
}

async function refreshKeyStatus(node) {
    try {
        const res = await api.fetchApi("/layout_sort/api_key");
        if (!res.ok) return;
        const status = await res.json();
        const button = keyButton(node);
        if (button) {
            button.name = status.configured ? KEY_BUTTON_SET : KEY_BUTTON_UNSET;
            node.setDirtyCanvas?.(true, false);
        }
    } catch (err) {
        console.warn("[LayoutSort] key status unavailable:", err);
    }
}

async function manageApiKey(node) {
    const result = await promptApiKey();
    if (!result) return;
    try {
        const res = await api.fetchApi("/layout_sort/api_key", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                api_key: result.key,
                // Bind the key to the server this node points at; it will
                // only ever be sent there (or to loopback addresses).
                provider: widgetValue(node, "llm_provider", "lmstudio"),
                origin_hint: widgetValue(node, "llm_base_url", ""),
            }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const status = await res.json();
        app.extensionManager?.toast?.add?.({
            severity: "success",
            summary: "Layout Sort",
            detail: status.configured
                ? "API key saved on the server (never written into workflows)."
                : "API key cleared.",
            life: 4000,
        });
    } catch (err) {
        console.error("[LayoutSort] saving API key failed:", err);
        app.extensionManager?.toast?.add?.({
            severity: "error",
            summary: "Layout Sort",
            detail: `Saving API key failed: ${err}`,
            life: 6000,
        });
    }
    refreshKeyStatus(node);
}

const MODEL_AUTO = "auto";
const PROVIDER_URLS = {
    lmstudio: "http://127.0.0.1:1234/v1",
    ollama: "http://127.0.0.1:11434/v1",
    openai: "https://api.openai.com/v1",
    anthropic: "https://api.anthropic.com",
};

const COMBO_VALUES = {
    direction: ["left_to_right", "top_to_bottom"],
    group_mode: ["cluster", "inner", "refit"],
    style: ["flow", "grid"],
    llm_provider: ["lmstudio", "ollama", "openai", "anthropic", "custom"],
};

function sanitizeWidgets(node) {
    // Workflows saved by older node versions restore widget values
    // positionally, shifting them one slot when widgets were added (e.g.
    // a URL lands in llm_provider and "auto" in llm_base_url, producing
    // http://auto -> getaddrinfo failures). Repair anything implausible.
    const get = (name) => node.widgets?.find((w) => w.name === name);
    for (const [name, values] of Object.entries(COMBO_VALUES)) {
        const widget = get(name);
        if (!widget || values.includes(widget.value)) continue;
        if (name === "llm_provider" && /^https?:\/\//i.test(String(widget.value))) {
            const baseUrl = get("llm_base_url");
            if (baseUrl) baseUrl.value = String(widget.value);
            widget.value = "custom";
        } else {
            widget.value = values[0];
        }
    }
    const promptWidget = get("llm_prompt");
    if (promptWidget) {
        // Positional restores from every earlier widget vintage leave
        // non-prompt values here: a boolean (llm_clustering era), a
        // provider name or a base URL (older eras). None of those are a
        // layout request, and a non-empty prompt triggers an LLM call —
        // only text the user actually typed may survive.
        const v = promptWidget.value;
        const s = typeof v === "string" ? v.trim() : null;
        if (s === null || /^https?:\/\/\S+$/i.test(s)
                || COMBO_VALUES.llm_provider.includes(s) || s === MODEL_AUTO) {
            promptWidget.value = "";
        }
    }
    const baseUrl = get("llm_base_url");
    if (baseUrl && (typeof baseUrl.value !== "string" || baseUrl.value === ""
            || baseUrl.value === "auto")) {
        baseUrl.value = PROVIDER_URLS[widgetValue(node, "llm_provider", "lmstudio")]
            ?? PROVIDER_URLS.lmstudio;
    }
    const model = get("llm_model");
    if (model && (typeof model.value !== "string" || /^\d+$/.test(model.value))) {
        model.value = MODEL_AUTO;
    }
    const tokens = get("llm_max_tokens");
    if (tokens && !(typeof tokens.value === "number" && tokens.value >= 256)) {
        tokens.value = 4096;
    }
}

function syncProviderUrl(node) {
    // Mirror the preset into llm_base_url so the UI shows the endpoint
    // actually used ("custom" keeps whatever the user typed).
    const provider = node.widgets?.find((w) => w.name === "llm_provider");
    const baseUrl = node.widgets?.find((w) => w.name === "llm_base_url");
    if (!provider || !baseUrl) return;
    const previous = provider.callback;
    provider.callback = function (value, ...rest) {
        previous?.call(this, value, ...rest);
        const preset = PROVIDER_URLS[value];
        if (preset) {
            baseUrl.value = preset;
            node.setDirtyCanvas?.(true, false);
        }
    };
}

function modelWidget(node) {
    // Declared as a COMBO in Python (initial options: ["auto"]); the
    // node's VALIDATE_INPUTS skips list-membership validation, so this
    // widget only needs its options.values refreshed.
    return node.widgets?.find((w) => w.name === "llm_model");
}

async function connectModels(node) {
    const toast = app.extensionManager?.toast;
    try {
        const res = await api.fetchApi("/layout_sort/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider: widgetValue(node, "llm_provider", "lmstudio"),
                base_url: widgetValue(node, "llm_base_url", ""),
            }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const result = await res.json();
        if (result.error) throw new Error(result.error);
        const widget = modelWidget(node);
        if (widget) {
            widget.options = widget.options ?? {};
            widget.options.values = [MODEL_AUTO, ...(result.models ?? [])];
            if (!widget.options.values.includes(widget.value)) {
                widget.value = MODEL_AUTO;
            }
        }
        node.setDirtyCanvas?.(true, false);
        toast?.add?.({
            severity: "success",
            summary: "Layout Sort",
            detail: `Connected — ${result.models.length} model(s) available.`,
            life: 4000,
        });
    } catch (err) {
        console.error("[LayoutSort] connect failed:", err);
        toast?.add?.({
            severity: "error",
            summary: "Layout Sort",
            detail: `Could not list models: ${err}`,
            life: 6000,
        });
    }
}

app.registerExtension({
    name: "comfyui.layout.sort",
    // All ops live in the command palette and are rebindable in ComfyUI's
    // keybinding settings; three ship with defaults that avoid the stock
    // shortcuts.
    commands: [
        { id: "layoutSort.sort",
          label: "Layout Sort: sort selection (or whole graph)",
          function: () => sortNow(findSortNode()) },
        { id: "layoutSort.alignLeft", label: "Layout Sort: align left",
          function: () => alignSelected("left") },
        { id: "layoutSort.alignRight", label: "Layout Sort: align right",
          function: () => alignSelected("right") },
        { id: "layoutSort.alignTop", label: "Layout Sort: align top",
          function: () => alignSelected("top") },
        { id: "layoutSort.alignBottom", label: "Layout Sort: align bottom",
          function: () => alignSelected("bottom") },
        { id: "layoutSort.centerHorizontal",
          label: "Layout Sort: center on vertical axis",
          function: () => alignSelected("center_h") },
        { id: "layoutSort.centerVertical",
          label: "Layout Sort: center on horizontal axis",
          function: () => alignSelected("center_v") },
        { id: "layoutSort.distributeHorizontal",
          label: "Layout Sort: distribute horizontally (equal gaps)",
          function: () => distributeSelected("h") },
        { id: "layoutSort.distributeVertical",
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
    setup() {
        api.addEventListener(WS_EVENT, ({ detail }) => applyLayout(detail ?? {}));
        api.addEventListener(PROGRESS_EVENT,
            ({ detail }) => onProgressEvent(detail?.stage));
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_NAME) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            syncProviderUrl(this);
            // Instant sort without queueing the workflow.
            this.addWidget("button", SORT_BUTTON, null, () => sortNow(this));
            this.addWidget("button", "🔌 Connect (load models)", null,
                () => connectModels(this));
            this.addWidget("button", KEY_BUTTON_UNSET, null, () => manageApiKey(this));
            refreshKeyStatus(this);
        };
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            onConfigure?.apply(this, arguments);
            // Runs after saved widget values are applied — repair values
            // shifted by older saves before they reach the backend.
            sanitizeWidgets(this);
        };
    },
});
