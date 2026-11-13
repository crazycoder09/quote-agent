"""
rate_limit.py
-------------
Groq rate-limit handling.

1. Per-minute budget (TPM): every LLM call goes through `TokenBudget.wait_for_room()`.
   If the call would push the last minute's usage over the model's limit, we wait
   until enough usage expires, then send it (the UI shows "Waiting Xs...").
2. Groq still says 429 for the minute window (estimate was off) -> wait the
   "try again in" time Groq gives (if short) and retry.
3. Daily limits (TPD/RPD) can't be waited out -> raise DailyLimitReached and
   remember the model is exhausted until its reset time, so callers can switch
   to a fallback model (see graph.agent_node).

Limits per model come from config/settings.json -> "rate_limits".
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Sequence, Tuple

from agent.agents import load_settings

log = logging.getLogger("quote_agent.llm")

WINDOW_SECONDS = 60.0
SAFETY = 0.92            # keep a margin below the hard per-minute limit
MAX_SHORT_WAIT = 75.0    # longest "try again in" we are willing to wait inline
MAX_RETRIES = 3

_RETRY_IN = re.compile(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", re.I)


class DailyLimitReached(Exception):
    def __init__(self, model: str, retry_after: float, message: str = ""):
        super().__init__(message or f"Daily limit reached for {model}")
        self.model = model
        self.retry_after = retry_after


class ModelBusy(DailyLimitReached):
    """Per-minute budget full right now: switch to another provider instead of waiting."""


class RequestTooLarge(Exception):
    """A single request is bigger than the model's per-minute window (Groq 413)."""

    def __init__(self, model: str, message: str = ""):
        super().__init__(message or f"Request too large for {model}")
        self.model = model


class AllModelsBusy(Exception):
    """Every configured model has hit its daily limit."""

    def __init__(self, retry_after: float):
        super().__init__("All AI models have reached their usage limit for now.")
        self.retry_after = retry_after


# Measured with the o200k tokenizer on real prompts: prose ~4 chars/token, JSON schemas ~3.7,
# spreadsheet rows (numbers, pipes, codes) only ~2.1-2.9. Use conservative ratios per kind.
PROSE_CHARS_PER_TOKEN = 3.6
DATA_CHARS_PER_TOKEN = 2.1


def estimate_tokens(messages: Sequence[Any], tools: Optional[Sequence[Any]] = None, max_output: int = 0) -> int:
    """Conservative token estimate for a request (tool results are data-heavy, so counted denser)."""
    tokens = 0.0
    for m in messages:
        text = str(getattr(m, "content", m))
        is_data = type(m).__name__ == "ToolMessage"
        tokens += len(text) / (DATA_CHARS_PER_TOKEN if is_data else PROSE_CHARS_PER_TOKEN) + 4
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            tokens += len(json.dumps([tc.get("args", {}) for tc in tool_calls], default=str)) / DATA_CHARS_PER_TOKEN
    if tools:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        tokens += sum(len(json.dumps(convert_to_openai_tool(t))) for t in tools) / PROSE_CHARS_PER_TOKEN
    return int(tokens * 1.08) + max_output


DAILY_RECHECK_SECONDS = 3600.0  # daily limit with no reset time given: look again after an hour


def _has_retry_time(message: str) -> bool:
    m = _RETRY_IN.search(message or "")
    return bool(m and any(m.groups()))


