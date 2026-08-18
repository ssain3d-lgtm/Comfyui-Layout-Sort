"""ComfyUI node + server glue for Layout Sort.

Two ways to trigger a sort:
  * Run the workflow: the LayoutSort node reads the serialized workflow
    from the hidden EXTRA_PNGINFO input, computes the layout on the
    backend, and pushes new positions to the browser over the websocket.
  * Press the "Sort now" button on the node (added by web/layoutSort.js):
    the frontend POSTs the current graph to /layout_sort/compute and
    applies the returned positions immediately, no queue needed.

Optionally, LM Studio (or any OpenAI-compatible local server) can suggest
semantic clusters for nodes that are not inside any group; those clusters
are laid out like groups and created as named frames on the canvas. Every
LLM failure falls back to a plain sort.
"""

import asyncio
import functools
import json
import os
import tempfile

from .layout_core import compute_layout
from .llm_client import (
    DEFAULT_BASE_URL,
    format_origin,
    is_valid_api_key,
    list_models,
    resolve_base_url,
    suggest_clusters,
)

WS_EVENT = "layout_sort_apply"
LLM_TIMEOUT_SECONDS = 120

# The style dropdown expands to engine options; explicit per-option keys
# in the request still win over the preset. Node sizes are never touched.
STYLE_PRESETS = {
    # Top-left aligned columns snapped to the canvas grid — the tidy look.
    "grid": {"align": "top"},
    # Centered columns — favors short, readable links.
    "flow": {"align": "center"},
}

# The API key is intentionally NOT a node widget: widget values get
# serialized into workflow JSON and PNG metadata, leaking the secret with
# every shared file. Instead it lives server-side only — in a file set via
# the node's key dialog (or the LAYOUT_SORT_LLM_API_KEY env var).
KEY_FILE_ENV_VAR = "LAYOUT_SORT_KEY_FILE"
KEY_FILE_NAME = "layout_sort_llm_api_key.txt"
MAX_KEY_LENGTH = 4096


def _key_file_path():
    override = os.environ.get(KEY_FILE_ENV_VAR, "").strip()
    if override:
        return override
    try:
        import folder_paths
        return os.path.join(folder_paths.get_user_directory(), KEY_FILE_NAME)
    except Exception:
        pass
    # Never fall back into custom_nodes: backup tools that ignore
    # .gitignore (e.g. Manager snapshots) could capture the key there.
    home = os.path.expanduser("~")
    base = home if home and home != "~" else tempfile.gettempdir()
    return os.path.join(base, ".comfyui-layout-sort", KEY_FILE_NAME)


def load_stored_key_info():
    """Return {"api_key": str, "allowed_origin": str|None}.

    The file is JSON; a bare-string file (pre-0.5 format) is treated as a
    key bound to no origin, i.e. loopback-only — the safe default.
    """
    try:
        with open(_key_file_path(), "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return {"api_key": "", "allowed_origin": None}
    if not raw:
        return {"api_key": "", "allowed_origin": None}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"api_key": raw, "allowed_origin": None}
    if not isinstance(data, dict):
        return {"api_key": "", "allowed_origin": None}
    origin = data.get("allowed_origin")
    return {
        "api_key": str(data.get("api_key") or "").strip(),
        "allowed_origin": str(origin) if origin else None,
    }


def load_stored_api_key():
    return load_stored_key_info()["api_key"]


