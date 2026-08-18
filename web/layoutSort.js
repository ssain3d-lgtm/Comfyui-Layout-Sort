import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const WS_EVENT = "layout_sort_apply";
const NODE_NAME = "LayoutSort";
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
    const toast = app.extensionManager?.toast;
    if (!llm) return;
    if (llm.error) {
        console.warn("[LayoutSort] LLM clustering skipped:", llm.error);
        toast?.add?.({
            severity: "warn",
            summary: "Layout Sort",
            detail: `LLM clustering skipped: ${llm.error}`,
            life: 6000,
        });
    } else if (llm.used) {
        toast?.add?.({
            severity: "success",
            summary: "Layout Sort",
            detail: `LLM suggested ${llm.clusters} clusters: ${(llm.names ?? []).join(", ")}`,
            life: 5000,
        });
    }
}

function applyLayout({ positions, groups, new_groups, llm, animate }) {
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

    const finish = () => {
        for (const m of moves) {
            m.node.pos[0] = m.to[0];
            m.node.pos[1] = m.to[1];
        }
        applyGroups(groups);
        createGroups(new_groups);
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
    return node.widgets?.find((w) => w.name === name)?.value ?? fallback;
}

async function sortNow(node) {
    if (node.__layoutSortBusy) return;
    node.__layoutSortBusy = true;
    const workflow = app.graph.serialize();
    const options = {
        direction: widgetValue(node, "direction", "left_to_right"),
        h_spacing: widgetValue(node, "layer_spacing", 80),
        v_spacing: widgetValue(node, "node_spacing", 40),
        group_mode: widgetValue(node, "group_mode", "cluster"),
    };
    // The API key is deliberately absent here: it lives server-side only
    // (key dialog / env var) and must never enter the graph or this payload.
    const llm = {
        enabled: widgetValue(node, "llm_clustering", false),
        base_url: widgetValue(node, "llm_base_url", ""),
        model: widgetValue(node, "llm_model", ""),
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
            llm: result.llm,
            animate: widgetValue(node, "animate", true),
        });
    } catch (err) {
        console.error("[LayoutSort] sort request failed:", err);
        app.extensionManager?.toast?.add?.({
            severity: "error",
            summary: "Layout Sort",
            detail: `Sort failed: ${err}`,
            life: 6000,
        });
    } finally {
        node.__layoutSortBusy = false;
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
            body: JSON.stringify({ api_key: result.key }),
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

app.registerExtension({
    name: "comfyui.layout.sort",
    setup() {
        api.addEventListener(WS_EVENT, ({ detail }) => applyLayout(detail ?? {}));
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_NAME) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            // Instant sort without queueing the workflow.
            this.addWidget("button", "✨ Sort now", null, () => sortNow(this));
            this.addWidget("button", KEY_BUTTON_UNSET, null, () => manageApiKey(this));
            refreshKeyStatus(this);
        };
    },
});
