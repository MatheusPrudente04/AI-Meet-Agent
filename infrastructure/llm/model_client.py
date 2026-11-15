"""Chamada ao modelo de linguagem, sem LangChain (ADR-014).

- Ollama: API nativa, com `num_ctx` do tamanho do prompt (ADR-010). Sem ele o
  Ollama corta o prompt em 4096 tokens sem avisar.
- OpenAI e OpenRouter: SDK oficial `openai` (OpenRouter fala o mesmo protocolo).
"""
from __future__ import annotations

from typing import Optional

import requests

from config.settings import get_settings
from domain.interfaces.llm_service import LLMServiceError

NUM_PREDICT = 2000
_CTX_STEPS = (4096, 8192, 16384, 32768, 65536, 131072)


class ModelClient:
    def __init__(self, settings=None) -> None:
        self._s = settings or get_settings()
        self._openai = None
        provider = self._s.llm_provider
        # OpenAI sem chave cai no Ollama local, como antes.
        self.ollama = provider == "ollama" or (provider == "openai" and not self._s.openai_api_key.strip())

    def complete(self, messages: list[dict], as_json: bool = False) -> str:
        try:
            return self._ollama(messages, as_json) if self.ollama else self._compatible(messages, as_json)
        except LLMServiceError:
            raise
        except Exception as exc:
            raise LLMServiceError(f"Falha ao chamar o modelo: {exc}") from exc

    def _ollama(self, messages: list[dict], as_json: bool) -> str:
        # ~2,6 caracteres por token em português (medido); 2,4 dá folga.
        needed = int(sum(len(m["content"]) for m in messages) / 2.4) + NUM_PREDICT
        if needed > self._s.ollama_num_ctx_max:
            raise LLMServiceError(
                f"Contexto de ~{needed} tokens excede o limite configurado "
                f"({self._s.ollama_num_ctx_max}). Reduza LLM_CONTEXT_CHARS ou aumente OLLAMA_NUM_CTX_MAX."
            )
        # Degraus fixos: o modelo não é recarregado a cada pedido.
        num_ctx = next(c for c in _CTX_STEPS if c >= needed)
        payload = {
            "model": self._s.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": NUM_PREDICT, "temperature": 0.1, "num_ctx": num_ctx},
        }
        if as_json:
            payload["format"] = "json"
        base = self._s.ollama_base_url.rstrip("/").removesuffix("/v1")
        resp = requests.post(f"{base}/api/chat", json=payload, timeout=600)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def _compatible(self, messages: list[dict], as_json: bool) -> str:
        if self._openai is None:
            from openai import OpenAI
            if self._s.llm_provider == "openrouter":
                self._openai = OpenAI(
                    api_key=self._s.openrouter_api_key, base_url="https://openrouter.ai/api/v1",
                    default_headers={"HTTP-Referer": self._s.openrouter_site_url, "X-Title": self._s.openrouter_site_name},
                )
                self._model = self._s.openrouter_model
            else:
                self._openai = OpenAI(api_key=self._s.openai_api_key)
                self._model = self._s.openai_model
        extra: dict = {"response_format": {"type": "json_object"}} if as_json else {}
        resp = self._openai.chat.completions.create(model=self._model, messages=messages, temperature=0.2, **extra)
        return resp.choices[0].message.content or ""
