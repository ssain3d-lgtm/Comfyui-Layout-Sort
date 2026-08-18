"""Standalone sanity tests for layout_core.compute_layout: refit mode,
cluster mode (disjoint group frames), nested groups, LLM extra clusters,
NaN sanitization, stale-group parking, degenerate input.

Run: python3 tests/test_layout.py (no ComfyUI required)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from layout_core import compute_layout, COLLAPSED_MAX_WIDTH, TITLE_HEIGHT


def effective_width(n):
    # Mirror the engine: collapsed nodes render at roughly title width.
    w = n["size"][0]
    return min(w, COLLAPSED_MAX_WIDTH) if n["flags"]["collapsed"] else w


def node(nid, ntype, pos, size, collapsed=False):
    return {
        "id": nid, "type": ntype, "pos": pos, "size": size,
        "flags": {"collapsed": collapsed}, "mode": 0,
    }


def rects_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


BASE = {
    "nodes": [
        node(4, "CheckpointLoaderSimple", [900, 700], [315, 98]),
        node(6, "CLIPTextEncode", [100, 500], [400, 200]),
        node(7, "CLIPTextEncode", [1200, 100], [400, 200]),
        node(5, "EmptyLatentImage", [50, 50], [315, 106]),
        node(3, "KSampler", [600, 50], [315, 262]),
        node(8, "VAEDecode", [200, 800], [210, 46]),
        node(9, "SaveImage", [700, 500], [308, 270]),
        node(10, "LoraLoader", [1500, 600], [315, 126], collapsed=True),
        node(99, "Note", [-200, 900], [350, 140]),          # island
        node(11, "LayoutSort", [1600, 900], [270, 130]),     # island (the sorter)
    ],
    # [link_id, origin_id, origin_slot, target_id, target_slot, type]
    "links": [
        [1, 4, 0, 10, 0, "MODEL"],
        [2, 4, 1, 10, 1, "CLIP"],
        [3, 10, 1, 6, 0, "CLIP"],
        [4, 10, 1, 7, 0, "CLIP"],
        [5, 10, 0, 3, 0, "MODEL"],
        [6, 6, 0, 3, 1, "CONDITIONING"],
        [7, 7, 0, 3, 2, "CONDITIONING"],
        [8, 5, 0, 3, 3, "LATENT"],
        [9, 3, 0, 8, 0, "LATENT"],
        [10, 4, 2, 8, 1, "VAE"],
        [11, 8, 0, 9, 0, "IMAGE"],
    ],
}
SIZES = {n["id"]: n for n in BASE["nodes"]}
FLOW_IDS = [4, 10, 6, 7, 5, 3, 8, 9]
ALL_IDS = [n["id"] for n in BASE["nodes"]]


def visual_rect(nid, pos):
    n = SIZES[nid]
    h = TITLE_HEIGHT + (0 if n["flags"]["collapsed"] else n["size"][1])
    return (pos[nid][0], pos[nid][1] - TITLE_HEIGHT, effective_width(n), h)


def check_flow_lr(pos, links):
    for _lid, o, _os, t, _ts, _ty in links:
        assert pos[o][0] + effective_width(SIZES[o]) <= pos[t][0] + 1e-6, \
            f"link {o}->{t} does not flow left to right"


def check_no_overlap(pos, ids):
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            assert not rects_overlap(visual_rect(a, pos), visual_rect(b, pos)), \
                f"nodes {a} and {b} overlap"


def inside(inner, outer):
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


# ---------------------------------------------------------------- refit mode
wf = dict(BASE)
wf["groups"] = [{"title": "Prompts", "bounding": [80, 60, 1600, 700]}]
result = compute_layout(wf, {"group_mode": "refit"})
pos = {int(k): v for k, v in result["positions"].items()}
assert set(pos) == set(ALL_IDS)
check_flow_lr(pos, BASE["links"])
check_no_overlap(pos, FLOW_IDS)
main_bottom = max(visual_rect(n, pos)[1] + visual_rect(n, pos)[3] for n in FLOW_IDS)
for nid in (99, 11):
    assert pos[nid][1] - TITLE_HEIGHT >= main_bottom, f"island {nid} not below main flow"
assert result["groups"], "refit: group should have been re-fitted"
g = result["groups"][0]["bounding"]
for nid in (6, 7, 3, 5):  # old members of the group region
    assert inside(visual_rect(nid, pos), g), f"refit: group lost node {nid}"
old_min_x = min(n["pos"][0] for n in BASE["nodes"])
assert abs(min(p[0] for p in pos.values()) - old_min_x) < 200, "layout drifted from anchor"
print("refit mode OK")

# -------------------------------------------------------------- cluster mode
wf = dict(BASE)
wf["groups"] = [
    {"title": "Loaders", "bounding": [880, 540, 1050, 320]},   # contains 4, 10
    {"title": "Sampling", "bounding": [0, 0, 1000, 400]},      # contains 3, 5
]
result = compute_layout(wf, {"group_mode": "cluster"})
pos = {int(k): v for k, v in result["positions"].items()}
assert set(pos) == set(ALL_IDS)
check_flow_lr(pos, BASE["links"])
check_no_overlap(pos, ALL_IDS)

frames = {u["index"]: u["bounding"] for u in result["groups"]}
assert set(frames) == {0, 1}, f"both groups should get frames, got {set(frames)}"
assert not rects_overlap(tuple(frames[0]), tuple(frames[1])), "cluster frames overlap"
for nid in (4, 10):
    assert inside(visual_rect(nid, pos), frames[0]), f"node {nid} escaped Loaders frame"
for nid in (3, 5):
    assert inside(visual_rect(nid, pos), frames[1]), f"node {nid} escaped Sampling frame"
members = {0: (4, 10), 1: (3, 5)}
for gindex, frame in frames.items():
    for nid in ALL_IDS:
        if nid not in members[gindex]:
            assert not rects_overlap(tuple(frame), visual_rect(nid, pos)), \
                f"frame {gindex} overlaps outsider node {nid}"
print("cluster mode OK")
for nid in sorted(pos, key=lambda n: (pos[n][0], pos[n][1])):
    print(f"  {SIZES[nid]['type']:<24} id={nid:<3} pos=({pos[nid][0]:7.1f}, {pos[nid][1]:7.1f})")
for gindex in sorted(frames):
    print(f"  frame[{gindex}] = {[round(v, 1) for v in frames[gindex]]}")

# ------------------------------------------------------------- nested groups
nested = {
    "nodes": [
        node(1, "A", [0, 0], [200, 80]),
        node(2, "B", [300, 300], [200, 80]),
        node(3, "Stray", [600, 0], [200, 80]),
    ],
    "links": [[1, 1, 0, 2, 0, "X"]],
    "groups": [
        {"title": "Outer", "bounding": [-50, -80, 900, 600]},   # contains inner + 3
        {"title": "Inner", "bounding": [-20, -50, 500, 480]},   # contains 1, 2
    ],
}
result = compute_layout(nested, {"group_mode": "cluster"})
pos = {int(k): v for k, v in result["positions"].items()}
frames = {u["index"]: u["bounding"] for u in result["groups"]}
assert set(frames) == {0, 1}, f"nested: expected both frames, got {set(frames)}"
sz = {n["id"]: n for n in nested["nodes"]}

def vrect(nid):
    return (pos[nid][0], pos[nid][1] - TITLE_HEIGHT, sz[nid]["size"][0],
            sz[nid]["size"][1] + TITLE_HEIGHT)

assert inside(vrect(1), frames[1]) and inside(vrect(2), frames[1]), "inner lost members"
assert inside(tuple(frames[1]), tuple(frames[0])), "outer frame does not contain inner frame"
assert inside(vrect(3), frames[0]), "outer frame lost stray node"
assert not rects_overlap(vrect(3), tuple(frames[1])), "stray node inside inner frame"
assert pos[1][0] + 200 <= pos[2][0] + 1e-6, "nested: flow broken inside cluster"
print("nested groups OK")

# ------------------------------------------------------------ extra clusters
plain = {
    "nodes": [node(i, f"T{i}", [i * 10, i * 10], [200, 100]) for i in range(1, 7)],
    "links": [[i, i, 0, i + 1, 0, "X"] for i in range(1, 6)],
}
result = compute_layout(plain, {"group_mode": "cluster"},
                        [{"name": "Front", "node_ids": [1, 2]},
                         {"name": "Solo", "node_ids": [3]},          # dropped (<2)
                         {"name": "Back", "node_ids": [5, 6, 999]}])  # 999 unknown
assert [g["title"] for g in result["new_groups"]] == ["Front", "Back"], result["new_groups"]
assert result["groups"] == []
pos = {int(k): v for k, v in result["positions"].items()}
for title, members in (("Front", (1, 2)), ("Back", (5, 6))):
    frame = next(g["bounding"] for g in result["new_groups"] if g["title"] == title)
    for nid in members:
        r = (pos[nid][0], pos[nid][1] - TITLE_HEIGHT, 200, 100 + TITLE_HEIGHT)
        assert inside(r, frame), f"{title} lost node {nid}"
print("extra clusters OK")

# ------------------------------------------------- NaN pos must not poison
poisoned = {
    "nodes": [node(1, "Bad", [float("nan"), 100], [float("inf"), 50]),
              node(2, "Good", [400, 100], [200, 80])],
    "links": [[1, 1, 0, 2, 0, "X"]],
}
result = compute_layout(poisoned)
assert set(result["positions"]) == {"1", "2"}
for p in result["positions"].values():
    assert all(math.isfinite(v) for v in p), f"non-finite coordinate: {p}"
print("NaN sanitization OK")

# --------------------------------------- stale (empty) groups get parked
stale_wf = {
    "nodes": [node(1, "A", [0, 0], [200, 80]), node(2, "B", [300, 0], [200, 80])],
    "links": [[1, 1, 0, 2, 0, "X"]],
    "groups": [{"title": "Empty", "bounding": [50, -40, 300, 260]},  # covers node 1
               {"title": "Truly empty", "bounding": [5000, 5000, 240, 120]}],
}
result = compute_layout(stale_wf, {"group_mode": "cluster"})
frames = {u["index"]: u["bounding"] for u in result["groups"]}
assert 1 in frames, "contentless group must still receive a frame update"
pos = {int(k): v for k, v in result["positions"].items()}
node_bottom = max(p[1] - TITLE_HEIGHT + 80 + TITLE_HEIGHT for p in pos.values())
parked = frames[1]
assert parked[1] >= node_bottom, "parked frame must sit below the layout"
# Snapping rounds frames outward, so each dimension may grow by <= grid.
assert 240 <= parked[2] <= 250 and 120 <= parked[3] <= 130, \
    f"parked frame should keep its size (mod outward snap): {parked}"
print("stale group parking OK")

# ---------------------------- LayoutSort trigger must not warp the layout
wf = {"nodes": list(BASE["nodes"]),
      "links": list(BASE["links"]) + [[99, 9, 0, 11, 0, "*"]]}  # 9 -> LayoutSort
result = compute_layout(wf, {"group_mode": "refit"})
pos = {int(k): v for k, v in result["positions"].items()}
check_flow_lr(pos, BASE["links"])          # real links still flow left-right
main_bottom = max(visual_rect(n, pos)[1] + visual_rect(n, pos)[3] for n in FLOW_IDS)
assert pos[11][1] - TITLE_HEIGHT >= main_bottom, \
    "LayoutSort with a trigger link must stay an island below the flow"
print("detach LayoutSort OK")

# -------------------------- upstream nodes follow their consumer's slots
slot_wf = {
    "nodes": [
        node(1, "A", [0, 300], [200, 80]),   # feeds slot 2
        node(2, "B", [0, 0], [200, 80]),     # feeds slot 0
        node(3, "C", [0, 600], [200, 80]),   # feeds slot 1
        node(4, "D", [400, 300], [200, 200]),
    ],
    "links": [
        [1, 1, 0, 4, 2, "X"],
        [2, 2, 0, 4, 0, "X"],
        [3, 3, 0, 4, 1, "X"],
    ],
}
result = compute_layout(slot_wf)
pos = {int(k): v for k, v in result["positions"].items()}
assert pos[2][1] < pos[3][1] < pos[1][1], \
    f"producers must stack in input-slot order (B,C,A): {pos}"
print("slot ordering OK")

# ------------------------------------------- reroutes follow their links
def check_reroute_chain(result, pos_key_a=1, pos_key_b=2):
    pos = {int(k): v for k, v in result["positions"].items()}
    rr = {int(k): v for k, v in result["reroutes"].items()}
    assert set(rr) == {1, 2}, f"both reroutes must be placed: {rr}"
    a_right = pos[pos_key_a][0] + 200
    b_left = pos[pos_key_b][0]
    assert a_right <= rr[1][0] < rr[2][0] <= b_left, \
        f"chain must run origin->target between the nodes: {rr}"
    # both points sit on the straight segment between the two port centers
    ay = pos[pos_key_a][1] - TITLE_HEIGHT + (80 + TITLE_HEIGHT) / 2
    by = pos[pos_key_b][1] - TITLE_HEIGHT + (80 + TITLE_HEIGHT) / 2
    for rid in (1, 2):
        frac = (rr[rid][0] - a_right) / max(b_left - a_right, 1e-6)
        expected_y = ay + (by - ay) * frac
        assert abs(rr[rid][1] - expected_y) < 1e-6, (rid, rr[rid], expected_y)

# 0.4 format: extra.reroutes + extra.linkExtensions
wf_04 = {
    "nodes": [node(1, "A", [0, 0], [200, 80]), node(2, "B", [900, 500], [200, 80])],
    "links": [[5, 1, 0, 2, 0, "X"]],
    "extra": {
        "reroutes": [
            {"id": 1, "pos": [400, 300], "linkIds": [5]},
            {"id": 2, "parentId": 1, "pos": [600, 400], "linkIds": [5]},
        ],
        "linkExtensions": [{"id": 5, "parentId": 2}],
    },
}
check_reroute_chain(compute_layout(wf_04, {"snap_grid": 0}))

# schema v1: top-level reroutes + object links carrying parentId
wf_v1 = {
    "nodes": [node(1, "A", [0, 0], [200, 80]), node(2, "B", [900, 500], [200, 80])],
    "links": [{"id": 5, "origin_id": 1, "origin_slot": 0,
               "target_id": 2, "target_slot": 0, "type": "X", "parentId": 2}],
    "reroutes": [
        {"id": 1, "pos": [400, 300], "linkIds": [5]},
        {"id": 2, "parentId": 1, "pos": [600, 400], "linkIds": [5]},
    ],
}
check_reroute_chain(compute_layout(wf_v1, {"snap_grid": 0}))
print("reroutes OK")

# ----------------------------------- Set/Get wireless pairs shape the flow
sg_wf = {
    "nodes": [
        node(1, "LoadImage", [0, 0], [200, 80]),
        node(2, "SetNode", [300, 0], [150, 60]),
        node(3, "GetNode", [0, 400], [150, 60]),
        node(4, "SaveImage", [300, 400], [200, 80]),
        node(5, "GetNode", [700, 700], [150, 60]),  # key via title fallback
        node(6, "PreviewImage", [900, 700], [200, 80]),
    ],
    "links": [[1, 1, 0, 2, 0, "*"], [2, 3, 0, 4, 0, "*"],
              [3, 5, 0, 6, 0, "*"]],
}
sg_wf["nodes"][1]["widgets_values"] = ["img"]
sg_wf["nodes"][2]["widgets_values"] = ["img"]
sg_wf["nodes"][4]["title"] = "Get_img"
result = compute_layout(sg_wf, {"snap_grid": 0})
pos = {int(k): v for k, v in result["positions"].items()}
assert pos[2][0] + 150 <= pos[3][0] + 1e-6, \
    "Set must lay out before its Get (virtual edge)"
assert pos[2][0] + 150 <= pos[5][0] + 1e-6, \
    "title-fallback Get must also follow the Set"
assert pos[1][0] + 200 <= pos[2][0] + 1e-6 and pos[3][0] + 150 <= pos[4][0] + 1e-6
compute_layout(sg_wf, {"snap_grid": 0, "link_set_get": False})  # opt-out works
print("set/get wireless OK")

# --------------------------- inner mode preserves the macro arrangement
inner_wf = {
    "nodes": [
        node(1, "A", [1200, 1350], [200, 80]),   # G1, scrambled: A after B
        node(2, "B", [1000, 1000], [200, 80]),   # G1
        node(3, "C", [5000, 200], [200, 80]),    # G2
        node(4, "Free", [9000, 9000], [200, 80]),  # ungrouped
    ],
    "links": [[1, 2, 0, 1, 0, "X"]],  # B -> A inside G1
    "groups": [
        {"title": "G1", "bounding": [950, 900, 600, 700]},
        {"title": "G2", "bounding": [4950, 100, 400, 300]},
    ],
}
result = compute_layout(inner_wf, {"group_mode": "inner", "snap_grid": 10})
pos = {int(k): v for k, v in result["positions"].items()}
assert set(pos) == {1, 2, 3}, f"ungrouped nodes must not move: {sorted(pos)}"
frames = {u["index"]: u["bounding"] for u in result["groups"]}
assert set(frames) == {0, 1}
for gindex, (ox, oy) in ((0, (950, 900)), (1, (4950, 100))):
    fx, fy = frames[gindex][0], frames[gindex][1]
    assert abs(fx - ox) <= 10 and abs(fy - oy) <= 10, \
        f"group {gindex} anchor moved: {(fx, fy)} vs {(ox, oy)}"
assert pos[2][0] + 200 <= pos[1][0] + 1e-6, "flow inside the group must sort"
for nid, gindex in ((1, 0), (2, 0), (3, 1)):
    x, y, w, h = pos[nid][0], pos[nid][1] - TITLE_HEIGHT, 200, 80 + TITLE_HEIGHT
    fx, fy, fw, fh = frames[gindex]
    assert fx <= x and fy <= y and x + w <= fx + fw + 1e-6 \
        and y + h <= fy + fh + 1e-6, f"node {nid} left frame {gindex}"
assert compute_layout({"nodes": inner_wf["nodes"], "links": []},
                      {"group_mode": "inner"})["positions"] == {}, \
    "inner mode without groups must be a no-op"
print("inner mode OK")

# --------------------------------------------- style: align / equalize / snap
style_wf = {
    "nodes": [
        node(1, "A", [0, 0], [200, 80]),      # layer 0 (short column)
        node(2, "C", [0, 500], [140, 300]),   # layer 0 (tall column)
        node(3, "B", [900, 200], [200, 100]), # layer 1
    ],
    "links": [[1, 1, 0, 3, 0, "X"], [2, 2, 0, 3, 1, "X"]],
}
# align=top: every column starts at the same visual top edge.
r_top = compute_layout(style_wf, {"align": "top", "snap_grid": 10})
p = {int(k): v for k, v in r_top["positions"].items()}
assert abs(p[1][1] - p[3][1]) < 1e-6, "top-aligned columns must share a top edge"
for pos in p.values():
    assert pos[0] % 10 == 0 and pos[1] % 10 == 0, f"not snapped: {pos}"
# align=center: single-node column 3 centers against the taller column.
r_center = compute_layout(style_wf, {"align": "center", "snap_grid": 0})
pc = {int(k): v for k, v in r_center["positions"].items()}
assert pc[3][1] > pc[1][1], "centered column should sit lower than the top edge"
# Node sizes are never part of the result — the sorter only moves things.
assert "sizes" not in r_top, "the sorter must not resize nodes"
print("style options OK")

# ---------------------------------------------------------------- degenerate
assert compute_layout({}) == {"positions": {}, "groups": [],
                              "new_groups": [], "reroutes": {}}
assert compute_layout({"nodes": [], "links": None})["positions"] == {}
compute_layout({"nodes": [node(1, "Solo", {"0": 5, "1": 6}, {"0": 100, "1": 50})],
                "links": [{"origin_id": 1, "target_id": 1}],
                "groups": [{"bounding": [0, 0, 10, 10]}, {"bad": True}, None]})
# group with no nodes at all + cluster of a single node
compute_layout({"nodes": [node(1, "Solo", [0, 0], [100, 50])],
                "links": [],
                "groups": [{"bounding": [-20, -50, 200, 150]},
                           {"bounding": [5000, 5000, 100, 100]}]},
               {"group_mode": "cluster"})
# top_to_bottom in cluster mode must flow downward
res_v = compute_layout(wf, {"direction": "top_to_bottom", "group_mode": "cluster"})
pos_v = {int(k): v for k, v in res_v["positions"].items()}
for _lid, o, _os, t, _ts, _ty in BASE["links"]:
    o_h = 0 if SIZES[o]["flags"]["collapsed"] else SIZES[o]["size"][1]
    assert pos_v[o][1] + o_h <= pos_v[t][1] - TITLE_HEIGHT + 1e-6, \
        f"link {o}->{t} does not flow top to bottom"
print("degenerate + vertical OK")
print("ALL CHECKS PASSED")
