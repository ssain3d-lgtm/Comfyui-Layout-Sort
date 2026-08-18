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

from .layout_core import compute_layout
from .llm_client import DEFAULT_BASE_URL, suggest_clusters

WS_EVENT = "layout_sort_apply"
LLM_TIMEOUT_SECONDS = 120


def run_layout(workflow, options, llm_cfg=None):
    """Shared pipeline for the node and the HTTP route: optionally ask the
    local LLM for clusters, then compute the layout (falling back to a
    plain sort whenever the LLM is unavailable)."""
    extra_clusters = None
    llm_info = None
    use_llm = bool(llm_cfg and llm_cfg.get("enabled"))
    if use_llm and (options.get("group_mode") or "cluster") != "cluster":
        llm_info = {"used": False,
                    "error": 'group_mode must be "cluster" for LLM clustering'}
    elif use_llm:
        clusters, error = suggest_clusters(
            workflow,
            base_url=llm_cfg.get("base_url") or DEFAULT_BASE_URL,
            model=llm_cfg.get("model") or "",
            timeout=LLM_TIMEOUT_SECONDS,
        )
        if error:
            llm_info = {"used": False, "error": error}
        else:
            extra_clusters = clusters
            llm_info = {
                "used": True,
                "clusters": len(clusters),
                "names": [c["name"] for c in clusters],
            }
    result = compute_layout(workflow, options, extra_clusters)
    if llm_info:
        result["llm"] = llm_info
    return result


try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.post("/layout_sort/compute")
    async def _layout_sort_compute(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
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

except ImportError:
    # Imported outside a running ComfyUI (e.g. unit tests): the node class
    # is still importable, only the live push/route are unavailable.
    PromptServer = None


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
                    ["cluster", "refit"],
                    {"default": "cluster",
                     "tooltip": "cluster: lay out each group as a block, then "
                                "arrange the blocks (frames never overlap). "
                                "refit: ignore groups while sorting, then "
                                "re-wrap each frame around its old members."},
                ),
                "animate": ("BOOLEAN", {"default": True}),
                "llm_clustering": (
                    "BOOLEAN",
                    {"default": False,
                     "tooltip": "Ask a local LLM (LM Studio / any "
                                "OpenAI-compatible server) to group ungrouped "
                                "nodes by function and create named frames "
                                "for them. Falls back to a plain sort when "
                                "the server is unreachable."},
                ),
                "llm_base_url": (
                    "STRING",
                    {"default": DEFAULT_BASE_URL,
                     "tooltip": "OpenAI-compatible endpoint. LM Studio "
                                "default: http://127.0.0.1:1234/v1"},
                ),
                "llm_model": (
                    "STRING",
                    {"default": "",
                     "tooltip": "Model id to use. Leave empty to use the "
                                "first model loaded in the server."},
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

    def sort(self, direction, layer_spacing, node_spacing, group_mode, animate,
             llm_clustering, llm_base_url, llm_model,
             trigger=None, extra_pnginfo=None, unique_id=None):
        workflow = (extra_pnginfo or {}).get("workflow")
        if not workflow or PromptServer is None:
            return {}
        result = run_layout(
            workflow,
            {
                "direction": direction,
                "h_spacing": layer_spacing,
                "v_spacing": node_spacing,
                "group_mode": group_mode,
            },
            {
                "enabled": llm_clustering,
                "base_url": llm_base_url,
                "model": llm_model,
            },
        )
        # Target the client that queued this prompt; fall back to broadcast.
        sid = getattr(PromptServer.instance, "client_id", None)
        PromptServer.instance.send_sync(WS_EVENT, {
            "positions": result["positions"],
            "groups": result["groups"],
            "new_groups": result.get("new_groups") or [],
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
