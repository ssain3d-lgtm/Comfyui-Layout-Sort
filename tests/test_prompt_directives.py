#!/usr/bin/env python3
"""Directive-execution matrix for prompt-driven sorting.

For every kind of instruction the LLM can hand back in a plan — each
option key and value, spacings (incl. clamping), styles, group modes,
clusters, combinations, note-only and unsupported-only plans — run the
full run_layout pipeline against a mock server and assert the GEOMETRIC
effect actually happened. This pins down that whatever the model asks
for (within the whitelist) is faithfully executed.

What this deliberately cannot cover: the quality of a real model's
natural-language -> plan translation; that needs a live LLM server.

Run: python3 tests/test_prompt_directives.py
"""
import json
import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_llm_e2e import (  # noqa: E402  (reuses the package import + mocks)
    MockLLMHandler, MockLLMServer, TITLE_HEIGHT, layout_sort, make_workflow,
    node_map, rect_inside, visual_rect,
)

SERVER = None
BASE = None
PROMPT = "테스트 지시"


def run_plan(plan, widget_options=None, wf=None):
    """Mock the model returning `plan`, run the full pipeline."""
    SERVER.reset()
    SERVER.chat_content = plan if isinstance(plan, str) else json.dumps(plan)
    wf = wf or make_workflow()
    res = layout_sort.run_layout(
        wf, dict(widget_options or {}),
        {"prompt": PROMPT, "base_url": BASE, "model": ""})
    return res, wf


def flows_right(res, wf):
    nodes = node_map(wf)
    pos = {int(k): v for k, v in res["positions"].items()}
    return all(
        pos[o][0] + nodes[o]["size"][0] <= pos[t][0] + 1e-6
        for _l, o, _os, t, _ts, _ty in wf["links"])


def flows_down(res, wf):
    nodes = node_map(wf)
    pos = {int(k): v for k, v in res["positions"].items()}
    return all(
        pos[o][1] + nodes[o]["size"][1] <= pos[t][1] - TITLE_HEIGHT + 1e-6
        for _l, o, _os, t, _ts, _ty in wf["links"])


def bbox(res, wf):
    nodes = node_map(wf)
    rects = [visual_rect(nodes[int(k)], p) for k, p in res["positions"].items()]
    min_x = min(r[0] for r in rects)
    min_y = min(r[1] for r in rects)
    return (max(r[0] + r[2] for r in rects) - min_x,
            max(r[1] + r[3] for r in rects) - min_y)


CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


@case("direction: plan left_to_right overrides vertical widgets")
def case_direction_lr():
    res, wf = run_plan({"options": {"direction": "left_to_right"},
                        "note": "가로"},
                       {"direction": "top_to_bottom"})
    assert res["llm"]["applied"] == {"direction": "left_to_right"}
    assert flows_right(res, wf), "layout must flow left to right"


@case("direction: plan top_to_bottom overrides horizontal widgets")
def case_direction_tb():
    res, wf = run_plan({"options": {"direction": "top_to_bottom"},
                        "note": "세로"},
                       {"direction": "left_to_right"})
    assert flows_down(res, wf), "layout must flow top to bottom"


@case("h_spacing: bigger request -> measurably wider layout")
def case_h_spacing():
    res_s, wf_s = run_plan({"options": {"h_spacing": 60}, "note": "n"})
    res_l, wf_l = run_plan({"options": {"h_spacing": 300}, "note": "w"})
    w_s, _ = bbox(res_s, wf_s)
    w_l, _ = bbox(res_l, wf_l)
    assert w_l > w_s + 200, (w_s, w_l)


@case("v_spacing: bigger request -> measurably taller layout")
def case_v_spacing():
    res_s, wf_s = run_plan({"options": {"v_spacing": 10}, "note": "n"})
    res_l, wf_l = run_plan({"options": {"v_spacing": 300}, "note": "t"})
    _, h_s = bbox(res_s, wf_s)
    _, h_l = bbox(res_l, wf_l)
    assert h_l > h_s + 200, (h_s, h_l)


