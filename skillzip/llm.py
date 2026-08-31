"""Minimal OpenAI-compatible LLM client: disk cache, retry, streaming, plus a
deterministic mock backend for fully offline runs.

This is SkillZip's *only* model-backed dependency. It serves the three
model-assisted stages -- contract extraction, relation checking, and the
structural audit -- while selection (minimum-cost cover) and rendering stay
completely deterministic.

The client is endpoint-agnostic: pass any OpenAI-compatible ``base_url`` and
supply the key through the ``api_key`` argument or the ``DASHSCOPE_API_KEY``
environment variable. Never hard-code credentials. Omit the key (or pass
``--no-llm`` on the CLI) to run the deterministic-only path.

Responses are cached on disk keyed by (model, prompt, params), which makes
repeated experiments cheap and reproducible.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional


def cache_key(model: str, prompt: str, params: dict) -> str:
    blob = json.dumps({"m": model, "p": prompt, "k": params}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _mock_response(prompt: str) -> str:
    """Deterministic offline stub used by ``backend="mock"``.

    It returns a fixed, prompt-hashed string rather than contacting any service.
    SkillZip's model-assisted stages validate every model reply and fall back to
    their deterministic parsers when a reply is unusable, so the mock backend
    lets the whole pipeline run with no network access.
    """
    h = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
    return f"[mock-response {h % 10000}]"


class LLMClient:
    def __init__(self, model: str, backend: str = "auto", base_url: str = "",
                 api_key: str = "", cache_dir: str = ".skillzip_cache",
                 timeout_s: int = 120, temperature: float = 0.0,
                 enable_thinking: Optional[bool] = None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.enable_thinking = enable_thinking
        self.backend = self._resolve_backend(backend)
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._mock_calls = 0

    def _resolve_backend(self, backend: str) -> str:
        if backend != "auto":
            return backend
        return "real" if self.api_key else "mock"

    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{self.model}__{key}.json")

    def _wants_thinking_flag(self) -> bool:
        # The enable_thinking switch is a Qwen3 feature; other vendors (e.g.
        # deepseek) may 400 on an unknown field, so only send it for qwen models.
        return self.enable_thinking is not None and "qwen" in self.model.lower()

    def chat(self, prompt: str, **params) -> str:
        p = {"temperature": params.get("temperature", self.temperature)}
        if self._wants_thinking_flag():
            p["enable_thinking"] = self.enable_thinking
        key = cache_key(self.model, prompt, p)
        path = self._cache_path(key)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)["response"]
        resp = self._mock(prompt) if self.backend == "mock" else self._real(prompt, p)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"prompt": prompt, "response": resp}, f)
        return resp

    def _mock(self, prompt: str) -> str:
        self._mock_calls += 1
        return _mock_response(prompt)

    def _real(self, prompt: str, params: dict) -> str:
        """Call the OpenAI-compatible endpoint with streaming. Streaming makes
        `timeout_s` an inter-chunk inactivity timeout rather than a total read
        timeout, so a long-but-active generation (e.g. code for a spreadsheet
        task) stays alive instead of tripping a whole-response read timeout."""
        import requests
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        body = {"model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": params["temperature"],
                "stream": True}
        if self._wants_thinking_flag():
            # Qwen3 hybrid-reasoning switch; False skips the long chain-of-thought
            # so responses are short/fast and streaming connections stay short-lived.
            body["enable_thinking"] = self.enable_thinking
        # (connect, read) tuple: `read` is the max idle gap between streamed
        # chunks, so a half-closed / stalled connection raises promptly instead
        # of hanging. `deadline` bounds total wall time for one generation; a
        # breach means the model is in a runaway generation (e.g. deepseek with
        # no thinking-off switch on a pathological problem), so we abort WITHOUT
        # retrying the same prompt -- retrying just repeats the runaway stream.
        read_gap = min(self.timeout_s, 60)
        deadline = max(self.timeout_s, 180)
        last = ""
        for attempt in range(4):
            start = time.time()
            hard_stop = False
            try:
                with requests.post(url, headers=headers, json=body,
                                   timeout=(15, read_gap), stream=True) as r:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}: {r.text[:200]}"
                        if r.status_code not in (429, 500, 502, 503, 504):
                            break
                        time.sleep(2 ** attempt)
                        continue
                    chunks: List[str] = []
                    for line in r.iter_lines(decode_unicode=True):
                        if time.time() - start > deadline:
                            last = f"exceeded {deadline}s wall deadline"
                            chunks = []
                            hard_stop = True
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0].get("delta", {})
                        except (ValueError, KeyError, IndexError):
                            continue
                        piece = delta.get("content")
                        if piece:
                            chunks.append(piece)
                    text = "".join(chunks)
                    if text:
                        return text
                    if not last:
                        last = "empty stream response"
            except Exception as e:  # noqa
                last = str(e)
            if hard_stop:
                break
            time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM call failed after retries: {last}")

    def map(self, prompts: List[str], concurrency: int = 8, **params) -> List[str]:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            return list(ex.map(lambda p: self.chat(p, **params), prompts))