def _retry_after(message: str) -> float:
    m = _RETRY_IN.search(message or "")
    if not m or not any(m.groups()):
        return 60.0
    hours, minutes, seconds = (float(x) if x else 0.0 for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status in (413, 429) or "rate_limit" in str(exc).lower()


def _is_daily(message: str) -> bool:
    text = message.lower()
    return any(k in text for k in ("per day", "per-day", "per_day", "(tpd)", "(rpd)"))


def _is_provider_down(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return (isinstance(status, int) and status >= 500) or name in (
        "APIConnectionError", "APITimeoutError", "InternalServerError", "ServiceUnavailableError")


# Calibrated estimate for the per-minute "is this model busy?" check ONLY. It does not change what is
# sent to the model (graph._fit_to_model still uses the conservative estimate_tokens()).
# Measured on Groq gpt-oss: estimate_tokens() says ~5,100 for the agent prompt, the real count is ~3,730,
# and Groq counts only real usage - max_tokens is NOT reserved against the per-minute limit.
# Each model's estimate is therefore scaled by the real counts its own responses report.
DEFAULT_INPUT_RATIO = 0.75     # real prompt tokens / estimate_tokens(), until a model has answered once
DEFAULT_OUTPUT_TOKENS = 600    # typical reply incl. low-effort reasoning, until a model has answered once
ESTIMATE_MARGIN = 1.05


class TokenBudget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: Dict[str, Deque[Tuple[float, int]]] = {}
        self._exhausted_until: Dict[str, float] = {}
        self._input_ratio: Dict[str, float] = {}   # learned: real prompt tokens / estimate_tokens()
        self._output_avg: Dict[str, float] = {}    # learned: average real completion tokens

    # -- calibrated estimate (budget check only) -----------------------------

    def estimate(self, model: str, messages: Sequence[Any], tools: Optional[Sequence[Any]] = None,
                 max_output: int = 0) -> Tuple[int, int]:
        """(tokens to reserve for this call, raw input estimate). Input is scaled by what this model
        really used before; output is its typical reply length (never more than max_output)."""
        raw_input = estimate_tokens(messages, tools, 0)
        ratio = self._input_ratio.get(model, DEFAULT_INPUT_RATIO)
        output = min(max_output, self._output_avg.get(model, DEFAULT_OUTPUT_TOKENS)) if max_output else 0
        return int(raw_input * ratio * ESTIMATE_MARGIN + output), raw_input

    def learn(self, model: str, raw_input: int, usage: Dict[str, Any]) -> None:
        """Calibrate the estimate from the real token counts a response reported."""
        real_in, real_out = usage.get("input_tokens"), usage.get("output_tokens")
        with self._lock:
            if real_in and raw_input:
                ratio = min(1.5, max(0.3, real_in / raw_input))
                old = self._input_ratio.get(model)
                self._input_ratio[model] = ratio if old is None else 0.7 * old + 0.3 * ratio
            if real_out is not None:
                old = self._output_avg.get(model)
                self._output_avg[model] = float(real_out) if old is None else 0.7 * old + 0.3 * float(real_out)

    @staticmethod
    def limit_for(model: str) -> Optional[int]:
        limits = load_settings().get("rate_limits", {})
        value = limits.get(model) or limits.get(model.partition(":")[2] if ":" in model else model)
        return int(value) if value else None

    # -- daily exhaustion ----------------------------------------------------

    def mark_exhausted(self, model: str, seconds: float, minimum: float = 30) -> None:
        self._exhausted_until[model] = time.time() + max(seconds, minimum)

    def exhausted_for(self, model: str) -> float:
        """Seconds until the model's daily limit resets (0 = available)."""
        return max(0.0, self._exhausted_until.get(model, 0.0) - time.time())

    # -- per-minute budget ---------------------------------------------------

    def _used(self, model: str, now: float) -> int:
        q = self._usage.setdefault(model, deque())
        while q and now - q[0][0] >= WINDOW_SECONDS:
            q.popleft()
        return sum(tokens for _, tokens in q)

    def wait_for_room(self, model: str, needed: int, notify: Optional[Callable[[int], None]] = None,
                      allow_wait: bool = True) -> None:
        """Block until `needed` tokens fit in the model's per-minute budget. With allow_wait=False,
        a full budget marks the model busy and raises at once, so the caller switches provider."""
        limit = self.limit_for(model)
        if not limit:
            return
        budget = int(limit * SAFETY)
        while True:
            with self._lock:
                now = time.time()
                used = self._used(model, now)
                q = self._usage[model]
                if used + needed <= budget or not q:
                    q.append((now, needed))  # reserve; corrected by record()
                    return
                freed, wait = 0, WINDOW_SECONDS
                for ts, tokens in q:
                    freed += tokens
                    if used - freed + needed <= budget:
                        wait = ts + WINDOW_SECONDS - now
                        break
            wait = max(1.0, min(wait, WINDOW_SECONDS))
            if not allow_wait:
                self.mark_exhausted(model, wait, minimum=wait)
                raise ModelBusy(model, wait)
            if notify:
                notify(int(wait))
            time.sleep(wait)

    def record(self, model: str, reserved: int, actual: Optional[int]) -> None:
        """Replace the reservation with the real token count from the response."""
        if actual is None:
            return
        with self._lock:
            q = self._usage.setdefault(model, deque())
            for i in range(len(q) - 1, -1, -1):
                if q[i][1] == reserved:
                    q[i] = (q[i][0], int(actual))
                    break


_budget = TokenBudget()

# Real token usage reported by the providers, for the per-reply meter in the UI and for tests.
usage_log: list = []  # (model, total_tokens)


def get_budget() -> TokenBudget:
    return _budget


def invoke_with_budget(llm: Any, model: str, messages: Sequence[Any], tools: Optional[Sequence[Any]] = None,
                       max_output: int = 0, notify: Optional[Callable[[int], None]] = None,
                       allow_wait: bool = True) -> Any:
    """
    Invoke an LLM after making sure it fits the per-minute budget. Short
    rate-limit waits are absorbed here; daily limits raise DailyLimitReached.
    """
    remaining = _budget.exhausted_for(model)
    if remaining:
        raise DailyLimitReached(model, remaining)

    needed, raw_input = _budget.estimate(model, messages, tools, max_output)
    for attempt in range(MAX_RETRIES + 1):
        _budget.wait_for_room(model, needed, notify, allow_wait=allow_wait)
        try:
            result = llm.invoke(list(messages))
        except Exception as exc:  # noqa: BLE001
            _budget.record(model, needed, needed)
            status = getattr(exc, "status_code", None)
            text = str(exc).lower()
            if status in (401, 403) or any(k in text for k in ("credit balance", "billing", "insufficient_quota",
                                                                "invalid x-api-key", "invalid api key")):
                # no balance / bad or revoked key (Anthropic says "credit balance is too low" with a 400):
                # skip this provider for an hour, the next one in the chain answers at once
                _budget.mark_exhausted(model, 3600)
                raise DailyLimitReached(model, 3600, str(exc)) from exc
            if status in (402, 404) or "model_not_found" in str(exc):
                # no credit / not available on this account -> let callers fall back
                _budget.mark_exhausted(model, 3600)
                raise DailyLimitReached(model, 3600, str(exc)) from exc
            if _is_provider_down(exc):
                _budget.mark_exhausted(model, 60)  # provider outage -> try the next provider for a minute
                raise DailyLimitReached(model, 60, str(exc)) from exc
            if getattr(exc, "status_code", None) == 413 or "request too large" in str(exc).lower():
                raise RequestTooLarge(model, str(exc)) from exc
            if not _is_rate_limit(exc):
                raise
            message = str(exc)
            wait = _retry_after(message)
            if _is_daily(message) and not _has_retry_time(message):
                # e.g. OpenRouter "free-models-per-day" gives no reset time: skip this model for an hour
                # instead of re-trying it on every call (each retry costs the customer ~0.5 s).
                wait = DAILY_RECHECK_SECONDS
            if _is_daily(message) or wait > MAX_SHORT_WAIT:
                _budget.mark_exhausted(model, wait)
                raise DailyLimitReached(model, wait, message) from exc
            if not allow_wait:  # another provider is available: switch now, come back after the window
                _budget.mark_exhausted(model, wait + 1, minimum=wait + 1)
                raise ModelBusy(model, wait + 1, message) from exc
            if attempt == MAX_RETRIES:
                raise
            if notify:
                notify(int(wait) + 1)
            time.sleep(wait + 1)
            continue
        usage = getattr(result, "usage_metadata", None) or {}
        _budget.record(model, needed, usage.get("total_tokens"))
        _budget.learn(model, raw_input, usage)
        usage_log.append((model, int(usage.get("total_tokens") or 0)))
        return result
    raise RuntimeError("unreachable")


def _invoke_chain(role: str, make_runnable: Callable[[Any], Any],
                          messages_for: Callable[[str, float], Sequence[Any]],
                          tools: Optional[Sequence[Any]] = None, max_output: int = 0,
                          notify: Optional[Callable[[int], None]] = None,
                          temperature: float = 0.2,
                          accept: Optional[Callable[[Any], bool]] = None) -> Tuple[Any, str]:
    """
    Run a call on the role's model chain (settings.json -> models.<role>): preferred model
    first, then fallbacks across providers when a model hits a limit, is unavailable, its
    provider is down, or its request is too large. Returns (result, model_used).
    """
    from agent.agents import get_llm, role_models

    waits: list = []
    last_error: Optional[Exception] = None
    chain = role_models(role)
    for model in chain:
        remaining = _budget.exhausted_for(model)
        if remaining:
            waits.append(remaining)
            continue
        # Never wait on one model: a busy model is skipped at once (marked busy until its window frees).
        # When EVERY model is busy, invoke_with_fallbacks waits once for whichever frees up first.
        # (Waiting inline on the last model used to retry e.g. Cloudflare 3 x 61 s while Groq was free in 20 s.)
        allow_wait = False
        runnable = make_runnable(get_llm(role, temperature=temperature, model=model))
        try:
            for factor in (0.85, 0.6):  # retry once, tighter, if the request is too large
                try:
                    result = invoke_with_budget(runnable, model, messages_for(model, factor), tools,
                                                max_output=max_output, notify=notify, allow_wait=allow_wait)
                except RequestTooLarge as exc:
                    last_error = exc
                    continue
                if accept is None or accept(result):
                    return result, model
                last_error = RuntimeError(f"{model} returned an unusable (empty) response")
                break  # weak/empty answer -> next model
        except DailyLimitReached as exc:
            waits.append(exc.retry_after)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            from_provider = getattr(exc, "status_code", None) is not None or type(exc).__module__.split(".")[0] in (
                "openai", "groq", "anthropic", "httpx", "httpx2", "langchain_openai", "langchain_groq")
            if from_provider or "tool_use_failed" in text or "failed to call a function" in text or "rate limit" in text:
                log.warning("model %s failed for %s (%s: %s) - trying the next model", model, role,
                            type(exc).__name__, str(exc)[:300])
                last_error = exc  # this provider rejected the request -> next model in the chain
                continue
            raise
    if waits:
        raise AllModelsBusy(min(waits))
    raise last_error or RuntimeError(f"No model available for {role}")


def invoke_with_fallbacks(role: str, make_runnable: Callable[[Any], Any],
                          messages_for: Callable[[str, float], Sequence[Any]],
                          tools: Optional[Sequence[Any]] = None, max_output: int = 0,
                          notify: Optional[Callable[[int], None]] = None,
                          temperature: float = 0.2,
                          accept: Optional[Callable[[Any], bool]] = None) -> Tuple[Any, str]:
    """
    Run a call on the role's model chain, switching providers instantly on limits/outages.
    Only when EVERY model is busy: wait for the one that frees up first (per-minute windows), then
    run the chain again from the top (preferred model first).
    """
    attempts = 3
    for attempt in range(attempts):
        try:
            return _invoke_chain(role, make_runnable, messages_for, tools, max_output, notify, temperature, accept)
        except AllModelsBusy as exc:
            if attempt == attempts - 1 or exc.retry_after > MAX_SHORT_WAIT:
                raise
            if notify:
                notify(int(exc.retry_after) + 1)
            time.sleep(exc.retry_after + 1)
    raise RuntimeError("unreachable")