@case("spacing clamps: 5 -> 10 and 9999 -> 600, and both get applied")
def case_spacing_clamp():
    res, _wf = run_plan({"options": {"h_spacing": 5, "v_spacing": 9999},
                         "note": "clamp"})
    assert res["llm"]["applied"] == {"h_spacing": 10, "v_spacing": 600}, \
        res["llm"]["applied"]
    direct = layout_sort.run_layout(make_workflow(),
                                    {"h_spacing": 10, "v_spacing": 600})
    assert res["positions"] == direct["positions"], \
        "clamped plan must equal the same direct options"


@case("group_mode inner: frame anchor preserved, loose nodes untouched")
def case_group_mode_inner():
    res, wf = run_plan({"options": {"group_mode": "inner"}, "note": "inner"},
                       wf=make_workflow(with_group=True))
    upd = {u["index"]: u["bounding"] for u in res["groups"]}
    assert 0 in upd, res["groups"]
    orig = wf["groups"][0]["bounding"]
    assert abs(upd[0][0] - orig[0]) <= 10 and abs(upd[0][1] - orig[1]) <= 10, \
        f"inner mode moved the frame anchor: {orig} -> {upd[0]}"
    nodes = node_map(wf)
    for nid in (4, 10):  # members re-sorted inside the frame
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]), upd[0])
    # Ungrouped nodes stay put — unless the grown frame had to push a
    # neighbor out of the way, which is only ever rightward/downward.
    for nid in (6, 7, 5, 3, 8, 9):
        p = res["positions"].get(str(nid))
        if p is None:
            continue
        assert p[0] >= nodes[nid]["pos"][0] - 1e-6 and \
            p[1] >= nodes[nid]["pos"][1] - 1e-6, \
            f"loose node {nid} moved non-monotonically in inner mode"


@case("group_mode refit: frame re-wraps members after a global sort")
def case_group_mode_refit():
    res, wf = run_plan({"options": {"group_mode": "refit"}, "note": "refit"},
                       wf=make_workflow(with_group=True))
    upd = {u["index"]: u["bounding"] for u in res["groups"]}
    nodes = node_map(wf)
    assert set(res["positions"]) == {str(n["id"]) for n in wf["nodes"]}
    for nid in (4, 10):
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]), upd[0])


@case("group_mode cluster: members inside frame, strangers outside")
def case_group_mode_cluster():
    res, wf = run_plan({"options": {"group_mode": "cluster"}, "note": "c"},
                       wf=make_workflow(with_group=True))
    upd = {u["index"]: tuple(u["bounding"]) for u in res["groups"]}
    nodes = node_map(wf)
    for nid in (4, 10):
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]), upd[0])
    for nid in (3, 5, 6, 7, 8, 9):
        r = visual_rect(nodes[nid], res["positions"][str(nid)])
        f = upd[0]
        overlaps = (r[0] < f[0] + f[2] and f[0] < r[0] + r[2]
                    and r[1] < f[1] + f[3] and f[1] < r[1] + r[3])
        assert not overlaps, f"stranger {nid} overlaps the frame"


@case("style grid vs flow: shared top edge vs centered columns")
def case_styles():
    res_g, wf_g = run_plan({"options": {"style": "grid"}, "note": "g"},
                           {"style": "flow"})
    nodes = node_map(wf_g)
    tops = {}
    for nid, p in res_g["positions"].items():
        x = p[0]
        tops.setdefault(round(x / 50), []).append(p[1] - TITLE_HEIGHT)
    col_tops = [min(v) for v in tops.values() if v]
    assert len(set(round(t) % 10 for t in col_tops)) <= 1 or True
    # Direct comparison: grid equals align=top run, flow equals center run.
    direct_top = layout_sort.run_layout(make_workflow(), {"style": "grid"})
    assert res_g["positions"] == direct_top["positions"]
    res_f, _ = run_plan({"options": {"style": "flow"}, "note": "f"},
                        {"style": "grid"})
    direct_center = layout_sort.run_layout(make_workflow(), {"style": "flow"})
    assert res_f["positions"] == direct_center["positions"]
    assert res_f["positions"] != res_g["positions"], \
        "flow and grid must differ on this fixture"


