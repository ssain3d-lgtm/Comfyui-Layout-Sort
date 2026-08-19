#!/usr/bin/env python3
"""End-to-end tests for the Comfyui-Layout-Sort prompt-driven LLM path.

Starts a mock OpenAI-compatible server (http.server on 127.0.0.1, port 0)
and drives llm_client.plan_layout and layout_sort.run_layout through
happy paths, plan validation (option whitelist + cluster cleaning), error
handling, the structured-output fallback, key security, and the "empty
prompt never contacts an LLM" rule.

Read-only with respect to the package repo; run with python3.
"""

import importlib.util
import json
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import os

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_INIT = os.path.join(PKG_DIR, "__init__.py")

# --- import the package the way ComfyUI does (relative imports inside) ------
spec = importlib.util.spec_from_file_location(
    "Comfyui-Layout-Sort", PKG_INIT, submodule_search_locations=[PKG_DIR])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
layout_sort = sys.modules["Comfyui-Layout-Sort.layout_sort"]
llm_client = sys.modules["Comfyui-Layout-Sort.llm_client"]

TITLE_HEIGHT = 30.0


# ---------------------------------------------------------------------------
# Mock OpenAI-compatible server
# ---------------------------------------------------------------------------

class MockLLMServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.requests = []           # [{"method","path","body","raw"}]
            self.chat_content = "{}"     # message.content returned on success
            self.fail_on_response_format = False  # 400 any POST containing it
            self.redirect_models_to = None  # 302 /v1/models to this URL
            self.finish_reason = "stop"
            self.anthropic_stop = "end_turn"

    def record(self, method, path, body, raw, auth=None, x_key=None):
        with self.lock:
            self.requests.append(
                {"method": method, "path": path, "body": body, "raw": raw,
                 "auth": auth, "x_key": x_key})

    def snapshot(self):
        with self.lock:
            return list(self.requests)


class MockLLMHandler(BaseHTTPRequestHandler):
    server_version = "MockLLM/1.0"

    def log_message(self, *_args):  # silence request logging
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.record("GET", self.path, None, "",
                           self.headers.get("Authorization"),
                           self.headers.get("x-api-key"))
        if self.path == "/v1/models":
            target = getattr(self.server, "redirect_models_to", None)
            if target:
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, {"data": [{"id": "qwen-test"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self.server.record("POST", self.path, body, raw,
                           self.headers.get("Authorization"),
                           self.headers.get("x-api-key"))
        if self.path == "/v1/messages":  # Anthropic Messages API shape
            self._send(200, {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": (body or {}).get("model", "?"),
                "content": [{"type": "text",
                             "text": self.server.chat_content}],
                "stop_reason": getattr(self.server, "anthropic_stop",
                                       "end_turn"),
            })
            return
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        has_rf = (isinstance(body, dict) and "response_format" in body) \
            or "response_format" in raw
        if self.server.fail_on_response_format and has_rf:
            self._send(400, {"error": {
                "message": "response_format is not supported by this model"}})
            return
        self._send(200, {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": (body or {}).get("model", "?"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": self.server.chat_content},
                "finish_reason": getattr(self.server, "finish_reason", "stop"),
            }],
        })


SERVER = None
BASE = None  # e.g. http://127.0.0.1:PORT/v1


# ---------------------------------------------------------------------------
# Fixtures + geometry helpers
# ---------------------------------------------------------------------------

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


PROMPT = "세로로 정렬하고 로더끼리 묶어줘"

PLAN_HAPPY = {
    "options": {"direction": "top_to_bottom", "v_spacing": 30},
    "clusters": [{"name": "Loaders", "node_ids": [4, 10]},
                 {"name": "Sampling", "node_ids": [3, 5]}],
    "note": "세로 방향, 간격 30, 클러스터 2개",
    "unsupported": [],
}

CONTENT_HAPPY = (
    "<think>the user wants vertical flow and loader clusters; 4 and 10 "
    "load models, 3 samples from latent 5.</think>\n"
    "Sure! Here is the plan:\n"
    "```json\n" + json.dumps(PLAN_HAPPY, ensure_ascii=False) + "\n```\n"
    "Let me know if you need anything else."
)