def store_api_key(key, allowed_origin=None):
    """Persist (or clear, for empty keys) the server-side API key.

    `allowed_origin` binds the key to one non-loopback origin: it is only
    ever attached to requests for that origin or loopback targets, so a
    shared workflow pointing llm_base_url elsewhere cannot exfiltrate it.

    Raises ValueError for keys with non-printable/non-ASCII characters —
    they would corrupt the Authorization header and could echo the key
    into error messages (see llm_client.is_valid_api_key).
    """
    path = _key_file_path()
    if not key:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    if not is_valid_api_key(key):
        raise ValueError("API key contains invalid characters")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    payload = json.dumps({"api_key": key, "allowed_origin": allowed_origin})
    # Create owner-only atomically (no 0644 window between open and chmod);
    # the chmod still runs to tighten a pre-existing file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def run_layout(workflow, options, llm_cfg=None):
    """Shared pipeline for the node and the HTTP route: optionally ask the
    local LLM for clusters, then compute the layout (falling back to a
    plain sort whenever the LLM is unavailable)."""
    if not workflow.get("nodes") and any(
        isinstance(v, dict) and "class_type" in v
        for v in workflow.values() if isinstance(v, dict)
    ):
        # "Save (API format)" exports carry no positions/links/groups —
        # there is nothing to lay out. Fail loudly instead of no-opping.
        raise ValueError(
            "this looks like an API-format workflow export (no layout "
            "data); load it into ComfyUI and sort the live graph instead"
        )
    options = dict(options or {})
    style = STYLE_PRESETS.get(str(options.pop("style", "") or "").lower())
    if style:
        options = {**style,
                   **{k: v for k, v in options.items() if v is not None}}
    extra_clusters = None
    llm_info = None
    use_llm = bool(llm_cfg and llm_cfg.get("enabled"))
    if use_llm and (options.get("group_mode") or "cluster") != "cluster":
        llm_info = {"used": False,
                    "error": 'group_mode must be "cluster" for LLM clustering'}
    elif use_llm:
        # Priority: explicit (programmatic callers, who paired key and URL
        # themselves) > stored file (bound to its saved origin) > env var
        # (gated inside suggest_clusters).
        explicit_key = (llm_cfg.get("api_key") or "").strip()
        if explicit_key:
            api_key, key_origin = explicit_key, "*"
        else:
            stored = load_stored_key_info()
            api_key, key_origin = stored["api_key"], stored["allowed_origin"]
        clusters, error = suggest_clusters(
            workflow,
            base_url=resolve_base_url(llm_cfg.get("provider"),
                                      llm_cfg.get("base_url")),
            model=llm_cfg.get("model") or "",
            timeout=LLM_TIMEOUT_SECONDS,
            api_key=api_key,
            key_origin=key_origin,
            max_tokens=llm_cfg.get("max_tokens"),
        )
        if error:
            llm_info = {"used": False, "error": error}
        else:
            extra_clusters = clusters
            llm_info = {"used": True}
    result = compute_layout(workflow, options, extra_clusters)
    if llm_info and llm_info.get("used"):
        # Report what actually got created: geometric filtering inside
        # compute_layout (existing groups win) can drop suggestions.
        created = result.get("new_groups") or []
        if created:
            llm_info["clusters"] = len(created)
            llm_info["names"] = [g["title"] for g in created]
        else:
            llm_info = {"used": False,
                        "error": "all suggested clusters were already "
                                 "covered by existing groups"}
    if llm_info:
        result["llm"] = llm_info
    return result


try:
    from server import PromptServer
    from aiohttp import web
except ImportError:
    # Imported outside a running ComfyUI (e.g. unit tests): the node class
    # is still importable, only the live push/route are unavailable.
    PromptServer = None
else:
    async def _layout_sort_compute(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"},
                                     status=400)
        workflow = data.get("workflow") or {}
        options = data.get("options") or {}
        llm_cfg = data.get("llm") or {}
        try:
            # The LLM call can take a while; keep the event loop free.
            result = await asyncio.get_running_loop().run_in_executor(
                None, run_layout, workflow, options, llm_cfg
            )
        except Exception as exc:  # never take the server down over a sort
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    def _key_status():
        from .llm_client import API_KEY_ENV_VAR
        stored = load_stored_key_info()
        if stored["api_key"]:
            source = "file"
        elif os.environ.get(API_KEY_ENV_VAR, "").strip():
            source = "env"
        else:
            source = "none"
        # Status only — the key itself is never sent back to any client.
        return web.json_response({
            "configured": source != "none",
            "source": source,
            "allowed_origin": stored["allowed_origin"],
        })

    async def _layout_sort_key_get(_request):
        return _key_status()

    async def _layout_sort_key_set(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"},
                                     status=400)
        key = str(data.get("api_key") or "").strip()
        if len(key) > MAX_KEY_LENGTH:
            return web.json_response({"error": "key too long"}, status=400)
        # Bind the key to the endpoint the node pointed at when it was
        # saved; loopback targets are always allowed regardless. With no
        # hint at all, the key stays loopback-only (None).
        hint = str(data.get("origin_hint") or "").strip()
        provider = str(data.get("provider") or "").strip()
        if provider or hint:
            allowed_origin = format_origin(resolve_base_url(provider, hint))
        else:
            allowed_origin = None
        try:
            store_api_key(key, allowed_origin)
        except ValueError as exc:
            # Message is fixed text — never contains the key value.
            return web.json_response({"error": str(exc)}, status=400)
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        return _key_status()

    async def _layout_sort_models(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"error": "body must be an object"},
                                     status=400)
        stored = load_stored_key_info()
        models, error = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                list_models,
                base_url=resolve_base_url(data.get("provider"),
                                          str(data.get("base_url") or "")),
                api_key=stored["api_key"],
                key_origin=stored["allowed_origin"],
            ),
        )
        return web.json_response({"models": models, "error": error})

    try:
        PromptServer.instance.routes.post("/layout_sort/compute")(
            _layout_sort_compute
        )
        PromptServer.instance.routes.get("/layout_sort/api_key")(
            _layout_sort_key_get
        )
        PromptServer.instance.routes.post("/layout_sort/api_key")(
            _layout_sort_key_set
        )
        PromptServer.instance.routes.post("/layout_sort/models")(
            _layout_sort_models
        )
    except Exception as exc:  # keep the node usable even if the routes fail
        import logging
        logging.getLogger("ComfyUI-Layout-Sort").warning(
            "could not register /layout_sort routes: %s", exc
        )


class AnyType(str):
    """Wildcard type so the optional trigger input accepts any connection."""

    def __ne__(self, other):
        return False


ANY = AnyType("*")


