"""Optional prompt-driven sorting via LM Studio, Ollama, OpenAI or the
Anthropic API. The user types a natural-language request on the node
("vertical, keep my groups, tighter spacing, group the VAE nodes...");
the model reads a digest of the real workflow and translates the request
into a strict JSON plan made of the engine's own controls — direction,
spacings, group mode, style, and named clusters for ungrouped nodes.

The model never places nodes: geometry stays 100% deterministic in
layout_core. Standard library only — no extra dependencies. The sort
always works without an LLM; every failure here degrades to a plain sort.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

LOGGER = logging.getLogger("ComfyUI-Layout-Sort")

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
# The llm_provider dropdown resolves to these; "custom" uses llm_base_url.
PROVIDER_PRESETS = {
    "lmstudio": DEFAULT_BASE_URL,
    "ollama": "http://127.0.0.1:11434/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
# Safe way to supply a token: it never gets serialized into workflows.
API_KEY_ENV_VAR = "LAYOUT_SORT_LLM_API_KEY"
# Origin the env-var key may be sent to (loopback is always allowed).
ALLOWED_ORIGIN_ENV_VAR = "LAYOUT_SORT_LLM_ALLOWED_ORIGIN"
MAX_NODES = 300
MAX_LINKS = 800
MAX_CLUSTERS = 12
MAX_NAME_LENGTH = 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Thinking models (qwen3 etc.) burn output tokens inside <think> before
# emitting the JSON answer; a small budget gets truncated mid-think and
# looks like "reply contains no JSON". Users can raise this per-node via
# the llm_max_tokens widget (up to MAX_COMPLETION_TOKENS_CAP).
DEFAULT_COMPLETION_TOKENS = 4096
MAX_COMPLETION_TOKENS_CAP = 262144
ANTHROPIC_VERSION = "2023-06-01"

# The whitelist of controls a plan may set; everything else is dropped.
PLAN_ENUMS = {
    "direction": ("left_to_right", "top_to_bottom"),
    "group_mode": ("cluster", "inner", "refit"),
    "style": ("flow", "grid"),
    "shape": ("auto", "square", "wide", "tall"),
}
PLAN_SPACING_KEYS = ("h_spacing", "v_spacing")
SPACING_MIN, SPACING_MAX = 10, 600
MAX_NOTE_LENGTH = 200
MAX_UNSUPPORTED = 8
# Per-field cap inside the digest: one multi-megabyte node title in a
# shared workflow must not balloon the LLM request.
DIGEST_MAX_FIELD = 80

SYSTEM_PROMPT = (
    "You translate a user's natural-language layout request for a ComfyUI "
    "node workflow into a strict JSON plan for a deterministic sorting "
    "engine. You never place nodes yourself and never invent controls.\n"
    "Controls you may set inside \"options\" (omit any you don't need):\n"
    '- "direction": "left_to_right" or "top_to_bottom" (main flow axis)\n'
    '- "h_spacing": integer 10-600, gap between layers in px\n'
    '- "v_spacing": integer 10-600, gap between nodes in a layer in px\n'
    '- "group_mode": "cluster" (lay out each group frame as a block, then '
    'arrange the blocks), "inner" (keep every group frame where the user '
    'put it and only tidy the nodes inside), "refit" (ignore frames while '
    "sorting, re-wrap them afterwards; only for lightly grouped graphs)\n"
    '- "style": "flow" (centered columns) or "grid" (top-aligned columns)\n'
    '- "shape": "square" (1:1 canvas), "wide" (2:1), "tall" (1:2) or '
    '"auto" — long pipelines fold into bands, tall graphs spread into '
    "extra columns, group interiors follow the same ratio\n"
    "Additionally, \"clusters\" may group UNGROUPED nodes into named "
    'frames: [{"name": "Model loading", "node_ids": [1, 2]}]. Use only '
    "ids from the UNGROUPED list; each node in at most one cluster; only "
    "clusters with 2+ nodes; short names (2-4 words); at most 12.\n"
    "Rules:\n"
    "- Map intent to controls: \"vertical / top to bottom\" -> direction; "
    "\"keep my groups where they are / only tidy insides\" -> group_mode "
    "\"inner\"; \"tighter/compact\" -> lower spacings, \"wider/airier\" -> "
    "higher; \"group X-related nodes\" -> clusters.\n"
    "- CURRENT SETTINGS in the digest are the baseline; set only what the "
    "user asked to change.\n"
    "- Anything the engine cannot do (exact pixel positions, resizing "
    "nodes, recoloring, rewiring) goes into \"unsupported\" as a short "
    "phrase — never fake it with other controls.\n"
    '- "note": one short sentence, in the user\'s language, saying what '
    "you set.\n"
    "Answer with JSON only, exactly in this shape:\n"
    '{"options": {"direction": "top_to_bottom"}, "clusters": [], '
    '"note": "...", "unsupported": []}'
)

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "layout_plan",
        "schema": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {
                        "direction": {
                            "type": "string",
                            "enum": list(PLAN_ENUMS["direction"]),
                        },
                        "h_spacing": {"type": "integer"},
                        "v_spacing": {"type": "integer"},
                        "group_mode": {
                            "type": "string",
                            "enum": list(PLAN_ENUMS["group_mode"]),
                        },
                        "style": {
                            "type": "string",
                            "enum": list(PLAN_ENUMS["style"]),
                        },
                        "shape": {
                            "type": "string",
                            "enum": list(PLAN_ENUMS["shape"]),
                        },
                    },
                },
                "clusters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            # ids can be UUID strings in subgraph-era graphs
                            "node_ids": {"type": "array",
                                         "items": {"type": ["integer", "string"]}},
                        },
                        "required": ["name", "node_ids"],
                    },
                },
                "note": {"type": "string"},
                "unsupported": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["note"],
        },
    },
}


def _group_rects(workflow):
    """[(title, (x, y, w, h))] for every parseable group frame."""
    rects = []
    for raw in workflow.get("groups") or []:
        if not isinstance(raw, dict):
            continue
        bounding = raw.get("bounding")
        if isinstance(bounding, dict):  # some exports serialize as {0:...}
            bounding = [bounding.get(k) for k in ("0", "1", "2", "3")]
        if not isinstance(bounding, (list, tuple)) or len(bounding) < 4:
            continue
        try:
            rect = tuple(float(v) for v in bounding[:4])
        except (TypeError, ValueError):
            continue
        rects.append((str(raw.get("title") or "Group"), rect))
    return rects


def _node_center(raw):
    pos, size = raw.get("pos"), raw.get("size")
    if isinstance(pos, dict):
        pos = [pos.get("0"), pos.get("1")]
    if isinstance(size, dict):
        size = [size.get("0"), size.get("1")]
    try:
        x, y = float(pos[0]), float(pos[1])
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (x + w / 2.0, y + h / 2.0)


def build_digest(workflow, current_options=None):
    """Compact plain-text description of the graph for the model: nodes,
    links, existing group frames, which nodes are still ungrouped, and the
    node's current widget settings as the baseline."""
    # Subgraph instance nodes carry an opaque UUID as their type; resolve
    # it to the subgraph's name so the model gets a semantic signal.
    subgraph_names = {}
    definitions = workflow.get("definitions") or {}
    for sub in definitions.get("subgraphs") or []:
        if isinstance(sub, dict) and sub.get("id") is not None:
            subgraph_names[str(sub["id"])] = \
                str(sub.get("name") or "Subgraph")[:DIGEST_MAX_FIELD]

    group_rects = _group_rects(workflow)

    def grouped(raw):
        center = _node_center(raw)
        if center is None:
            return False
        return any(
            r[0] <= center[0] <= r[0] + r[2] and r[1] <= center[1] <= r[1] + r[3]
            for _t, r in group_rects
        )

    lines = ["NODES (id | type | title):"]
    nodes = workflow.get("nodes") or []
    ungrouped = []
    for raw in nodes[:MAX_NODES]:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        node_type = str(raw.get("type") or "?")
        if node_type in subgraph_names:
            node_type = f"[subgraph] {subgraph_names[node_type]}"
        node_type = node_type[:DIGEST_MAX_FIELD]
        title = str(raw.get("title") or "")[:DIGEST_MAX_FIELD]
        suffix = f" | {title}" if title and title != node_type else ""
        lines.append(f"{raw['id']} | {node_type}{suffix}")
        if not grouped(raw):
            ungrouped.append(str(raw["id"]))
    if len(nodes) > MAX_NODES:
        lines.append(f"... {len(nodes) - MAX_NODES} more nodes omitted")

    if group_rects:
        lines.append("EXISTING GROUP FRAMES (title):")
        lines.extend(f"- {title[:DIGEST_MAX_FIELD]}"
                     for title, _r in group_rects)
    lines.append("UNGROUPED NODE IDS (the only ids usable in clusters): "
                 + (", ".join(ungrouped) if ungrouped else "(none)"))

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

    if current_options:
        shown = []
        for key in ("direction", "h_spacing", "v_spacing", "group_mode",
                    "style", "shape"):
            value = current_options.get(key)
            if value is not None:
                shown.append(f"{key}={value}")
        if shown:
            lines.append("CURRENT SETTINGS: " + ", ".join(shown))
    return "\n".join(lines)