VALID_OPTIONS = {"direction": "top_to_bottom", "v_spacing": 30}


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


@case("1. happy path: think-block + fenced plan JSON, auto-picked model")
def case_happy_path():
    SERVER.chat_content = CONTENT_HAPPY
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT,
        current_options={"direction": "left_to_right", "h_spacing": 80},
        base_url=BASE, model="", timeout=30)
    assert err is None, f"expected no error, got: {err!r}"
    assert plan["options"] == VALID_OPTIONS, plan
    assert plan["clusters"] == [
        {"name": "Loaders", "node_ids": [4, 10]},
        {"name": "Sampling", "node_ids": [3, 5]},
    ], f"unexpected clusters: {plan['clusters']!r}"
    assert plan["note"] == PLAN_HAPPY["note"], plan
    assert plan["unsupported"] == [], plan

    reqs = SERVER.snapshot()
    gets = [r for r in reqs if r["method"] == "GET"]
    posts = [r for r in reqs if r["method"] == "POST"]
    assert any(g["path"] == "/v1/models" for g in gets), \
        f"model auto-pick never hit GET /v1/models: {gets!r}"
    assert len(posts) == 1 and posts[0]["path"] == "/v1/chat/completions", \
        f"expected exactly one chat POST, got: {[p['path'] for p in posts]!r}"
    body = posts[0]["body"]
    assert body["model"] == "qwen-test", \
        f"auto-picked model not sent in POST body: {body.get('model')!r}"
    assert "response_format" in body, "first attempt should send response_format"
    assert body["messages"][0]["role"] == "system"
    user_msg = body["messages"][1]["content"]
    for expected in ("NODES", "UNGROUPED NODE IDS", "LINKS",
                     "CURRENT SETTINGS: direction=left_to_right",
                     "USER REQUEST:\n" + PROMPT):
        assert expected in user_msg, f"digest missing {expected!r}"


@case("2. plan validation: option whitelist/clamp + cluster cleaning")
def case_validation():
    long_name = "L" * 75
    SERVER.chat_content = json.dumps({
        "options": {
            "direction": "diagonal",        # not in enum -> dropped
            "group_mode": "banana",         # not in enum -> dropped
            "style": "GRID",                # case-insensitive -> "grid"
            "h_spacing": "9000",            # clamps to 600
            "v_spacing": 3,                 # clamps to 10
            "detach_types": ["KSampler"],   # not whitelisted -> dropped
            "snap_grid": 0,                 # not whitelisted -> dropped
        },
        "clusters": [
            {"name": long_name, "node_ids": [4, 999, 10]},   # 999 unknown
            {"name": "Decode", "node_ids": [4, 8, 9]},        # 4 already taken
            {"name": "", "node_ids": ["3", 5]},               # "3" matches int 3
            {"name": "Solo", "node_ids": [6]},                # singleton -> drop
        ],
        "note": "n" * 500,                                    # truncated to 200
    })
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert err is None, f"expected no error, got: {err!r}"
    assert plan["options"] == {"style": "grid", "h_spacing": 600,
                               "v_spacing": 10}, plan["options"]
    assert plan["clusters"] == [
        {"name": "L" * 60, "node_ids": [4, 10]},          # truncated to 60
        {"name": "Decode", "node_ids": [8, 9]},           # 4 kept by cluster 1
        {"name": "Cluster 3", "node_ids": [3, 5]},        # name fallback
    ], f"unexpected cleaned clusters: {plan['clusters']!r}"
    assert len(plan["note"]) == 200, len(plan["note"])


@case("3. garbage content (no JSON) -> (None, error)")
def case_garbage():
    SERVER.chat_content = "I could not figure out any plan, sorry!"
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert plan is None, f"expected no plan, got: {plan!r}"
    assert isinstance(err, str) and err.strip(), \
        f"expected non-empty error message, got: {err!r}"


@case("4. dead server -> (None, error) and returns quickly")
def case_dead_server():
    # Grab a port that is definitely closed right now.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    start = time.monotonic()
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT,
        base_url=f"http://127.0.0.1:{dead_port}/v1", model="", timeout=8)
    elapsed = time.monotonic() - start
    assert plan is None, f"expected no plan, got: {plan!r}"
    assert isinstance(err, str) and err.strip(), \
        f"expected non-empty error, got: {err!r}"
    assert elapsed < 10.0, f"dead-server failure took {elapsed:.1f}s (>= 10s)"


