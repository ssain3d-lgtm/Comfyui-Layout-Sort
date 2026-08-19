"""ComfyUI node + server glue for Layout Sort.

Two ways to trigger a sort:
  * Run the workflow: the LayoutSort node reads the serialized workflow
    from the hidden EXTRA_PNGINFO input, computes the layout on the
    backend, and pushes new positions to the browser over the websocket.
  * Press the "Sort now" button on the node (added by web/layoutSort.js):
    the frontend POSTs the current graph to /layout_sort/compute and
    applies the returned positions immediately, no queue needed.

Optionally, typing a request into the node's llm_prompt widget routes it
through an LLM (LM Studio/Ollama locally, or the OpenAI/Anthropic APIs):
the model reads a digest of the workflow and translates the request into
the sorter's own controls — direction, spacings, group mode, style, and
named clusters for ungrouped nodes. The model never places nodes, and an
empty prompt never contacts any LLM. Every LLM failure falls back to a
plain sort with the widget settings.
"""

import asyncio
import functools
import json
import os
import tempfile

from .layout_core import (
    TITLE_HEIGHT,
    _center,
    _group_contains,
    _normalize_groups,
    _normalize_nodes,
    compute_layout,
)
from .llm_client import (
    DEFAULT_BASE_URL,
    format_origin,
    is_valid_api_key,
    list_models,
    plan_layout,
    resolve_base_url,
)

WS_EVENT = "layout_sort_apply"
PROGRESS_EVENT = "layout_sort_progress"
LLM_TIMEOUT_SECONDS = 120

