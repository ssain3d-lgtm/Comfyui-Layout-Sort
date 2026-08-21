#!/usr/bin/env python3
"""Selection-scoped sorting: options["scope_ids"] through run_layout.

Only the selected nodes may move, anchored where the selection sits;
fully selected group frames refit (with their ORIGINAL indices), while
partially selected frames stay untouched.

Run: python3 tests/test_scoped_sort.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_llm_e2e import (  # noqa: E402
    TITLE_HEIGHT, layout_sort, make_workflow, node_map, rect_inside,
    visual_rect,
)


def two_group_workflow():
    wf = make_workflow()
    wf["groups"] = [
        # holds centers of 4 and 10 only
        {"title": "Loaders", "bounding": [50, 100, 500, 500]},
        # holds visual centers of 8 (1505, 308) and 9 (1857.5, 420) only
        {"title": "IO", "bounding": [1380, 250, 700, 300]},
    ]
    return wf


def main():
    # --- scope moves only the selected nodes, others absent from output
    wf = make_workflow()
    res = layout_sort.run_layout(wf, {"scope_ids": [3, 8]})
    assert set(res["positions"]) == {"3", "8"}, sorted(res["positions"])
    nodes = node_map(wf)
    p3, p8 = res["positions"]["3"], res["positions"]["8"]
    assert p3[0] + nodes[3]["size"][0] <= p8[0] + 1e-6, \
        "selected pair must still flow left to right"
    # anchored at the selection's own visual top-left (snap tolerance)
    old_min_x = min(nodes[3]["pos"][0], nodes[8]["pos"][0])
    old_min_y = min(nodes[3]["pos"][1] - TITLE_HEIGHT,
                    nodes[8]["pos"][1] - TITLE_HEIGHT)
    new_min_x = min(p3[0], p8[0])
    new_min_y = min(p3[1] - TITLE_HEIGHT, p8[1] - TITLE_HEIGHT)
    assert abs(new_min_x - old_min_x) <= 10 and \
        abs(new_min_y - old_min_y) <= 10, \
        f"selection anchor drifted: ({old_min_x},{old_min_y}) -> " \
        f"({new_min_x},{new_min_y})"
    assert res["group_count"] == 0
    print("scope basic + anchor OK")

    # --- string ids behave like ints
    res_str = layout_sort.run_layout(make_workflow(),
                                     {"scope_ids": ["3", "8"]})
    assert res_str["positions"] == res["positions"]
    print("string ids OK")

    # --- fully selected group refits and keeps its ORIGINAL index
    wf = two_group_workflow()
    res = layout_sort.run_layout(wf, {"scope_ids": [8, 9]})
    assert set(res["positions"]) == {"8", "9"}
    assert [u["index"] for u in res["groups"]] == [1], res["groups"]
    bounding = res["groups"][0]["bounding"]
    nodes = node_map(wf)
    for nid in (8, 9):
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]), bounding)
    assert res["group_count"] == 2, "staleness token stays full-graph"
    print("group index remap OK")

    # --- partially selected group: frame untouched, member sorts loose
    wf = two_group_workflow()
    res = layout_sort.run_layout(wf, {"scope_ids": [4, 6]})  # 10 left out
    assert set(res["positions"]) == {"4", "6"}
    assert res["groups"] == [], \
        f"partial group frame must stay untouched: {res['groups']}"
    print("partial group OK")

    # --- empty scope list falls back to a whole-graph sort
    wf = make_workflow()
    res = layout_sort.run_layout(wf, {"scope_ids": []})
    assert set(res["positions"]) == {str(n["id"]) for n in wf["nodes"]}
    print("empty scope fallback OK")

    # --- scope with zero matching ids yields an empty, harmless result
    res = layout_sort.run_layout(make_workflow(), {"scope_ids": [999]})
    assert res["positions"] == {} and res["groups"] == []
    print("unknown ids OK")

    # --- drawn zone: an EMPTY group frame shapes and places the layout
    wf = two_group_workflow()
    wf["groups"].append({"title": "Zone", "bounding": [5000, 7000,
                                                       2400, 1600]})
    res = layout_sort.run_layout(wf, {"zone": [5000, 7000, 2400, 1600],
                                      "zone_index": 2})
    assert res["zone"] == {"applied": True, "reason": None}, res.get("zone")
    # content lands at the drawn corner (snap tolerance)
    nodes = node_map(wf)
    min_x = min(min(p[0] for p in res["positions"].values()),
                min(u["bounding"][0] for u in res["groups"]))
    min_y = min(min(p[1] - TITLE_HEIGHT
                    for p in res["positions"].values()),
                min(u["bounding"][1] for u in res["groups"]))
    assert abs(min_x - 5000) <= 10 and abs(min_y - 7000) <= 10, \
        f"zone corner missed: ({min_x}, {min_y})"
    # the zone frame itself is never updated; real groups keep their
    # ORIGINAL indices (0 and 1) despite the zone being dropped mid-way
    touched = {u["index"] for u in res["groups"]}
    assert 2 not in touched and touched <= {0, 1}, res["groups"]
    assert res["group_count"] == 3, "staleness token counts the zone too"
    for nid in (8, 9):  # IO group members stay inside their frame
        bounding = next(u["bounding"] for u in res["groups"]
                        if u["index"] == 1)
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]), bounding)
    print("zone fit OK")

    # --- wrong index hint, right rectangle: the rect wins (frontends
    # with proxy-broken indexOf send -1 or a stale index)
    for bad_hint in (-1, 0, None):
        wf = two_group_workflow()
        wf["groups"].append({"title": "Zone",
                             "bounding": [5000, 7000, 2400, 1600]})
        res = layout_sort.run_layout(
            wf, {"zone": [5000, 7000, 2400, 1600], "zone_index": bad_hint})
        assert res["zone"]["applied"] is True, (bad_hint, res.get("zone"))
        assert 2 not in {u["index"] for u in res["groups"]}
    print("zone rect resolution OK")

    # --- a POPULATED group offered as a zone is refused (it is content)
    wf = two_group_workflow()
    res = layout_sort.run_layout(wf, {"zone": [50, 100, 500, 500],
                                      "zone_index": 0})
    assert {u["index"] for u in res["groups"]} == {0, 1}, \
        "populated zone candidate must sort normally"
    assert res["zone"]["applied"] is False \
        and "contains nodes" in res["zone"]["reason"], res["zone"]
    print("populated zone refused OK")

    # --- a rect matching no frame is reported honestly
    res = layout_sort.run_layout(two_group_workflow(),
                                 {"zone": [9999, 9999, 500, 500],
                                  "zone_index": 5})
    assert res["zone"]["applied"] is False \
        and "matches" in res["zone"]["reason"], res["zone"]
    print("unmatched zone reported OK")

    # --- fixed-frame sort: a selected POPULATED group keeps its exact
    # frame; only its members re-arrange inside it
    wf = make_workflow()
    wf["groups"] = [{"title": "Roomy",
                     "bounding": [40, 100, 900, 700]}]  # holds 4 and 10
    res = layout_sort.run_layout(
        wf, {"scope_ids": [4, 10],
             "frames": [{"rect": [40, 100, 900, 700], "title": "Roomy",
                         "ids": [4, 10]}]})
    assert res["groups"] == [], \
        f"the selected frame must never be updated: {res['groups']}"
    assert set(res["positions"]) == {"4", "10"}
    nodes = node_map(wf)
    for nid in (4, 10):
        assert rect_inside(visual_rect(nodes[nid],
                                       res["positions"][str(nid)]),
                           [40, 100, 900, 700]), \
            f"member {nid} left the fixed frame"
    assert res["frames"] == {"count": 1, "overflow": []}, res.get("frames")
    print("fixed frame OK")

    # --- too-small frame: sorted anyway, overflow reported honestly
    wf = make_workflow()
    wf["groups"] = [{"title": "Tiny", "bounding": [40, 100, 400, 200]}]
    res = layout_sort.run_layout(
        wf, {"scope_ids": [4, 10],
             "frames": [{"rect": [40, 100, 400, 200], "title": "Tiny",
                         "ids": [4, 10]}]})
    assert res["groups"] == [] and set(res["positions"]) == {"4", "10"}
    assert res["frames"]["overflow"] == ["Tiny"], res["frames"]
    # anchored at the frame's padded corner even when overflowing
    min_x = min(p[0] for p in res["positions"].values())
    assert abs(min_x - (40 + 24)) <= 10, min_x
    print("frame overflow OK")

    # --- mixed selection: frame fixed, extra loose node sorts separately
    wf = make_workflow()
    wf["groups"] = [{"title": "Roomy", "bounding": [40, 100, 900, 700]}]
    res = layout_sort.run_layout(
        wf, {"scope_ids": [4, 10, 9],
             "frames": [{"rect": [40, 100, 900, 700], "title": "Roomy",
                         "ids": [4, 10]}]})
    assert set(res["positions"]) == {"4", "10", "9"}
    assert res["groups"] == [], res["groups"]
    print("mixed frame + loose OK")

    print("ALL SCOPED-SORT TESTS PASSED")


if __name__ == "__main__":
    main()