@case("5. structured-output 400 -> retry without response_format succeeds")
def case_response_format_fallback():
    SERVER.chat_content = CONTENT_HAPPY
    SERVER.fail_on_response_format = True
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert err is None, f"expected success via retry, got error: {err!r}"
    assert [c["name"] for c in plan["clusters"]] == ["Loaders", "Sampling"], \
        f"unexpected clusters after retry: {plan['clusters']!r}"

    posts = [r for r in SERVER.snapshot()
             if r["method"] == "POST" and r["path"] == "/v1/chat/completions"]
    assert len(posts) == 2, f"expected 2 POST attempts, got {len(posts)}"
    assert "response_format" in posts[0]["body"], "first POST lost response_format"
    assert "response_format" not in posts[1]["body"], \
        "retry still contained response_format"


@case("6. run_layout: plan options override widgets, clusters become frames")
def case_run_layout_new_groups():
    SERVER.chat_content = CONTENT_HAPPY
    wf = make_workflow()
    # Widgets say horizontal; the prompt's plan says vertical and must win.
    res = layout_sort.run_layout(
        wf, {"direction": "left_to_right"},
        {"prompt": PROMPT, "base_url": BASE, "model": ""})

    llm = res["llm"]
    assert llm["used"] is True, f"llm info: {llm!r}"
    assert llm["applied"] == VALID_OPTIONS, llm
    assert llm["note"] == PLAN_HAPPY["note"], llm
    assert llm["clusters"] == 2 and llm["names"] == ["Loaders", "Sampling"], llm
    assert res["groups"] == [], f"expected no real-group updates: {res['groups']!r}"
    assert set(res["positions"].keys()) == {str(n["id"]) for n in wf["nodes"]}, \
        f"positions keys wrong: {sorted(res['positions'])!r}"

    # direction=top_to_bottom applied: every link flows downward.
    nodes = node_map(wf)
    pos = {int(k): v for k, v in res["positions"].items()}
    for _lid, origin, _os, target, _ts, _ty in wf["links"]:
        o_bottom = pos[origin][1] + nodes[origin]["size"][1]
        assert o_bottom <= pos[target][1] - TITLE_HEIGHT + 1e-6, \
            f"link {origin}->{target} does not flow top to bottom"

    ng = res["new_groups"]
    assert [g["title"] for g in ng] == ["Loaders", "Sampling"], \
        f"unexpected new_groups: {ng!r}"
    by_title = {g["title"]: g["bounding"] for g in ng}
    for title, members in (("Loaders", [4, 10]), ("Sampling", [3, 5])):
        bounding = by_title[title]
        for nid in members:
            rect = visual_rect(nodes[nid], res["positions"][str(nid)])
            assert rect_inside(rect, bounding), (
                f"node {nid} visual rect {rect} not inside new group "
                f"{title!r} bounding {bounding}")
    # Unclustered nodes must not sit inside any synthetic frame.
    for nid in (6, 7, 8, 9):
        rect = visual_rect(nodes[nid], res["positions"][str(nid)])
        for title, bounding in by_title.items():
            assert not center_inside(rect, bounding), (
                f"unclustered node {nid} landed inside new group {title!r}")