def _clamped_tokens(value):
    """Completion-token budget from the widget, bounded to a sane range."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COMPLETION_TOKENS
    return max(256, min(MAX_COMPLETION_TOKENS_CAP, number))


def is_valid_api_key(key):
    """Bearer tokens must be printable ASCII with no spaces or control
    characters — anything else corrupts the HTTP header, and the resulting
    exception text would echo the key into logs and error toasts."""
    return all(33 <= ord(ch) <= 126 for ch in key)


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return (scheme, (parts.hostname or "").lower(), port)


def _ensure_scheme(url):
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    return url


def format_origin(url):
    """Normalize a URL to its origin string, e.g. "http://127.0.0.1:1234"."""
    url = _ensure_scheme(url)
    if not url:
        return None
    scheme, host, port = _origin(url)
    if not host:
        return None
    return f"{scheme}://{host}:{port}"


def _is_loopback_host(host):
    return host in ("localhost", "::1") or host.startswith("127.")


def key_allowed_for(base_url, key_origin):
    """May a stored/ambient key be attached to a request to base_url?

    key_origin semantics: "*" = always (the caller explicitly paired key
    and URL); None = loopback targets only; an origin/URL string = that
    origin only. Loopback targets are always allowed — the risk being
    gated is a shared workflow pointing llm_base_url at a hostile remote
    host to exfiltrate the server-stored key.
    """
    if key_origin == "*":
        return True
    scheme, host, port = _origin(_ensure_scheme(base_url))
    if _is_loopback_host(host):
        return True
    if not key_origin:
        return False
    return _origin(_ensure_scheme(key_origin)) == (scheme, host, port)


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib forwards auth headers across redirects by default, so a
    hostile or compromised endpoint could 302 the request elsewhere and
    capture the token. Drop them whenever a redirect leaves the original
    origin (scheme, host, port)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_request = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if new_request is not None and _origin(newurl) != _origin(req.full_url):
            new_request.remove_header("Authorization")
            new_request.remove_header("X-api-key")
        return new_request


def _request_json(url, payload, timeout, api_key="", provider="openai"):
    # LM Studio runs on localhost: bypass any system proxy configuration.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _AuthStrippingRedirectHandler()
    )
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        if provider == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    if provider == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("LLM response exceeds the size limit")
        return json.loads(raw.decode("utf-8", "replace"))


def resolve_base_url(provider, base_url):
    """Turn the llm_provider dropdown value into an endpoint; "custom"
    (or anything unknown) falls back to the llm_base_url text.

    Also heals workflows saved by older node versions, where positional
    widget values shift one slot: a URL that landed in the provider slot
    is used as the endpoint, and junk like "auto" in the base_url slot
    falls back to the LM Studio default instead of becoming a fake
    hostname (http://auto -> getaddrinfo failure)."""
    provider = (provider or "").strip()
    preset = PROVIDER_PRESETS.get(provider.lower())
    if preset:
        return preset
    if re.match(r"^https?://", provider, re.IGNORECASE):
        return provider
    base = (base_url or "").strip()
    if not base or base.lower() == "auto":
        return DEFAULT_BASE_URL
    return base


def _provider_for(base_url):
    """"anthropic" for the Anthropic API, "openai" for everything that
    speaks the OpenAI-compatible format (LM Studio, Ollama, OpenAI...)."""
    host = _origin(_ensure_scheme(base_url))[1]
    return "anthropic" if host.endswith("anthropic.com") else "openai"


def _models_url(base, provider):
    if provider == "anthropic":
        return (format_origin(base) or base) + "/v1/models"
    return base + "/models"


def _pick_model(base_url, timeout, api_key="", provider="openai"):
    if provider == "anthropic":
        # Anthropic has no "first loaded model" notion; use the current
        # default recommendation instead of guessing list order.
        return "claude-opus-5"
    data = _request_json(_models_url(base_url, provider), None, timeout,
                         api_key, provider)
    models = data.get("data") or []
    if not models:
        raise RuntimeError("no model is loaded in LM Studio")
    return str(models[0].get("id"))


def list_models(base_url=None, timeout=15, api_key="", key_origin="*"):
    """List model ids from the server (for the node's Connect button).

    Returns (model_ids, None) or ([], error_message). The stored/ambient
    key is attached only when `key_allowed_for` permits it. Works for
    OpenAI-compatible servers and the Anthropic API alike (both expose a
    models listing as {"data": [{"id": ...}]}).
    """
    base = _ensure_scheme((base_url or DEFAULT_BASE_URL).strip().rstrip("/")
                          or DEFAULT_BASE_URL)
    provider = _provider_for(base)
    api_key = (api_key or "").strip()
    if api_key:
        if not key_allowed_for(base, key_origin) or not is_valid_api_key(api_key):
            api_key = ""
    else:
        env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
        env_origin = os.environ.get(ALLOWED_ORIGIN_ENV_VAR, "").strip() or None
        if env_key and key_allowed_for(base, env_origin) and is_valid_api_key(env_key):
            api_key = env_key
    try:
        data = _request_json(_models_url(base, provider), None, timeout,
                             api_key, provider)
        models = [str(m.get("id")) for m in data.get("data") or []
                  if isinstance(m, dict) and m.get("id")]
        if not models:
            return [], "the server reports no loaded models"
        return models, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc} (endpoint: {base})"