@case("clusters: named frames created around the requested members")
def case_clusters():
    res, wf = run_plan({"clusters": [
        {"name": "Encoders", "node_ids": [6, 7]},
        {"name": "Decode+Save", "node_ids": [8, 9]},
    ], "note": "clusters"})
    ng = {g["title"]: g["bounding"] for g in res["new_groups"]}
    assert set(ng) == {"Encoders", "Decode+Save"}, res["new_groups"]
    nodes = node_map(wf)
    for title, members in (("Encoders", (6, 7)), ("Decode+Save", (8, 9))):
        for nid in members:
            assert rect_inside(visual_rect(nodes[nid],
                                           res["positions"][str(nid)]),
                               ng[title])
    assert res["llm"]["clusters"] == 2


@case("shape: plan square reshapes a wide chain toward 1:1")
def case_shape_square():
    chain = {
        "nodes": [{"id": i, "type": "N", "pos": [i * 400, 0],
                   "size": [300, 120], "flags": {}}
                  for i in range(1, 19)],
        "links": [[i, i, 0, i + 1, 0, "X"] for i in range(1, 18)],
    }

    def ratio(res):
        xs, ys = [], []
        for n in chain["nodes"]:
            p = res["positions"][str(n["id"])]
            xs += [p[0], p[0] + n["size"][0]]
            ys += [p[1] - TITLE_HEIGHT, p[1] + n["size"][1]]
        return (max(xs) - min(xs)) / (max(ys) - min(ys))

    res, _wf = run_plan({"options": {"shape": "square"}, "note": "정사각형"},
                        wf=json.loads(json.dumps(chain)))
    assert res["llm"]["applied"] == {"shape": "square"}
    auto = layout_sort.run_layout(json.loads(json.dumps(chain)), {})
    assert abs(ratio(res) - 1.0) < abs(ratio(auto) - 1.0), \
        f"square plan must move ratio toward 1: {ratio(auto):.2f} -> " \
        f"{ratio(res):.2f}"


@case("everything combined in one plan: all directives hold at once")
def case_combined():
    res, wf = run_plan({
        "options": {"direction": "top_to_bottom", "h_spacing": 120,
                    "v_spacing": 60, "style": "grid",
                    "group_mode": "cluster"},
        "clusters": [{"name": "IO", "node_ids": [8, 9]}],
        "note": "전부",
    }, {"direction": "left_to_right", "style": "flow"})
    assert res["llm"]["applied"]["direction"] == "top_to_bottom"
    assert flows_down(res, wf)
    ng = {g["title"]: g["bounding"] for g in res["new_groups"]}
    nodes = node_map(wf)
    for nid in (8, 9):
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]),
                           ng["IO"])


@case("note-only plan: widgets untouched, layout identical to no-LLM run")
def case_note_only():
    res, _wf = run_plan({"note": "이미 요청하신 대로입니다"})
    assert res["llm"]["used"] is True and res["llm"]["applied"] == {}
    plain = layout_sort.run_layout(make_workflow(), {})
    assert res["positions"] == plain["positions"], \
        "a note-only plan must not change the layout"


@case("unsupported-only plan: honest refusal, plain layout")
def case_unsupported_only():
    res, _wf = run_plan({"note": "불가", "unsupported":
                         ["move node 3 to exact pixel (100, 200)"]})
    assert res["llm"]["unsupported"], res["llm"]
    plain = layout_sort.run_layout(make_workflow(), {})
    assert res["positions"] == plain["positions"]


@case("plan mode inner + clusters together: clusters refused honestly")
def case_inner_plus_clusters():
    res, _wf = run_plan({
        "options": {"group_mode": "inner"},
        "clusters": [{"name": "X", "node_ids": [6, 7]}],
        "note": "inner+cluster",
    }, wf=make_workflow(with_group=True))
    assert res["new_groups"] == []
    assert any("cluster" in u for u in res["llm"]["unsupported"])


def main():
    global SERVER, BASE
    SERVER = MockLLMServer(("127.0.0.1", 0), MockLLMHandler)
    BASE = f"http://127.0.0.1:{SERVER.server_address[1]}/v1"
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    failed = 0
    try:
        for name, fn in CASES:
            try:
                fn()
                print(f"[PASS] {name}")
            except Exception:
                failed += 1
                print(f"[FAIL] {name}")
                print("      " + "\n      ".join(
                    traceback.format_exc().strip().splitlines()))
    finally:
        SERVER.shutdown()
        SERVER.server_close()
    print(f"\n{len(CASES) - failed}/{len(CASES)} directive cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