@case("7. user groups win: real group keeps its nodes, cluster shrinks/drops")
def case_user_groups_win():
    SERVER.chat_content = CONTENT_HAPPY  # plan claims [4,10] and [3,5]
    wf = make_workflow(with_group=True)  # real group already holds 4 and 10
    res = layout_sort.run_layout(
        wf, {}, {"prompt": PROMPT, "base_url": BASE, "model": ""})

    assert res["llm"]["used"] is True, f"llm info: {res.get('llm')!r}"

    groups = res["groups"]
    assert len(groups) == 1 and groups[0]["index"] == 0, \
        f"expected one update for real group 0: {groups!r}"
    real_bounding = groups[0]["bounding"]

    # The "Loaders" cluster loses both members to the real group -> fewer
    # than 2 free members -> the synthetic cluster is dropped entirely.
    ng = res["new_groups"]
    assert [g["title"] for g in ng] == ["Sampling"], (
        f"expected only the Sampling cluster to survive, got: {ng!r}")
    assert res["llm"]["clusters"] == 1 and res["llm"]["names"] == ["Sampling"], \
        res["llm"]
    sampling_bounding = ng[0]["bounding"]

    nodes = node_map(wf)
    for nid in (4, 10):
        rect = visual_rect(nodes[nid], res["positions"][str(nid)])
        assert rect_inside(rect, real_bounding), (
            f"real-group node {nid} rect {rect} not inside updated real "
            f"group bounding {real_bounding}")
        assert not center_inside(rect, sampling_bounding), (
            f"real-group node {nid} leaked into the synthetic Sampling frame")
    for nid in (3, 5):
        rect = visual_rect(nodes[nid], res["positions"][str(nid)])
        assert rect_inside(rect, sampling_bounding), (
            f"cluster node {nid} rect {rect} not inside Sampling bounding "
            f"{sampling_bounding}")
        assert not center_inside(rect, real_bounding), (
            f"cluster node {nid} leaked into the real group frame")


@case("8. plan clusters + non-cluster group_mode -> honest unsupported note")
def case_clusters_need_cluster_mode():
    SERVER.chat_content = json.dumps({
        "options": {},
        "clusters": [{"name": "Loaders", "node_ids": [4, 10]}],
        "note": "clustered the loaders",
    })
    wf = make_workflow()
    res = layout_sort.run_layout(
        wf, {"group_mode": "refit"},
        {"prompt": PROMPT, "base_url": BASE, "model": ""})
    llm = res.get("llm") or {}
    assert llm.get("used") is True, llm
    assert any("cluster" in u for u in llm.get("unsupported") or []), \
        f"expected a clusters-need-cluster-mode notice: {llm!r}"
    assert res["new_groups"] == [], \
        f"refit must not invent groups: {res['new_groups']!r}"
    assert set(res["positions"].keys()) == {str(n["id"]) for n in wf["nodes"]}, \
        "refit layout missing positions"


@case("9. dropped singleton cluster must NOT consume its node id")
def case_probe_singleton_consumes():
    # Ids are claimed only by clusters that survive the len>=2 check, so a
    # node listed in a discarded singleton X stays available to a later
    # valid cluster Y.
    SERVER.chat_content = json.dumps({"clusters": [
        {"name": "X", "node_ids": [6]},
        {"name": "Y", "node_ids": [6, 7]},
    ], "note": "x"})
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert err is None, f"unexpected error: {err!r}"
    assert plan["clusters"] == [{"name": "Y", "node_ids": [6, 7]}], \
        f"Y should survive with both members: {plan['clusters']!r}"


