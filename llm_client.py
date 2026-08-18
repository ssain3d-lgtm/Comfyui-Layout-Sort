"""Optional LLM cluster suggestions via LM Studio (or any OpenAI-compatible
server such as Ollama). Used to propose semantic, per-function clusters for
nodes that are not inside any group; the layout engine then treats those
clusters like groups and the frontend creates named frames for them.

Standard library only — no extra dependencies. The sort always works
without an LLM; every failure here degrades to a plain sort.
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
                            # ids can be UUID strings in subgraph-era graphs
                            "node_ids": {"type": "array",
                                         "items": {"type": ["integer", "string"]}},
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
    # Subgraph instance nodes carry an opaque UUID as their type; resolve
    # it to the subgraph's name so the model gets a semantic signal.
    subgraph_names = {}
    definitions = workflow.get("definitions") or {}
    for sub in definitions.get("subgraphs") or []:
        if isinstance(sub, dict) and sub.get("id") is not None:
            subgraph_names[str(sub["id"])] = str(sub.get("name") or "Subgraph")

    lines = ["NODES (id | type | title):"]
    nodes = workflow.get("nodes") or []
    for raw in nodes[:MAX_NODES]:
        if not isinstance(raw, dict) or "id" not in raw:
            continue
        node_type = str(raw.get("type") or "?")
        if node_type in subgraph_names:
            node_type = f"[subgraph] {subgraph_names[node_type]}"
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


def _parse_anthropic_reply(data):
    """Extract clusters from an Anthropic Messages API response."""
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
    """Extract clusters from a chat completion, with actionable errors."""
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


def suggest_clusters(workflow, base_url=None, model="", timeout=60,
                     api_key="", key_origin="*", max_tokens=None,
                     provider=None):
    """Ask the local LLM for semantic clusters.

    `api_key` is sent as a Bearer token, but only when `key_allowed_for`
    permits it for this base_url (`key_origin`: "*" = caller explicitly
    paired key and URL; an origin string = the origin the stored key is
    bound to; None = loopback only). With no key given, the
    LAYOUT_SORT_LLM_API_KEY environment variable applies under the same
    gate (its origin comes from LAYOUT_SORT_LLM_ALLOWED_ORIGIN). This
    stops a shared workflow from pointing llm_base_url at a hostile host
    and exfiltrating the server-stored key.

    Returns (clusters, None) on success or ([], error_message) on any
    failure — callers fall back to a plain sort.
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
        return [], "the API key contains invalid characters"
    model = (model or "").strip()
    if model.lower() == "auto":
        model = ""
    budget = int(_clamped_tokens(max_tokens))
    provider = provider or _provider_for(base)
    try:
        model_id = model or _pick_model(base, min(timeout, 15), api_key, provider)
        digest = build_digest(workflow)
        if provider == "anthropic":
            payload = {
                "model": model_id,
                "max_tokens": budget,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": digest}],
            }
            url = (format_origin(base) or base) + "/v1/messages"
            data = _request_json(url, payload, timeout, api_key, provider)
            clusters = _validate(_parse_anthropic_reply(data), workflow)
        else:
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": digest},
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
            clusters = _validate(_parse_reply(data), workflow)
        if not clusters:
            return [], "the model returned no usable clusters"
        LOGGER.info("LLM suggested %d clusters via %s", len(clusters), model_id)
        return clusters, None
    except Exception as exc:  # degrade to a plain sort, never break it
        message = f"{type(exc).__name__}: {exc} (endpoint: {base})"
        if withheld and isinstance(exc, urllib.error.HTTPError) \
                and exc.code in (401, 403):
            message += (" — the stored API key was withheld because this "
                        "llm_base_url is outside its allowed origin; re-save "
                        "the key while the node points at this server")
        LOGGER.warning("LLM clustering unavailable (%s)", message)
        return [], message