def _extract_json(text):
    """Pull the first JSON object/array out of a chatty model reply."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models
    # A reply truncated mid-think has an unterminated <think> with nothing
    # useful after it — drop it instead of feeding it to the parser.
    text = re.sub(r"<think>.*\Z", "", text, flags=re.S)
    # Try every fenced block first, then the full text — the answer is not
    # necessarily in the first fence.
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)]
    candidates.append(text)
    last_error = None
    for candidate in candidates:
        try:
            return _parse_first_json(candidate.strip())
        except ValueError as error:
            last_error = error
    raise last_error or ValueError("reply contains no JSON")


def _parse_first_json(text):
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


def _validate_clusters(items, workflow):
    if not isinstance(items, list):
        return []
    valid_ids = {}
    for raw in workflow.get("nodes") or []:
        if isinstance(raw, dict) and "id" in raw:
            valid_ids[str(raw["id"])] = raw["id"]

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


def _validate_plan(parsed, workflow):
    """Reduce a model reply to a safe plan the engine can execute.

    Options go through a strict whitelist (unknown keys and out-of-enum
    values are dropped, spacings clamped) so a confused model can never
    inject engine options like detach_types or snap distortions.
    Returns {"options", "clusters", "note", "unsupported"} — always all
    four keys, empty when absent.
    """
    if not isinstance(parsed, dict):
        raise ValueError("reply is not a JSON plan object")
    raw_options = parsed.get("options")
    options = {}
    if isinstance(raw_options, dict):
        for key, allowed in PLAN_ENUMS.items():
            value = str(raw_options.get(key) or "").strip().lower()
            if value in allowed:
                options[key] = value
        for key in PLAN_SPACING_KEYS:
            value = raw_options.get(key)
            try:
                # json.loads accepts the Infinity literal -> OverflowError
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            options[key] = max(SPACING_MIN, min(SPACING_MAX, number))
    clusters = _validate_clusters(parsed.get("clusters"), workflow)
    note = str(parsed.get("note") or "").strip()[:MAX_NOTE_LENGTH]
    raw_unsupported = parsed.get("unsupported")
    if not isinstance(raw_unsupported, list):
        raw_unsupported = []
    unsupported = [
        str(item).strip()[:120]
        for item in raw_unsupported[:MAX_UNSUPPORTED]
        if str(item).strip()
    ]
    if not options and not clusters and not note and not unsupported:
        raise ValueError("the model returned no actionable plan")
    return {"options": options, "clusters": clusters,
            "note": note, "unsupported": unsupported}


def _parse_anthropic_reply(data):
    """Extract the reply JSON from an Anthropic Messages API response."""
    stop_reason = data.get("stop_reason")
    if stop_reason == "refusal":
        raise ValueError("the model declined this request")
    text = "\n".join(
        block.get("text") or ""
        for block in data.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )
    try:
        return _extract_json(text)
    except ValueError:
        LOGGER.info("unparseable LLM reply (stop_reason=%s): %.300r",
                    stop_reason, text)
        if stop_reason == "max_tokens":
            raise ValueError(
                "the model hit its token limit before finishing the answer "
                "— raise llm_max_tokens"
            )
        raise


def _parse_reply(data):
    """Extract the reply JSON from a chat completion, with actionable
    errors."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    # Some servers put a thinking model's chain-of-thought in a separate
    # field; if content is empty the answer sometimes hides in there.
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    finish = choice.get("finish_reason")
    for text in (content, reasoning):
        if not text:
            continue
        try:
            return _extract_json(text)
        except ValueError:
            continue
    LOGGER.info("unparseable LLM reply (finish_reason=%s): %.300r",
                finish, content or reasoning)
    if finish == "length":
        raise ValueError(
            "the model hit its token limit before finishing the answer — "
            "thinking models can spend the whole budget inside <think>; "
            "use a non-thinking model or a bigger context/token limit"
        )
    raise ValueError("reply contains no JSON")