@case("10. api_key becomes a Bearer header; env var fallback; none by default")
def case_api_key():
    import os

    # Explicit key: every request (model pick + chat) carries the header.
    SERVER.chat_content = CONTENT_HAPPY
    _plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30,
        api_key="sk-test-123")
    assert err is None, f"unexpected error: {err!r}"
    auths = [r["auth"] for r in SERVER.snapshot()]
    assert auths and all(a == "Bearer sk-test-123" for a in auths), auths

    # No key: no Authorization header at all (LM Studio default setup).
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    _plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert err is None, f"unexpected error: {err!r}"
    auths = [r["auth"] for r in SERVER.snapshot()]
    assert auths and all(a is None for a in auths), auths

    # Env var fallback keeps tokens out of saved workflows.
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    os.environ[llm_client.API_KEY_ENV_VAR] = "sk-from-env"
    try:
        _plan, err = llm_client.plan_layout(
            make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    finally:
        del os.environ[llm_client.API_KEY_ENV_VAR]
    assert err is None, f"unexpected error: {err!r}"
    auths = [r["auth"] for r in SERVER.snapshot()]
    assert auths and all(a == "Bearer sk-from-env" for a in auths), auths


@case("11. server-side key store: file used, explicit beats file, clear works")
def case_key_store():
    import os
    import tempfile

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    os.environ[layout_sort.KEY_FILE_ENV_VAR] = handle.name
    try:
        # Stored key reaches the server as a Bearer header via run_layout.
        layout_sort.store_api_key("sk-stored")
        assert layout_sort.load_stored_api_key() == "sk-stored"
        if os.name == "posix":
            mode = os.stat(handle.name).st_mode & 0o777
            assert mode == 0o600, f"key file should be 0600, got {oct(mode)}"
        SERVER.reset()
        SERVER.chat_content = CONTENT_HAPPY
        res = layout_sort.run_layout(
            make_workflow(), {"group_mode": "cluster"},
            {"prompt": PROMPT, "base_url": BASE, "model": ""})
        assert res["llm"]["used"] is True, res["llm"]
        auths = [r["auth"] for r in SERVER.snapshot()]
        assert auths and all(a == "Bearer sk-stored" for a in auths), auths

        # An explicit key from a programmatic caller wins over the file.
        SERVER.reset()
        SERVER.chat_content = CONTENT_HAPPY
        layout_sort.run_layout(
            make_workflow(), {"group_mode": "cluster"},
            {"prompt": PROMPT, "base_url": BASE, "model": "",
             "api_key": "sk-explicit"})
        auths = [r["auth"] for r in SERVER.snapshot()]
        assert auths and all(a == "Bearer sk-explicit" for a in auths), auths

        # Clearing removes the file and the header.
        layout_sort.store_api_key("")
        assert layout_sort.load_stored_api_key() == ""
        assert not os.path.exists(handle.name), "clear must delete the file"

        # With no override (and no folder_paths in the test env) the
        # fallback must live OUTSIDE custom_nodes — backup tools that
        # ignore .gitignore could otherwise capture the key.
        os.environ.pop(layout_sort.KEY_FILE_ENV_VAR, None)
        fallback = os.path.abspath(layout_sort._key_file_path())
        assert not fallback.startswith(os.path.abspath(PKG_DIR) + os.sep), \
            f"key fallback inside the package dir: {fallback}"
    finally:
        os.environ.pop(layout_sort.KEY_FILE_ENV_VAR, None)
        try:
            os.unlink(handle.name)
        except OSError:
            pass


@case("12. invalid-character key is rejected without echoing the key")
def case_invalid_key_chars():
    import os
    import tempfile

    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    secret = "sk-ZZSECRETZZ\nX: injected"
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30,
        api_key=secret)
    assert plan is None and err, (plan, err)
    assert "ZZSECRETZZ" not in err, f"key leaked into error: {err!r}"
    assert not SERVER.snapshot(), "no request may reach the server"

    # store_api_key refuses it too, with a fixed message.
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    os.environ[layout_sort.KEY_FILE_ENV_VAR] = handle.name
    try:
        try:
            layout_sort.store_api_key(secret)
            raise AssertionError("store_api_key must reject control chars")
        except ValueError as exc:
            assert "ZZSECRETZZ" not in str(exc), str(exc)
    finally:
        os.environ.pop(layout_sort.KEY_FILE_ENV_VAR, None)
        try:
            os.unlink(handle.name)
        except OSError:
            pass


