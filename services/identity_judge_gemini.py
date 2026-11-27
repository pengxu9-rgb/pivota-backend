"""A Gemini model as the matcher or the sceptic of the live judge (services/identity_live_judge.py).

OFFLINE RUNS ONLY (scripts/ops/run_identity_judge_offline.py). Nothing on a request path imports
this module, and the judge's rules stay in charge: vetoes run before any agent, the verifier drops
an answer that cites a field its input does not have, and grounding stops an agent from creating
agreement the text does not show. A model can therefore hold a pair back or leave it at likely; it
can never confirm a size, shade or price the listings do not state.

Source terms: listings go to a model vendor only where that source's terms allow it. Today that is
Shopify listings only (the platform's written terms), so GeminiAgent refuses a pair unless BOTH
sides read as Shopify listings (shopify_listing). The refusal is an agent error, which the judge
turns into not_shown; the offline runner filters such pairs out before scoring.

Each call goes through services/vertex_gemini (the backend's one Gemini transport: Vertex AI with
ADC when VERTEX_AI_ENABLED=true, else the AI Studio endpoint with GEMINI_API_KEY) and:
  * puts the role and answer format in the system instruction, and the two listings inside the
    product-data fence (services/llm_fence), since store titles are third-party text;
  * asks for JSON in the answer's schema (responseMimeType + responseSchema) at a stated thinking
    level;
  * keeps the answer only when the response names the model asked for (modelVersion), has one
    candidate that finished with STOP, and was not blocked; anything else is no answer;
  * parses the non-thought text with parse_agent_answer (the whole reply must be one JSON object).
A 429, a 5xx or a timeout is retried twice with a pause; any other failure is an agent error.
Every call is recorded (pair id, role, model, answer, finish reason, token counts) so the run can
be replayed by ReplayAgent and scored again without new calls.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from services import identity_live_judge as lj
from services.llm_fence import PRODUCT_DATA_FENCE

DEFAULT_MATCHER_MODEL = "gemini-4-hydrogen"
DEFAULT_SCEPTIC_MODEL = "gemini-4-carbon"
THINKING_LEVELS = ("LOW", "MEDIUM", "HIGH")  # MINIMAL is refused by gemini-4-carbon
DEFAULT_THINKING = "MEDIUM"
MAX_OUTPUT_TOKENS = 8192  # thinking counts against it; the answer itself is small
TIMEOUT_S = 90.0
RETRY_PAUSES_S = (2.0, 8.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

Post = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _enum(values: List[str]) -> Dict[str, Any]:
    return {"type": "STRING", "enum": values}


ANSWER_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "same": {"type": "BOOLEAN"},
        "fields": {
            "type": "OBJECT",
            "properties": {
                f: _enum(list(lj.FIELD_VALUES) if f in lj.NOT_APPLICABLE_OK
                         else [v for v in lj.FIELD_VALUES if v != "not_applicable"])
                for f in lj.FIELDS
            },
            "required": list(lj.FIELDS),
        },
        "cites": {"type": "ARRAY", "items": _enum([f"{s}.{c}" for s in ("a", "b") for c in lj.CITABLE])},
    },
    "required": ["same", "fields", "cites"],
}


def shopify_listing(side: Mapping[str, Any]) -> bool:
    """True when the listing's own links show a Shopify store: its image on Shopify's CDN (the
    cdn.shopify.com host, or a store domain's /cdn/shop/ path) or its page on a .myshopify.com
    host. Read from the listing only; nothing is fetched."""
    if not isinstance(side, Mapping):
        return False
    try:
        img = urlsplit(str(side.get("image_url") or ""))
        page = urlsplit(str(side.get("url") or ""))
        img_host, page_host = img.hostname, page.hostname
    except ValueError:
        return False
    if img.scheme in ("http", "https") and (img_host == "cdn.shopify.com" or img.path.startswith("/cdn/shop/")):
        return True
    return page.scheme in ("http", "https") and (page_host or "").endswith(".myshopify.com")


def shopify_pair(view: Mapping[str, Any]) -> bool:
    return shopify_listing(view.get("a") or {}) and shopify_listing(view.get("b") or {})


class SourceNotAllowed(lj.AnswerError):
    """The pair holds a listing whose source terms do not allow sending it to a model vendor."""


class TransportError(RuntimeError):
    """The call failed with a status worth retrying (429, 5xx) or timed out."""


def build_request(role: str, view: Mapping[str, Any], thinking: str) -> Dict[str, Any]:
    """The generateContent body for one role on one pair."""
    return {
        "systemInstruction": {"parts": [{"text": lj.role_instructions(role) + PRODUCT_DATA_FENCE.notice}]},
        "contents": [{"role": "user", "parts": [{"text": PRODUCT_DATA_FENCE.fence_payload(lj.agent_sides(view))}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingLevel": thinking},
            "responseMimeType": "application/json",
            "responseSchema": ANSWER_SCHEMA,
        },
    }


def vertex_post(model: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """POST through the backend's Gemini transport (imported here: only a model run needs it)."""
    import httpx

    from services import vertex_gemini

    try:
        resp = httpx.post(vertex_gemini.generate_content_url(model), json=request,
                          headers=vertex_gemini.auth_headers_sync(), timeout=TIMEOUT_S)
    except httpx.TimeoutException as exc:
        raise TransportError("timeout") from exc
    if resp.status_code in RETRY_STATUS:
        raise TransportError(f"http {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def _usage(payload: Mapping[str, Any]) -> Dict[str, int]:
    u = payload.get("usageMetadata") if isinstance(payload, Mapping) else None
    out = {}
    for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount"):
        v = (u or {}).get(k) if isinstance(u, Mapping) else None
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    return out


def read_response(payload: Any, model: str) -> tuple:
    """(answer text, finish reason, problem). problem is None only for a complete answer from
    the model asked for."""
    if not isinstance(payload, Mapping):
        return None, None, "response is not an object"
    block = (payload.get("promptFeedback") or {}).get("blockReason") if isinstance(
        payload.get("promptFeedback"), Mapping) else None
    if block:
        return None, None, f"blocked {block}"
    if payload.get("modelVersion") != model:
        return None, None, "served by another model"
    cands = payload.get("candidates")
    if not isinstance(cands, list) or len(cands) != 1 or not isinstance(cands[0], Mapping):
        return None, None, f"{len(cands) if isinstance(cands, list) else 0} candidates"
    finish = cands[0].get("finishReason")
    if finish != "STOP":
        return None, finish, f"finish {finish}"
    parts = (cands[0].get("content") or {}).get("parts") if isinstance(cands[0].get("content"), Mapping) else None
    if not isinstance(parts, list):
        return None, finish, "no parts"
    texts = [p.get("text") for p in parts if isinstance(p, Mapping) and not p.get("thought")]
    if not texts or not all(isinstance(t, str) for t in texts):
        return None, finish, "no answer text"
    return "".join(texts), finish, None


class GeminiAgent:
    """One role on one model. Call it with a pair view; it returns the parsed answer, None when the
    reply is not one JSON object, or raises (the judge then does not show the pair)."""

    def __init__(self, role: str, model: str, *, thinking: str = DEFAULT_THINKING,
                 post: Post = vertex_post, sleep: Callable[[float], None] = time.sleep):
        if role not in lj.ROLES:
            raise ValueError(f"unknown role {role!r}")
        if not isinstance(model, str) or not model.startswith("gemini-"):
            raise ValueError(f"not a Gemini model id: {model!r}")
        if thinking not in THINKING_LEVELS:
            raise ValueError(f"unknown thinking level {thinking!r}")
        self.role, self.model, self.thinking = role, model, thinking
        self._post, self._sleep = post, sleep
        self._lock = threading.Lock()
        self.records: List[Dict[str, Any]] = []

    def _record(self, view: Mapping[str, Any], answer: Any, finish: Any, usage: Dict[str, int],
                error: Optional[str]) -> None:
        row = {"pair_id": view.get("pair_id"), "role": self.role, "model": self.model,
               "thinking": self.thinking, "answer": answer, "finish_reason": finish, "usage": usage}
        if error:
            row["error"] = error
        with self._lock:
            self.records.append(row)

    def _call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        for pause in (*RETRY_PAUSES_S, None):
            try:
                return self._post(self.model, request)
            except TransportError:
                if pause is None:
                    raise
                self._sleep(pause)
        raise AssertionError("unreachable")

    def __call__(self, view: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        if not shopify_pair(view):
            raise SourceNotAllowed("a listing is not from a source whose terms allow a model vendor")
        payload = self._call(build_request(self.role, view, self.thinking))
        text, finish, problem = read_response(payload, self.model)
        usage = _usage(payload) if isinstance(payload, Mapping) else {}
        if problem:
            self._record(view, None, finish, usage, problem)
            raise lj.AnswerError(problem)
        answer = lj.parse_agent_answer(text)
        self._record(view, answer, finish, usage, None if answer is not None else "not one JSON object")
        return answer


def usage_totals(records: List[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Calls and token counts per model, for the run's report."""
    out: Dict[str, Dict[str, int]] = {}
    for r in records:
        row = out.setdefault(str(r.get("model")), {"calls": 0})
        row["calls"] += 1
        for k, v in (r.get("usage") or {}).items():
            row[k] = row.get(k, 0) + int(v)
    return dict(sorted(out.items()))
