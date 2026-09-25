"""Route-layer tests for /layout_sort/compute.

ComfyUI's `server` module and aiohttp are not available in the test
environment, so minimal shims are installed BEFORE the package import;
the registered handlers are then invoked directly with a fake request.

Run: python3 tests/test_routes.py (no ComfyUI required).
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HANDLERS = {}


class _Routes:
    def post(self, path):
        def decorator(fn):
            HANDLERS[("POST", path)] = fn
            return fn
        return decorator

    def get(self, path):
        def decorator(fn):
            HANDLERS[("GET", path)] = fn
            return fn
        return decorator


def _install_shims():
    server_mod = types.ModuleType("server")

    class PromptServer:
        pass

    PromptServer.instance = types.SimpleNamespace(
        routes=_Routes(), client_id=None, send_sync=lambda *a, **k: None)
    server_mod.PromptServer = PromptServer

    aiohttp_mod = types.ModuleType("aiohttp")
    web_mod = types.ModuleType("aiohttp.web")

    def json_response(data, status=200):
        json.dumps(data)  # must be JSON-serializable, like the real thing
        return types.SimpleNamespace(status=status, data=data)

    web_mod.json_response = json_response
    aiohttp_mod.web = web_mod
    sys.modules["server"] = server_mod
    sys.modules["aiohttp"] = aiohttp_mod
    sys.modules["aiohttp.web"] = web_mod


_install_shims()
spec = importlib.util.spec_from_file_location(
    "Comfyui-Layout-Sort", os.path.join(PKG_DIR, "__init__.py"),
    submodule_search_locations=[PKG_DIR])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
layout_sort = sys.modules["Comfyui-Layout-Sort.layout_sort"]


class FakeRequest:
    def __init__(self, body=None, raise_json=False,
                 content_type="application/json"):
        self._body = body
        self._raise = raise_json
        self.content_type = content_type

    async def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._body


def call(method, path, request):
    return asyncio.run(HANDLERS[(method, path)](request))


def main():
    node_cls = layout_sort.LayoutSort
    required = node_cls.INPUT_TYPES()["required"]
    assert not any(k.startswith("llm") for k in required), \
        "LLM inputs must be gone"
    assert list(required) == ["direction", "layer_spacing", "node_spacing",
                              "group_mode", "style", "shape", "animate"], \
        list(required)
    style_decl = node_cls.INPUT_TYPES()["required"]["style"]
    assert style_decl[1]["default"] == "flow", style_decl
    print("node declaration OK")

    assert ("POST", "/layout_sort/compute") in HANDLERS, "compute not registered"
    assert set(HANDLERS) == {("POST", "/layout_sort/compute")}, \
        f"only the compute route may exist: {sorted(HANDLERS)}"

    # --- /layout_sort/compute -------------------------------------------
    r = call("POST", "/layout_sort/compute", FakeRequest(raise_json=True))
    assert r.status == 400 and "error" in r.data, (r.status, r.data)

    r = call("POST", "/layout_sort/compute", FakeRequest(body=[1, 2]))
    assert r.status == 400, "non-object body must be a 400, not a 500"

    workflow = {"nodes": [{"id": 1, "type": "A", "pos": [0, 0],
                           "size": [100, 50], "flags": {}}], "links": []}
    r = call("POST", "/layout_sort/compute", FakeRequest(body={"workflow": workflow}))
    assert r.status == 200 and set(r.data["positions"]) == {"1"}, (r.status, r.data)
    assert r.data["group_count"] == 0, r.data
    assert "llm" not in r.data, r.data

    # CSRF hardening: non-JSON content types are rejected before parsing
    # on every POST route (browser form posts carry text/plain or
    # form-urlencoded and never application/json without CORS).
    for path in ("/layout_sort/compute",):
        r = call("POST", path, FakeRequest(body={"workflow": workflow},
                                           content_type="text/plain"))
        assert r.status == 400 and "content-type" in r.data["error"], \
            (path, r.status, r.data)

    r = call("POST", "/layout_sort/compute",
             FakeRequest(body={"workflow": workflow, "options": "boom"}))
    assert r.status == 500 and "error" in r.data, \
        "internal errors must come back as JSON 500"
    print("compute route OK")

    # API-format exports (no positions/links) must fail loudly.
    api_format = {"3": {"inputs": {}, "class_type": "KSampler"},
                  "4": {"inputs": {}, "class_type": "SaveImage"}}
    r = call("POST", "/layout_sort/compute",
             FakeRequest(body={"workflow": api_format}))
    assert r.status == 500 and "API-format" in r.data["error"], \
        (r.status, r.data)
    print("api-format guard OK")

    print("ALL ROUTE TESTS PASSED")


if __name__ == "__main__":
    main()
