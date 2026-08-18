"""ComfyUI node + server glue for Layout Sort.

Two ways to trigger a sort:
  * Run the workflow: the LayoutSort node reads the serialized workflow
    from the hidden EXTRA_PNGINFO input, computes the layout on the
    backend, and pushes new positions to the browser over the websocket.
  * Press the "Sort now" button on the node (added by web/layoutSort.js):
    the frontend POSTs the current graph to /layout_sort/compute and
    applies the returned positions immediately, no queue needed.
"""

from .layout_core import compute_layout

WS_EVENT = "layout_sort_apply"

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
        try:
            result = compute_layout(workflow, options)
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
                "animate": ("BOOLEAN", {"default": True}),
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

    def sort(self, direction, layer_spacing, node_spacing, animate,
             trigger=None, extra_pnginfo=None, unique_id=None):
        workflow = (extra_pnginfo or {}).get("workflow")
        if not workflow or PromptServer is None:
            return {}
        result = compute_layout(workflow, {
            "direction": direction,
            "h_spacing": layer_spacing,
            "v_spacing": node_spacing,
        })
        PromptServer.instance.send_sync(WS_EVENT, {
            "positions": result["positions"],
            "groups": result["groups"],
            "animate": bool(animate),
            "source_node": unique_id,
        })
        return {}


NODE_CLASS_MAPPINGS = {
    "LayoutSort": LayoutSort,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LayoutSort": "Layout Sort (Auto Arrange Workflow)",
}
