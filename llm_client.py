"""Optional LLM cluster suggestions via LM Studio (or any OpenAI-compatible
server such as Ollama). Used to propose semantic, per-function clusters for
nodes that are not inside any group; the layout engine then treats those
clusters like groups and the frontend creates named frames for them.

Standard library only — no extra dependencies. The sort always works
without an LLM; every failure here degrades to a plain sort.
"""

import json
import logging
import re
import urllib.error
import urllib.request

LOGGER = logging.getLogger("ComfyUI-Layout-Sort")

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
MAX_NODES = 300
MAX_LINKS = 800
MAX_CLUSTERS = 12
MAX_NAME_LENGTH = 60

SYSTEM_PROMPT = (
    "You are an expert at reading ComfyUI node workflows.\n"
    "Group the nodes into functional clusters such as: model loading, "
    "prompting/conditioning, latent preparation, sampling, decoding, "
    "upscaling, post-processing, saving/output, video, masking, controlnet.\n"
    "Rules:\n"
    "- Use only the node ids listed; never invent ids.\n"
    "- Each node belongs to at most one cluster; leave a node out if unsure.\n"
    "- Only include clusters with 2 or more nodes. At most 12 clusters.\n"
    "- Give each cluster a short descriptive name (2-4 words).\n"
    "Answer with JSON only, exactly in this shape:\n"
    '{"clusters": [{"name": "Model loading", "node_ids": [1, 2]}]}'
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "workflow_clusters",
        "schema": {
            "type": "object",
            "properties": {
                "clusters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "node_ids": {"type": "array", "items": {"type": "integer"}},
                        },
                        "required": ["name", "node_ids"],
                    },
                }
            },
            "required": ["clusters"],
        },
    },
}


def build_digest(workflow):
    """Compact plain-text description of the graph for the model."""
    lines = ["NODES (id | type | title):"]
    nodes = workflow.get("nodes") or []
    for raw in nodes[:MAX_NODES]:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        node_type = str(raw.get("type") or "?")
        title = str(raw.get("title") or "")
        suffix = f" | {title}" if title and title != node_type else ""
        lines.append(f"{raw['id']} | {node_type}{suffix}")
    if len(nodes) > MAX_NODES:
        lines.append(f"... {len(nodes) - MAX_NODES} more nodes omitted")

    lines.append("LINKS (origin_id -> target_id):")
    links = workflow.get("links") or []
    count = 0
    for raw in links:
        if count >= MAX_LINKS:
            lines.append(f"... {len(links) - MAX_LINKS} more links omitted")
            break
        if isinstance(raw, (list, tuple)) and len(raw) >= 5:
            origin, target = raw[1], raw[3]
        elif isinstance(raw, dict):
            origin, target = raw.get("origin_id"), raw.get("target_id")
        else:
            continue
        lines.append(f"{origin} -> {target}")
        count += 1
    return "\n".join(lines)


def _request_json(url, payload, timeout):
    # LM Studio runs on localhost: bypass any system proxy configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _pick_model(base_url, timeout):
    data = _request_json(base_url + "/models", None, timeout)
    models = data.get("data") or []
    if not models:
        raise RuntimeError("no model is loaded in LM Studio")
    return str(models[0].get("id"))


def _extract_json(text):
    """Pull the first JSON object/array out of a chatty model reply."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("reply contains no JSON")
    opener, closer = text[start], {"{": "}", "[": "]"}[text[start]]
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("reply contains unbalanced JSON")


def _validate(parsed, workflow):
    valid_ids = {}
    for raw in workflow.get("nodes") or []:
        if isinstance(raw, dict) and "id" in raw:
            valid_ids[str(raw["id"])] = raw["id"]

    items = parsed.get("clusters") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise ValueError("no clusters array in reply")

    clusters = []
    taken = set()
    for item in items[:MAX_CLUSTERS]:
        if not isinstance(item, dict):
            continue
        members = []
        for value in item.get("node_ids") or []:
            nid = valid_ids.get(str(value))
            if nid is not None and nid not in taken and nid not in members:
                members.append(nid)
        if len(members) < 2:
            continue
        # Claim ids only for clusters that survive, so a dropped singleton
        # cannot starve a later valid cluster of its members.
        taken.update(members)
        name = str(item.get("name") or "").strip()[:MAX_NAME_LENGTH]
        clusters.append({
            "name": name or f"Cluster {len(clusters) + 1}",
            "node_ids": members,
        })
    return clusters


def suggest_clusters(workflow, base_url=None, model="", timeout=60):
    """Ask the local LLM for semantic clusters.

    Returns (clusters, None) on success or ([], error_message) on any
    failure — callers fall back to a plain sort.
    """
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URL
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    try:
        model_id = (model or "").strip() or _pick_model(base, min(timeout, 15))
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_digest(workflow)},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": RESPONSE_SCHEMA,
        }
        try:
            data = _request_json(base + "/chat/completions", payload, timeout)
        except urllib.error.HTTPError:
            # Some models/servers reject structured output; retry plain.
            payload.pop("response_format", None)
            data = _request_json(base + "/chat/completions", payload, timeout)
        content = data["choices"][0]["message"]["content"]
        clusters = _validate(_extract_json(content), workflow)
        if not clusters:
            return [], "the model returned no usable clusters"
        LOGGER.info("LLM suggested %d clusters via %s", len(clusters), model_id)
        return clusters, None
    except Exception as exc:  # degrade to a plain sort, never break it
        message = f"{type(exc).__name__}: {exc}"
        LOGGER.warning("LLM clustering unavailable (%s)", message)
        return [], message
