import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const WS_EVENT = "layout_sort_apply";
const NODE_NAME = "LayoutSort";

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

function applyGroups(groups) {
    const graphGroups = app.graph._groups ?? app.graph.groups ?? [];
    for (const update of groups ?? []) {
        const group = graphGroups[update.index];
        const b = update.bounding;
        if (!group || !Array.isArray(b) || b.length < 4) continue;
        group.pos = [b[0], b[1]];
        group.size = [b[2], b[3]];
    }
}

function applyLayout({ positions, groups, animate }) {
    const moves = collectMoves(positions);
    if (!moves.length) return;
    // A broadcast event may reach a tab showing a different workflow;
    // only apply when the ids clearly belong to this graph.
    const total = Object.keys(positions ?? {}).length;
    if (total > 0 && moves.length / total < 0.9) {
        console.warn(`[LayoutSort] ignoring layout for a different graph (${moves.length}/${total} ids matched)`);
        return;
    }

    const finish = () => {
        for (const m of moves) {
            m.node.pos[0] = m.to[0];
            m.node.pos[1] = m.to[1];
        }
        applyGroups(groups);
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
    const workflow = app.graph.serialize();
    const options = {
        direction: widgetValue(node, "direction", "left_to_right"),
        h_spacing: widgetValue(node, "layer_spacing", 80),
        v_spacing: widgetValue(node, "node_spacing", 40),
        group_mode: widgetValue(node, "group_mode", "cluster"),
    };
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
            animate: widgetValue(node, "animate", true),
        });
    } catch (err) {
        console.error("[LayoutSort] sort request failed:", err);
    }
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
        };
    },
});