@case("13. Authorization is stripped on cross-origin redirects")
def case_redirect_strips_auth():
    import threading

    other = MockLLMServer(("127.0.0.1", 0), MockLLMHandler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        SERVER.reset()
        SERVER.chat_content = CONTENT_HAPPY
        other.chat_content = CONTENT_HAPPY
        # Different port = different origin, even on the same host.
        SERVER.redirect_models_to = (
            f"http://127.0.0.1:{other.server_address[1]}/v1/models")
        _plan, err = llm_client.plan_layout(
            make_workflow(), PROMPT, base_url=BASE, model="", timeout=30,
            api_key="sk-redirect-test")
        assert err is None, f"unexpected error: {err!r}"
        first_hop = [r for r in SERVER.snapshot() if r["method"] == "GET"]
        second_hop = [r for r in other.snapshot() if r["method"] == "GET"]
        assert first_hop and first_hop[0]["auth"] == "Bearer sk-redirect-test", \
            first_hop
        assert second_hop and second_hop[0]["auth"] is None, \
            f"token must not follow a cross-origin redirect: {second_hop}"
    finally:
        other.shutdown()


@case("14. stored keys are origin-bound; loopback targets always allowed")
def case_key_origin_binding():
    allowed = llm_client.key_allowed_for
    # Loopback targets always receive the key, whatever the binding.
    assert allowed("http://127.0.0.1:1234/v1", None)
    assert allowed("http://localhost:1234/v1", None)
    assert allowed(BASE, "https://api.example.com")
    # Non-loopback targets need an exact origin match.
    assert not allowed("https://evil.example/v1", None)
    assert not allowed("https://evil.example/v1", "https://api.example.com")
    assert allowed("https://api.example.com/v1", "https://api.example.com")
    assert allowed("https://api.example.com:443/v1", "https://api.example.com")
    assert not allowed("http://api.example.com/v1", "https://api.example.com")
    # "*" means the caller explicitly paired key and URL.
    assert allowed("https://anything.example/v1", "*")

    # Integration: a loopback-bound (None) stored key still reaches the
    # loopback mock server through plan_layout.
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    _plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30,
        api_key="sk-bound", key_origin=None)
    assert err is None, f"unexpected error: {err!r}"
    auths = [r["auth"] for r in SERVER.snapshot()]
    assert auths and all(a == "Bearer sk-bound" for a in auths), auths


@case("15. truncated thinking reply yields an actionable token-limit error")
def case_thinking_truncation():
    SERVER.reset()
    # Unterminated <think>: the model burned the whole budget reasoning.
    SERVER.chat_content = "<think>step 1... step 2... let me reconsider"
    SERVER.finish_reason = "length"
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert plan is None and err, (plan, err)
    assert "token limit" in err, f"error should explain the token limit: {err!r}"

    # An "auto" model value behaves like empty (auto-pick).
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="auto", timeout=30)
    assert err is None and plan, (plan, err)
    posts = [r for r in SERVER.snapshot() if r["method"] == "POST"]
    assert posts and posts[0]["body"]["model"] == "qwen-test", posts


@case("16. llm_max_tokens reaches the request; out-of-range values clamp")
def case_max_tokens():
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    _plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30,
        max_tokens=100000)
    assert err is None, err
    posts = [r for r in SERVER.snapshot() if r["method"] == "POST"]
    assert posts[0]["body"]["max_tokens"] == 100000, posts[0]["body"]

    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    llm_client.plan_layout(make_workflow(), PROMPT, base_url=BASE, model="",
                           timeout=30, max_tokens=99999999)
    posts = [r for r in SERVER.snapshot() if r["method"] == "POST"]
    assert posts[0]["body"]["max_tokens"] == llm_client.MAX_COMPLETION_TOKENS_CAP

    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    llm_client.plan_layout(make_workflow(), PROMPT, base_url=BASE, model="",
                           timeout=30, max_tokens=None)
    posts = [r for r in SERVER.snapshot() if r["method"] == "POST"]
    assert posts[0]["body"]["max_tokens"] == llm_client.DEFAULT_COMPLETION_TOKENS


@case("17. Anthropic provider: /v1/messages, x-api-key, parse + limit error")
def case_anthropic_provider():
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="claude-test",
        timeout=30, api_key="sk-ant-mock", max_tokens=8192,
        provider="anthropic")
    assert err is None and len(plan["clusters"]) == 2, (plan, err)
    posts = [r for r in SERVER.snapshot() if r["method"] == "POST"]
    assert posts and posts[0]["path"] == "/v1/messages", posts
    body = posts[0]["body"]
    assert body["model"] == "claude-test" and body["max_tokens"] == 8192
    assert "system" in body and "response_format" not in body, body
    assert "USER REQUEST:\n" + PROMPT in body["messages"][0]["content"], \
        "prompt missing from the Anthropic user message"
    assert posts[0]["x_key"] == "sk-ant-mock" and posts[0]["auth"] is None, \
        "Anthropic auth must use x-api-key, not Authorization"

    # Token exhaustion surfaces the actionable error.
    SERVER.reset()
    SERVER.chat_content = "I was about to answer but"
    SERVER.anthropic_stop = "max_tokens"
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="claude-test",
        timeout=30, provider="anthropic")
    assert plan is None and "token limit" in (err or ""), (plan, err)

    # Provider auto-detection by host.
    assert llm_client._provider_for("https://api.anthropic.com") == "anthropic"
    assert llm_client._provider_for("http://127.0.0.1:1234/v1") == "openai"
    assert llm_client._provider_for("https://api.openai.com/v1") == "openai"

    # llm_provider dropdown resolution.
    resolve = llm_client.resolve_base_url
    assert resolve("lmstudio", "http://ignored") == "http://127.0.0.1:1234/v1"
    assert resolve("ollama", "") == "http://127.0.0.1:11434/v1"
    assert resolve("openai", "") == "https://api.openai.com/v1"
    assert resolve("anthropic", "") == "https://api.anthropic.com"
    assert resolve("custom", "http://my.server:8080/v1") == "http://my.server:8080/v1"
    assert resolve("custom", "") == llm_client.DEFAULT_BASE_URL
    assert resolve(None, "http://my.server:8080/v1") == "http://my.server:8080/v1"


