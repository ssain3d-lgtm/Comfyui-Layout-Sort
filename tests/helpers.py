"""Shared fixtures for the test scripts: imports the package the way
ComfyUI does (relative imports inside) and provides a small txt2img-ish
workflow plus geometry helpers."""
import importlib.util
import os
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if "Comfyui-Layout-Sort" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "Comfyui-Layout-Sort", os.path.join(PKG_DIR, "__init__.py"),
        submodule_search_locations=[PKG_DIR])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
layout_sort = sys.modules["Comfyui-Layout-Sort.layout_sort"]

TITLE_HEIGHT = 30.0


def make_workflow(with_group=False):
    """~8-node txt2img-ish graph, ComfyUI array-form links."""
    nodes = [
        {"id": 4, "type": "CheckpointLoaderSimple", "pos": [100, 200],
         "size": [315, 98], "flags": {}},
        {"id": 10, "type": "VAELoader", "pos": [120, 400],
         "size": [315, 126], "flags": {}},
        {"id": 6, "type": "CLIPTextEncode", "pos": [500, 200],
         "size": [400, 200], "flags": {}},
        {"id": 7, "type": "CLIPTextEncode", "pos": [500, 500],
         "size": [400, 200], "flags": {}},
        {"id": 5, "type": "EmptyLatentImage", "pos": [520, 800],
         "size": [315, 106], "flags": {}},
        {"id": 3, "type": "KSampler", "pos": [1000, 300],
         "size": [315, 262], "flags": {}},
        {"id": 8, "type": "VAEDecode", "pos": [1400, 300],
         "size": [210, 46], "flags": {}},
        {"id": 9, "type": "SaveImage", "pos": [1700, 300],
         "size": [315, 270], "flags": {}},
    ]
    links = [
        # [link_id, origin, origin_slot, target, target_slot, "TYPE"]
        [1, 4, 0, 3, 0, "MODEL"],
        [2, 4, 1, 6, 0, "CLIP"],
        [3, 4, 1, 7, 0, "CLIP"],
        [4, 6, 0, 3, 1, "CONDITIONING"],
        [5, 7, 0, 3, 2, "CONDITIONING"],
        [6, 5, 0, 3, 3, "LATENT"],
        [7, 3, 0, 8, 0, "LATENT"],
        [8, 10, 0, 8, 1, "VAE"],
        [9, 8, 0, 9, 0, "IMAGE"],
    ]
    wf = {"nodes": nodes, "links": links, "groups": []}
    if with_group:
        # Geometrically contains the visual centers of nodes 4 (257.5, 234)
        # and 10 (277.5, 448) and of no other node.
        wf["groups"] = [{"title": "My Loaders",
                        "bounding": [50, 100, 500, 500]}]
    return wf


def node_map(wf):
    return {n["id"]: n for n in wf["nodes"]}


def visual_rect(node, pos):
    """Visual rect (x, y, w, h) from a returned LiteGraph pos [px, py]."""
    w = max(float(node["size"][0]), 1.0)
    collapsed = bool((node.get("flags") or {}).get("collapsed"))
    body = 0.0 if collapsed else max(float(node["size"][1]), 1.0)
    return (float(pos[0]), float(pos[1]) - TITLE_HEIGHT, w, body + TITLE_HEIGHT)


def rect_inside(rect, bounding, eps=1e-6):
    x, y, w, h = rect
    bx, by, bw, bh = bounding
    return (bx - eps <= x and by - eps <= y
            and x + w <= bx + bw + eps and y + h <= by + bh + eps)


def center_inside(rect, bounding):
    x, y, w, h = rect
    bx, by, bw, bh = bounding
    cx, cy = x + w / 2.0, y + h / 2.0
    return bx <= cx <= bx + bw and by <= cy <= by + bh

