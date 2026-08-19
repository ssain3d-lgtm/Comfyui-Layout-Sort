// Align/distribute geometry from web/layoutSort.js, exercised on plain
// rects. The pure functions are extracted between their markers so this
// runs in plain node (the module itself imports ComfyUI scripts).
//
// Run: node tests/test_frontend_tools.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "web", "layoutSort.js"), "utf8");
const start = src.indexOf("// --- pure geometry");
const end = src.indexOf("// --- end pure geometry");
if (start < 0 || end < 0) throw new Error("pure-geometry markers missing");
const factory = new Function(
    src.slice(start, end) +
    "\nreturn { computeAlignDeltas, computeDistributeDeltas };");
const { computeAlignDeltas, computeDistributeDeltas } = factory();

const rects = [
    { id: 1, x: 100, y: 50, w: 200, h: 100 },
    { id: 2, x: 400, y: 300, w: 100, h: 60 },
    { id: 3, x: 900, y: 120, w: 300, h: 200 },
];
const apply = (deltas) => rects.map((r) => ({
    ...r, x: r.x + (deltas[r.id]?.[0] ?? 0), y: r.y + (deltas[r.id]?.[1] ?? 0),
}));
const assert = (cond, msg) => { if (!cond) throw new Error(msg); };

// align left: every x lands on the leftmost edge
let out = apply(computeAlignDeltas(rects, "left"));
assert(out.every((r) => r.x === 100), "align left");
// align right: every right edge on the rightmost
out = apply(computeAlignDeltas(rects, "right"));
assert(out.every((r) => r.x + r.w === 1200), "align right");
// align top / bottom
out = apply(computeAlignDeltas(rects, "top"));
assert(out.every((r) => r.y === 50), "align top");
out = apply(computeAlignDeltas(rects, "bottom"));
assert(out.every((r) => r.y + r.h === 360), "align bottom");
// centers coincide on the span midpoint (rounding tolerance 1px)
out = apply(computeAlignDeltas(rects, "center_h"));
const cxs = out.map((r) => r.x + r.w / 2);
assert(Math.max(...cxs) - Math.min(...cxs) <= 1, "center_h");
out = apply(computeAlignDeltas(rects, "center_v"));
const cys = out.map((r) => r.y + r.h / 2);
assert(Math.max(...cys) - Math.min(...cys) <= 1, "center_v");

// distribute horizontally: outermost fixed, equal gaps between rects
out = apply(computeDistributeDeltas(rects, "h")).sort((a, b) => a.x - b.x);
assert(out[0].x === 100 && out[2].x + out[2].w === 1200,
    "distribute keeps the outermost fixed");
const gap1 = out[1].x - (out[0].x + out[0].w);
const gap2 = out[2].x - (out[1].x + out[1].w);
assert(Math.abs(gap1 - gap2) <= 1, `equal gaps, got ${gap1} vs ${gap2}`);

// distribute vertically
out = apply(computeDistributeDeltas(rects, "v")).sort((a, b) => a.y - b.y);
const vgap1 = out[1].y - (out[0].y + out[0].h);
const vgap2 = out[2].y - (out[1].y + out[1].h);
assert(Math.abs(vgap1 - vgap2) <= 1, `equal v-gaps, got ${vgap1} vs ${vgap2}`);

// too-few selections are no-ops
assert(Object.keys(computeAlignDeltas([rects[0]], "left")).length === 0,
    "align needs 2+");
assert(Object.keys(computeDistributeDeltas(rects.slice(0, 2), "h")).length === 0,
    "distribute needs 3+");

console.log("ALL FRONTEND TOOL TESTS PASSED");