# The style dropdown expands to engine options; explicit per-option keys
# in the request still win over the preset. Node sizes are never touched.
STYLE_PRESETS = {
    # Centered columns — fewer crossings on big graphs (the default).
    "flow": {"align": "center"},
    # Top-left aligned columns snapped to the canvas grid — the tidy look
    # for small graphs.
    "grid": {"align": "top"},
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


def _scoped_workflow(workflow, scope_ids):
    """A copy of the workflow reduced to the selected nodes.

    Keeps links whose both endpoints are selected, and group frames whose
    every member (by the engine's center-containment rule) is selected —
    a partially selected group's frame stays untouched and its selected
    members sort as loose nodes. Returns (scoped_workflow, index_map)
    where index_map translates scoped group indices back to the original
    workflow's group indices.
    """
    ids = {str(v) for v in scope_ids}
    all_nodes = _normalize_nodes(workflow)
    kept_nodes = [
        raw for raw in workflow.get("nodes") or []
        if isinstance(raw, dict) and str(raw.get("id")) in ids
    ]
    kept_ids = {str(raw.get("id")) for raw in kept_nodes}

    kept_links = []
    for raw in workflow.get("links") or []:
        if isinstance(raw, (list, tuple)) and len(raw) >= 5:
            origin, target = raw[1], raw[3]
        elif isinstance(raw, dict):
            origin, target = raw.get("origin_id"), raw.get("target_id")
        else:
            continue
        if str(origin) in kept_ids and str(target) in kept_ids:
            kept_links.append(raw)

    kept_groups, index_map = [], {}
    raw_groups = workflow.get("groups") or []
    for group in _normalize_groups(workflow):
        members = [
            nid for nid, node in all_nodes.items()
            if _group_contains(group, *_center(node))
        ]
        if members and all(str(nid) in kept_ids for nid in members):
            index_map[len(kept_groups)] = group["index"]
            kept_groups.append(raw_groups[group["index"]])

    scoped = dict(workflow)
    scoped["nodes"] = kept_nodes
    scoped["links"] = kept_links
    scoped["groups"] = kept_groups
    return scoped, index_map


def _drop_empty_group(workflow, index):
    """Remove group `index` when it is verifiably EMPTY (a drawn zone).

    Returns (workflow_copy, index_map new->old) or (workflow, None) when
    the group has members or the index is invalid — a populated group is
    content, never a zone specification."""
    raw_groups = workflow.get("groups") or []
    if not isinstance(index, int) or not (0 <= index < len(raw_groups)):
        return workflow, None
    target = next((g for g in _normalize_groups(workflow)
                   if g["index"] == index), None)
    if target is None:
        return workflow, None
    nodes = _normalize_nodes(workflow)
    if any(_group_contains(target, *_center(n)) for n in nodes.values()):
        return workflow, None
    out = dict(workflow)
    out["groups"] = [g for i, g in enumerate(raw_groups) if i != index]
    survivors = [i for i in range(len(raw_groups)) if i != index]
    return out, {new: old for new, old in enumerate(survivors)}


def run_layout(workflow, options, llm_cfg=None, progress=None):
    """Shared pipeline for the node and the HTTP route.

    With a non-empty llm_cfg["prompt"], the LLM first translates the
    request into validated engine options (which win over the widget
    values for this run) and optional named clusters; geometry itself is
    always computed deterministically. An empty prompt — or any LLM
    failure — is a plain sort with the widget settings.

    options["scope_ids"] (node id list) restricts the sort to a
    selection: only those nodes move, anchored where the selection sits,
    and everything else — including partially selected group frames — is
    left exactly as it was.

    `progress`, when given, is called with a stage string as the run
    advances — "llm_request" (about to ask the model, the long part),
    "llm_done" (reply received or failed), "layout" (deterministic
    geometry) — so a UI can show what the wait is spent on. Progress
    callbacks may never break the sort."""

    def notify(stage):
        if progress is None:
            return
        try:
            progress(stage)
        except Exception:
            pass

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
    full_group_count = len(workflow.get("groups") or [])
    group_mode = str(options.get("group_mode") or "cluster")

    # A selected EMPTY group frame acts as a drawn zone: the layout is
    # shaped to its proportions and placed at its corner. The zone frame
    # itself is dropped from the compute copy (it is the specification,
    # not content) and stays exactly where the user drew it.
    zone_rect = None
    raw_zone = options.pop("zone", None)
    zone_index = options.pop("zone_index", None)
    group_index_map = None
    if (group_mode != "inner"
            and isinstance(raw_zone, (list, tuple)) and len(raw_zone) >= 4):
        try:
            candidate = [float(v) for v in raw_zone[:4]]
        except (TypeError, ValueError):
            candidate = None
        if candidate and candidate[2] > 0 and candidate[3] > 0:
            workflow, group_index_map = _drop_empty_group(workflow,
                                                          zone_index)
            if group_index_map is not None:
                zone_rect = candidate
                options["zone_size"] = [candidate[2], candidate[3]]

    scope_ids = options.pop("scope_ids", None)
    if scope_ids:
        workflow, scope_map = _scoped_workflow(workflow, scope_ids)
        if group_index_map is None:
            group_index_map = scope_map
        else:
            group_index_map = {new: group_index_map[mid]
                               for new, mid in scope_map.items()}
    extra_clusters = None
    llm_info = None
    prompt = str((llm_cfg or {}).get("prompt") or "").strip()
    if prompt:
        # Priority: explicit (programmatic callers, who paired key and URL
        # themselves) > stored file (bound to its saved origin) > env var
        # (gated inside plan_layout).
        explicit_key = (llm_cfg.get("api_key") or "").strip()
        if explicit_key:
            api_key, key_origin = explicit_key, "*"
        else:
            stored = load_stored_key_info()
            api_key, key_origin = stored["api_key"], stored["allowed_origin"]
        notify("llm_request")
        plan, error = plan_layout(
            workflow, prompt,
            current_options=options,
            base_url=resolve_base_url(llm_cfg.get("provider"),
                                      llm_cfg.get("base_url")),
            model=llm_cfg.get("model") or "",
            timeout=LLM_TIMEOUT_SECONDS,
            api_key=api_key,
            key_origin=key_origin,
            max_tokens=llm_cfg.get("max_tokens"),
        )
        notify("llm_done")
        if error:
            llm_info = {"used": False, "error": error}
        else:
            options.update(plan["options"])
            extra_clusters = plan["clusters"] or None
            llm_info = {"used": True, "note": plan["note"],
                        "applied": plan["options"],
                        "unsupported": list(plan["unsupported"])}
            if (extra_clusters
                    and (options.get("group_mode") or "cluster") != "cluster"):
                # compute_layout only materializes clusters in cluster
                # mode; say so instead of silently dropping them.
                llm_info["unsupported"].append(
                    'clusters need group_mode "cluster"')
                extra_clusters = None
    style = STYLE_PRESETS.get(str(options.pop("style", "") or "").lower())
    if style:
        options = {**style,
                   **{k: v for k, v in options.items() if v is not None}}
    notify("layout")
    result = compute_layout(workflow, options, extra_clusters)
    if group_index_map is not None:
        # Filtered copies renumber groups from 0; translate frame updates
        # back to the live graph's group indices.
        result["groups"] = [
            {**u, "index": group_index_map[u["index"]]}
            for u in result.get("groups") or []
            if u["index"] in group_index_map
        ]
    if (zone_rect is not None and result.get("positions")
            and (options.get("group_mode") or "cluster") != "inner"):
        # Land the reshaped content at the drawn box's corner (snapped so
        # the grid alignment survives). Sizes are never scaled: content
        # larger than the box overflows right/down at the box's ratio.
        min_x = min(
            [p[0] for p in result["positions"].values()]
            + [u["bounding"][0] for u in result.get("groups") or []]
            + [g["bounding"][0] for g in result.get("new_groups") or []])
        min_y = min(
            [p[1] - TITLE_HEIGHT for p in result["positions"].values()]
            + [u["bounding"][1] for u in result.get("groups") or []]
            + [g["bounding"][1] for g in result.get("new_groups") or []])
        dx = round((zone_rect[0] - min_x) / 10.0) * 10.0
        dy = round((zone_rect[1] - min_y) / 10.0) * 10.0
        if dx or dy:
            for p in result["positions"].values():
                p[0] += dx
                p[1] += dy
            for u in (result.get("groups") or []) + (result.get("new_groups")
                                                     or []):
                u["bounding"][0] += dx
                u["bounding"][1] += dy
            for p in (result.get("reroutes") or {}).values():
                p[0] += dx
                p[1] += dy
    # Frame updates are index-based; the frontend compares this against
    # the live graph so frames added/removed during a slow LLM round-trip
    # can never receive another frame's geometry.
    result["group_count"] = full_group_count
    if llm_info and llm_info.get("used") and extra_clusters:
        # Report what actually got created: geometric filtering inside
        # compute_layout (existing groups win) can drop suggestions.
        created = result.get("new_groups") or []
        llm_info["clusters"] = len(created)
        llm_info["names"] = [g["title"] for g in created]
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
    def _reject_non_json(request):
        """CSRF hardening: browsers can fire cross-site POSTs without a
        preflight only for form/text content types; requiring the JSON
        content type (which our frontend always sends) forces CORS."""
        if request.content_type != "application/json":
            return web.json_response(
                {"error": "content-type must be application/json"},
                status=400)
        return None

    async def _layout_sort_compute(request):
        rejected = _reject_non_json(request)
        if rejected is not None:
            return rejected
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

        def send_progress(stage):
            # Broadcast; tabs without a sort in flight ignore the event.
            # send_sync is thread-safe (it is how executing nodes push).
            try:
                PromptServer.instance.send_sync(PROGRESS_EVENT,
                                                {"stage": stage})
            except Exception:
                pass

        try:
            # The LLM call can take a while; keep the event loop free.
            result = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(run_layout, workflow, options,
                                        llm_cfg, progress=send_progress)
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
        rejected = _reject_non_json(request)
        if rejected is not None:
            return rejected
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
        rejected = _reject_non_json(request)
        if rejected is not None:
            return rejected
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
                    ["flow", "grid"],
                    {"default": "flow",
                     "tooltip": "flow: centered columns — fewer crossings, "
                                "best for big graphs. grid: top-left aligned "
                                "columns snapped to the canvas grid. Node "
                                "sizes are never changed."},
                ),
                "shape": (
                    ["auto", "square", "wide", "tall"],
                    {"default": "auto",
                     "tooltip": "Target canvas proportions. square = 1:1, "
                                "wide = 2:1, tall = 1:2 — long pipelines "
                                "fold into serpentine bands, tall graphs "
                                "spread into extra columns; group interiors "
                                "follow the same ratio. auto keeps the "
                                "natural flow. Tip: select an EMPTY group "
                                "frame before sorting to fit the layout "
                                "into that drawn box instead."},
                ),
                "animate": ("BOOLEAN", {"default": True}),
                "llm_prompt": (
                    "STRING",
                    {"default": "", "multiline": True,
                     "tooltip": "Optional. Describe how you want the sort "
                                "in plain language (any language) — e.g. "
                                "\"vertical, keep my groups, only tidy "
                                "insides\" or \"tighter spacing, group the "
                                "VAE nodes\". An LLM translates it into "
                                "this node's own settings; it never places "
                                "nodes itself. Leave empty for a plain "
                                "sort with no LLM involved."},
                ),
                "llm_provider": (
                    ["lmstudio", "ollama", "openai", "anthropic", "custom"],
                    {"default": "lmstudio",
                     "tooltip": "Where the LLM runs (only used when "
                                "llm_prompt is not empty). lmstudio/ollama "
                                "= local; openai = ChatGPT API; anthropic "
                                "= Claude API; custom = use llm_base_url."},
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
             shape, animate, llm_prompt, llm_provider, llm_base_url,
             llm_model, llm_max_tokens, trigger=None, extra_pnginfo=None,
             unique_id=None):
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
                "shape": shape,
            },
            {
                "prompt": llm_prompt,
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
            "group_count": result.get("group_count"),
        }, sid)
        return {}


NODE_CLASS_MAPPINGS = {
    "LayoutSort": LayoutSort,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LayoutSort": "Layout Sort (Auto Arrange Workflow)",
}