class LayoutSort:
    """Arranges every node in the current workflow by data flow when executed."""

    CATEGORY = "utils/layout"
    FUNCTION = "sort"
    RETURN_TYPES = ()
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "direction": (
                    ["left_to_right", "top_to_bottom"],
                    {"default": "left_to_right"},
                ),
                "layer_spacing": (
                    "INT",
                    {"default": 80, "min": 10, "max": 500, "step": 10,
                     "tooltip": "Gap between layers (columns) in pixels."},
                ),
                "node_spacing": (
                    "INT",
                    {"default": 40, "min": 10, "max": 500, "step": 10,
                     "tooltip": "Gap between nodes inside a layer in pixels."},
                ),
                "group_mode": (
                    ["cluster", "inner", "refit"],
                    {"default": "cluster",
                     "tooltip": "cluster: lay out each group as a block, then "
                                "arrange the blocks (frames never overlap). "
                                "inner: keep every group where you put it and "
                                "only tidy the nodes inside each one "
                                "(ungrouped nodes stay untouched). "
                                "refit: ignore groups while sorting, then "
                                "re-wrap each frame around its old members."},
                ),
                "style": (
                    ["grid", "flow"],
                    {"default": "grid",
                     "tooltip": "grid: top-left aligned columns snapped to "
                                "the canvas grid. flow: centered columns — "
                                "favors short links. Node sizes are never "
                                "changed."},
                ),
                "animate": ("BOOLEAN", {"default": True}),
                "llm_clustering": (
                    "BOOLEAN",
                    {"default": False,
                     "tooltip": "Ask an LLM (local LM Studio/Ollama, or "
                                "OpenAI/Anthropic cloud) to group ungrouped "
                                "nodes by function and create named frames "
                                "for them. Falls back to a plain sort when "
                                "the server is unreachable."},
                ),
                "llm_provider": (
                    ["lmstudio", "ollama", "openai", "anthropic", "custom"],
                    {"default": "lmstudio",
                     "tooltip": "Where the LLM runs. lmstudio/ollama = "
                                "local; openai = ChatGPT API; anthropic = "
                                "Claude API; custom = use llm_base_url."},
                ),
                "llm_base_url": (
                    "STRING",
                    {"default": DEFAULT_BASE_URL,
                     "tooltip": "Endpoint used when llm_provider is "
                                "\"custom\" (any OpenAI-compatible server). "
                                "Presets fill this in for reference."},
                ),
                # A combo whose real options arrive at runtime: the Connect
                # button fills widget.options.values from /layout_sort/models.
                # VALIDATE_INPUTS below skips the stock is-it-in-the-list
                # check, so any fetched model id validates.
                "llm_model": (
                    ["auto"],
                    {"default": "auto",
                     "tooltip": "Model to use. \"auto\" picks the first model "
                                "loaded in the server; press the Connect "
                                "button to list the available models and "
                                "choose one."},
                ),
                "llm_max_tokens": (
                    "INT",
                    {"default": 4096, "min": 256, "max": 262144, "step": 256,
                     "tooltip": "Completion token budget for the LLM. "
                                "Thinking models spend tokens reasoning "
                                "before answering — raise this if you see "
                                "token-limit errors."},
                ),
            },
            "optional": {
                "trigger": (
                    ANY,
                    {"tooltip": "Optional. Connect any output here to control "
                                "when the sort runs during execution."},
                ),
            },
            "hidden": {
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Re-run on every queue: sorting is a side effect, never cached.
        return float("nan")

    @classmethod
    def VALIDATE_INPUTS(cls, llm_model):
        # llm_model's combo options are dynamic (fetched from the LLM
        # server at runtime), so the default list-membership validation
        # would reject every real model id.
        return True

    def sort(self, direction, layer_spacing, node_spacing, group_mode, style,
             animate, llm_clustering, llm_provider, llm_base_url, llm_model,
             llm_max_tokens, trigger=None, extra_pnginfo=None, unique_id=None):
        workflow = (extra_pnginfo or {}).get("workflow")
        server = getattr(PromptServer, "instance", None) if PromptServer else None
        if not workflow or server is None:
            return {}
        result = run_layout(
            workflow,
            {
                "direction": direction,
                "h_spacing": layer_spacing,
                "v_spacing": node_spacing,
                "group_mode": group_mode,
                "style": style,
            },
            {
                "enabled": llm_clustering,
                "provider": llm_provider,
                "base_url": llm_base_url,
                "model": llm_model,
                "max_tokens": llm_max_tokens,
            },
        )
        # Target the client that queued this prompt; fall back to broadcast.
        sid = getattr(server, "client_id", None)
        server.send_sync(WS_EVENT, {
            "positions": result["positions"],
            "groups": result["groups"],
            "new_groups": result.get("new_groups") or [],
            "reroutes": result.get("reroutes") or {},
            "llm": result.get("llm"),
            "animate": bool(animate),
            "source_node": unique_id,
        }, sid)
        return {}


NODE_CLASS_MAPPINGS = {
    "LayoutSort": LayoutSort,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LayoutSort": "Layout Sort (Auto Arrange Workflow)",
}