def plan_layout(workflow, prompt, current_options=None, base_url=None,
                model="", timeout=60, api_key="", key_origin="*",
                max_tokens=None, provider=None):
    """Translate the user's natural-language request into a layout plan.

    Returns (plan, None) on success — plan is the validated
    {"options", "clusters", "note", "unsupported"} dict from
    `_validate_plan` — or (None, error_message) on any failure; callers
    fall back to a plain sort with the widget settings.

    `api_key` is sent as a Bearer token, but only when `key_allowed_for`
    permits it for this base_url (`key_origin`: "*" = caller explicitly
    paired key and URL; an origin string = the origin the stored key is
    bound to; None = loopback only). With no key given, the
    LAYOUT_SORT_LLM_API_KEY environment variable applies under the same
    gate (its origin comes from LAYOUT_SORT_LLM_ALLOWED_ORIGIN). This
    stops a shared workflow from pointing llm_base_url at a hostile host
    and exfiltrating the server-stored key.
    """
    base = _ensure_scheme((base_url or DEFAULT_BASE_URL).strip().rstrip("/")
                          or DEFAULT_BASE_URL)
    withheld = False
    api_key = (api_key or "").strip()
    if api_key:
        if not key_allowed_for(base, key_origin):
            api_key = ""
            withheld = True
    else:
        env_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
        if env_key:
            env_origin = os.environ.get(ALLOWED_ORIGIN_ENV_VAR, "").strip() or None
            if key_allowed_for(base, env_origin):
                api_key = env_key
            else:
                withheld = True
    if withheld:
        LOGGER.info("API key withheld: %s is outside the key's allowed origin", base)
    if api_key and not is_valid_api_key(api_key):
        # Refuse before any header is built, so the key value can never
        # surface in an exception message, log line, or toast.
        return None, "the API key contains invalid characters"
    model = (model or "").strip()
    if model.lower() == "auto":
        model = ""
    budget = int(_clamped_tokens(max_tokens))
    provider = provider or _provider_for(base)
    request_text = (build_digest(workflow, current_options)
                    + "\n\nUSER REQUEST:\n" + str(prompt or "").strip())
    try:
        model_id = model or _pick_model(base, min(timeout, 15), api_key, provider)
        if provider == "anthropic":
            payload = {
                "model": model_id,
                "max_tokens": budget,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": request_text}],
            }
            url = (format_origin(base) or base) + "/v1/messages"
            data = _request_json(url, payload, timeout, api_key, provider)
            plan = _validate_plan(_parse_anthropic_reply(data), workflow)
        else:
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": request_text},
                ],
                "temperature": 0.2,
                "max_tokens": budget,
                "response_format": RESPONSE_SCHEMA,
            }
            try:
                data = _request_json(base + "/chat/completions", payload,
                                     timeout, api_key)
            except urllib.error.HTTPError as error:
                # Some models/servers reject structured output with a 400;
                # retry plain then. Other statuses (404/401/500...) would
                # fail identically on retry, so surface them immediately.
                code = error.code
                error.close()
                if code != 400:
                    raise
                payload.pop("response_format", None)
                data = _request_json(base + "/chat/completions", payload,
                                     timeout, api_key)
            plan = _validate_plan(_parse_reply(data), workflow)
        LOGGER.info(
            "LLM plan via %s: options=%s clusters=%d unsupported=%d",
            model_id, plan["options"], len(plan["clusters"]),
            len(plan["unsupported"]))
        return plan, None
    except Exception as exc:  # degrade to a plain sort, never break it
        message = f"{type(exc).__name__}: {exc} (endpoint: {base})"
        if withheld and isinstance(exc, urllib.error.HTTPError) \
                and exc.code in (401, 403):
            message += (" — the stored API key was withheld because this "
                        "llm_base_url is outside its allowed origin; re-save "
                        "the key while the node points at this server")
        LOGGER.warning("LLM plan unavailable (%s)", message)
        return None, message
