"""LLM backend abstraction with content-hash caching."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import ollama

CACHE_PATH = Path("./.llm_cache")
DEBUG_PATH = Path("./.llm_debug")

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_OPTIONS = {
    # Qwen3 recommendation; temperature 0 makes Qwen3 loop. Revisit for other models.
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "seed": 0,
    "num_ctx": 16384,
    "num_predict": 8192,  # hard stop, otherwise a looping model never ends
}

LOOP_CHECK_EVERY = 32
MIN_LOOP_CHARS = 400


class GenerationError(RuntimeError):
    """The model did not produce a usable answer; the partial output is kept."""

    def __init__(self, message: str, debug_file: Optional[Path] = None):
        super().__init__(message + (f" (partial output: {debug_file})" if debug_file else ""))
        self.debug_file = debug_file


def _loop_reason(text: str) -> Optional[str]:
    """Detect a model stuck repeating itself at the end of its output."""
    tail = text[-2000:]
    if len(tail) >= 300 and not tail[-300:].strip():
        return "endless whitespace"
    for period in range(8, 401):
        reps = max(4, -(-MIN_LOOP_CHARS // period))
        span = period * reps
        if len(tail) < span:
            continue
        if tail[-span:] == tail[-period:] * reps:
            unit = " ".join(tail[-period:].split())
            return f"repeating {unit[:70]!r}{'…' if len(unit) > 70 else ''} {reps}+ times"
    return None


def _progress(tokens: int, start: float, text: str, thinking: bool) -> None:
    rate = tokens / max(time.time() - start, 1e-6)
    tail = text[-50:].replace("\n", "⏎").replace("\r", "")
    phase = "thinking" if thinking else "writing"
    sys.stdout.write(f"\r\033[K  {tokens:6d} tokens  {rate:5.1f} tok/s  {phase}  …{tail}")
    sys.stdout.flush()


@dataclass
class LLMResponse:
    """A model response plus everything needed to reproduce it."""

    content: str
    model: str
    options: dict
    prompt_sha256: str
    cache_key: str
    cached: bool
    duration_s: float
    think: Optional[bool] = None

    def provenance(self) -> dict:
        """Reduce to the fields worth keeping in a provenance record."""
        return {
            "model": self.model,
            "options": self.options,
            "think": self.think,
            "promptSha256": self.prompt_sha256,
            "cacheKey": self.cache_key,
            "cached": self.cached,
            "durationSeconds": round(self.duration_s, 3),
        }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class OllamaClient:
    """Ollama backend."""

    model: str = DEFAULT_MODEL
    host: Optional[str] = None
    options: dict = field(default_factory=lambda: dict(DEFAULT_OPTIONS))
    keep_alive: str = "5m"
    use_cache: bool = True
    cache_path: Path = CACHE_PATH

    def __post_init__(self):
        self.host = self.host or os.environ.get("OLLAMA_HOST")
        self._client = ollama.Client(host=self.host) if self.host else ollama.Client()
        if self.use_cache:
            self.cache_path.mkdir(exist_ok=True)

    def _cache_key(self, prompt: str, schema: Optional[dict], think: Optional[bool]) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "schema": schema,
            "options": self.options,
            "think": think,
        }
        return sha256_text(json.dumps(payload, sort_keys=True, default=str))

    def _cache_file(self, key: str) -> Path:
        return self.cache_path / f"{key}.json"

    def chat(
        self,
        prompt: str,
        schema: Optional[dict] = None,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        """Send a single-turn prompt."""
        key = self._cache_key(prompt, schema, think)
        prompt_hash = sha256_text(prompt)

        if self.use_cache:
            cache_file = self._cache_file(key)
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                return LLMResponse(
                    content=cached["content"],
                    model=self.model,
                    options=self.options,
                    prompt_sha256=prompt_hash,
                    cache_key=key,
                    cached=True,
                    duration_s=0.0,
                    think=think,
                )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "options": self.options,
            "keep_alive": self.keep_alive,
        }
        if schema is not None:
            kwargs["format"] = schema
        if think is not None:
            kwargs["think"] = think

        start = time.time()
        content = self._stream(kwargs, key, start).strip()
        duration = time.time() - start

        if self.use_cache:
            self._cache_file(key).write_text(
                json.dumps({"content": content, "model": self.model}, ensure_ascii=False),
                encoding="utf-8",
            )

        return LLMResponse(
            content=content,
            model=self.model,
            options=self.options,
            prompt_sha256=prompt_hash,
            cache_key=key,
            cached=False,
            duration_s=duration,
            think=think,
        )

    def _stream(self, kwargs: dict, key: str, start: float) -> str:
        """Stream the answer with live progress, stopping loops early."""
        parts, thinking_parts, tokens, done_reason = [], [], 0, None

        def dump(reason: str) -> Path:
            DEBUG_PATH.mkdir(exist_ok=True)
            path = DEBUG_PATH / f"{time.strftime('%Y%m%d-%H%M%S')}_{key[:12]}.txt"
            path.write_text(
                f"# {reason}\n# model={self.model} tokens={tokens}\n\n"
                + ("## thinking\n" + "".join(thinking_parts) + "\n\n## answer\n" if thinking_parts else "")
                + "".join(parts),
                encoding="utf-8",
            )
            return path

        try:
            stream = self._client.chat(**kwargs, stream=True)
            for chunk in stream:
                message = chunk.get("message") or {}
                if message.get("thinking"):
                    thinking_parts.append(message["thinking"])
                if message.get("content"):
                    parts.append(message["content"])
                tokens += 1
                if chunk.get("done"):
                    done_reason = chunk.get("done_reason")
                    tokens = chunk.get("eval_count") or tokens
                if tokens % LOOP_CHECK_EVERY == 0:
                    text = "".join(parts) or "".join(thinking_parts)
                    _progress(tokens, start, text, thinking=not parts)
                    reason = _loop_reason(text)
                    if reason:
                        stream.close()
                        raise GenerationError(f"model is looping ({reason}) after {tokens} tokens", dump(reason))
        except KeyboardInterrupt:
            print(f"\n  interrupted, partial output: {dump('interrupted by user')}")
            raise
        finally:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        if done_reason == "length":
            raise GenerationError(
                f"output cut off at the limit of {self.options.get('num_predict')} tokens "
                "(model probably looping, or the document needs a higher --max-tokens)",
                dump("hit num_predict"),
            )
        return "".join(parts)

    def unload(self):
        """Drop the model from VRAM. Call this when a batch is finished."""
        try:
            self._client.chat(
                model=self.model,
                messages=[{"role": "user", "content": ""}],
                keep_alive=0,
            )
        except Exception as e:
            print(f"  [WARN] Could not unload {self.model}: {e}")
