"""Route-layer tests for /layout_sort/compute and /layout_sort/api_key.

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
import tempfile
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
llm_client = sys.modules["Comfyui-Layout-Sort.llm_client"]


class FakeRequest:
    def __init__(self, body=None, raise_json=False):
        self._body = body
        self._raise = raise_json

    async def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._body


def call(method, path, request):
    return asyncio.run(HANDLERS[(method, path)](request))


def main():
    assert ("POST", "/layout_sort/compute") in HANDLERS, "compute not registered"
    assert ("GET", "/layout_sort/api_key") in HANDLERS, "key GET not registered"
    assert ("POST", "/layout_sort/api_key") in HANDLERS, "key POST not registered"

    # --- /layout_sort/compute -------------------------------------------
    r = call("POST", "/layout_sort/compute", FakeRequest(raise_json=True))
    assert r.status == 400 and "error" in r.data, (r.status, r.data)

    r = call("POST", "/layout_sort/compute", FakeRequest(body=[1, 2]))
    assert r.status == 400, "non-object body must be a 400, not a 500"

    workflow = {"nodes": [{"id": 1, "type": "A", "pos": [0, 0],
                           "size": [100, 50], "flags": {}}], "links": []}
    r = call("POST", "/layout_sort/compute", FakeRequest(body={"workflow": workflow}))
    assert r.status == 200 and set(r.data["positions"]) == {"1"}, (r.status, r.data)

    r = call("POST", "/layout_sort/compute",
             FakeRequest(body={"workflow": workflow, "options": "boom"}))
    assert r.status == 500 and "error" in r.data, \
        "internal errors must come back as JSON 500"
    print("compute route OK")

    # --- /layout_sort/api_key -------------------------------------------
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    handle.close()
    os.environ[layout_sort.KEY_FILE_ENV_VAR] = handle.name
    try:
        r = call("GET", "/layout_sort/api_key", FakeRequest())
        assert r.data == {"configured": False, "source": "none"}, r.data

        r = call("POST", "/layout_sort/api_key",
                 FakeRequest(body={"api_key": "sk-route"}))
        assert r.status == 200 and r.data == {"configured": True,
                                              "source": "file"}, (r.status, r.data)
        assert layout_sort.load_stored_api_key() == "sk-route"

        r = call("POST", "/layout_sort/api_key",
                 FakeRequest(body={"api_key": "x" * (layout_sort.MAX_KEY_LENGTH + 1)}))
        assert r.status == 400, "over-long key must be rejected"
        assert layout_sort.load_stored_api_key() == "sk-route", "must not overwrite"

        r = call("POST", "/layout_sort/api_key",
                 FakeRequest(body={"api_key": "sk-ZZSECRETZZ\tX"}))
        assert r.status == 400, "control chars must be rejected"
        assert "ZZSECRETZZ" not in json.dumps(r.data), "key echoed in error"
        assert layout_sort.load_stored_api_key() == "sk-route", "must not overwrite"

        r = call("POST", "/layout_sort/api_key", FakeRequest(body="nope"))
        assert r.status == 400, "non-object body must be a 400"

        r = call("POST", "/layout_sort/api_key", FakeRequest(raise_json=True))
        assert r.status == 400, "invalid json must be a 400"

        # Clearing, then env-var fallback drives the reported source.
        r = call("POST", "/layout_sort/api_key", FakeRequest(body={"api_key": ""}))
        assert r.status == 200 and r.data["configured"] is False, (r.status, r.data)
        assert not os.path.exists(handle.name), "clear must delete the file"

        os.environ[llm_client.API_KEY_ENV_VAR] = "sk-env"
        try:
            r = call("GET", "/layout_sort/api_key", FakeRequest())
            assert r.data == {"configured": True, "source": "env"}, r.data
        finally:
            del os.environ[llm_client.API_KEY_ENV_VAR]
        print("api_key route OK")
    finally:
        os.environ.pop(layout_sort.KEY_FILE_ENV_VAR, None)
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    print("ALL ROUTE TESTS PASSED")


if __name__ == "__main__":
    main()