@case("18. empty/whitespace prompt never contacts any LLM")
def case_empty_prompt_no_llm():
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    for silent in ("", "   \n  ", None):
        wf = make_workflow()
        res = layout_sort.run_layout(
            wf, {"group_mode": "cluster"},
            {"prompt": silent, "base_url": BASE, "model": ""})
        assert "llm" not in res, f"llm info present for prompt {silent!r}"
        assert set(res["positions"].keys()) == \
            {str(n["id"]) for n in wf["nodes"]}
    assert not SERVER.snapshot(), \
        "no HTTP request may be made when the prompt is empty"
    # No llm_cfg at all behaves the same.
    res = layout_sort.run_layout(make_workflow(), {})
    assert "llm" not in res and not SERVER.snapshot()


@case("19. malformed plan values drop, not fatal; digest caps long fields")
def case_malformed_plan_values():
    # json.loads accepts the Infinity literal; a dict where a list is
    # expected must not raise either. The valid direction survives both.
    SERVER.reset()
    SERVER.chat_content = (
        '{"options": {"direction": "top_to_bottom", "h_spacing": Infinity},'
        ' "unsupported": {"a": "b"}, "note": "vertical"}'
    )
    plan, err = llm_client.plan_layout(
        make_workflow(), PROMPT, base_url=BASE, model="", timeout=30)
    assert err is None, f"malformed values must drop, not fail: {err!r}"
    assert plan["options"] == {"direction": "top_to_bottom"}, plan["options"]
    assert plan["unsupported"] == [] and plan["note"] == "vertical", plan

    # A multi-megabyte node title must not balloon the request digest.
    wf = make_workflow()
    wf["nodes"][0]["title"] = "T" * 1_000_000
    wf["groups"] = [{"title": "G" * 5000, "bounding": [0, 0, 10, 10]}]
    digest = llm_client.build_digest(wf)
    assert len(digest) < 20_000, f"digest ballooned to {len(digest)}"
    assert "T" * 200 not in digest and "G" * 200 not in digest

    # The staleness token for index-based frame updates is always present.
    SERVER.reset()
    SERVER.chat_content = CONTENT_HAPPY
    wf = make_workflow(with_group=True)
    res = layout_sort.run_layout(
        wf, {}, {"prompt": PROMPT, "base_url": BASE, "model": ""})
    assert res["group_count"] == 1, res.get("group_count")
    assert layout_sort.run_layout(make_workflow(), {})["group_count"] == 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    global SERVER, BASE
    SERVER = MockLLMServer(("127.0.0.1", 0), MockLLMHandler)
    BASE = f"http://127.0.0.1:{SERVER.server_address[1]}/v1"
    thread = threading.Thread(target=SERVER.serve_forever, daemon=True)
    thread.start()

    results = []
    try:
        for name, fn in CASES:
            SERVER.reset()
            try:
                fn()
                results.append((name, True, ""))
            except Exception:
                results.append((name, False, traceback.format_exc()))
    finally:
        SERVER.shutdown()
        SERVER.server_close()

    print(f"mock server: {BASE}")
    failed = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
            print("      " + "\n      ".join(detail.strip().splitlines()))
    print(f"\n{len(results) - failed}/{len(results)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
