"""Chamada ao modelo de linguagem, sem LangChain (ADR-014).

- Ollama: API nativa, com `num_ctx` do tamanho do prompt (ADR-010). Sem ele o
  Ollama corta o prompt em 4096 tokens sem avisar.
- OpenAI e OpenRouter: SDK oficial `openai` (OpenRouter fala o mesmo protocolo).
"""
from __future__ import annotations

import json
from typing import Optional

import requests

from config.settings import get_settings
from domain.interfaces.llm_service import LLMServiceError
from infrastructure.observability import timed

NUM_PREDICT = 2000
_CTX_STEPS = (4096, 8192, 16384, 32768, 65536, 131072)


class ModelClient:
    def __init__(self, settings=None) -> None:
        self._s = settings or get_settings()
        self._openai = None
        provider = self._s.llm_provider
        # OpenAI sem chave cai no Ollama local, como antes.
        self.ollama = provider == "ollama" or (provider == "openai" and not self._s.openai_api_key.strip())
        self._provider = "ollama" if self.ollama else provider

    def complete(self, messages: list[dict], as_json: bool = False) -> str:
        try:
            with timed("llm_seconds", {"kind": "json" if as_json else "text", "provider": self._provider}):
                return self._ollama(messages, as_json) if self.ollama else self._compatible(messages, as_json)
        except LLMServiceError:
            raise
        except Exception as exc:
            raise LLMServiceError(f"Falha ao chamar o modelo: {exc}") from exc

    def complete_tools(self, messages: list[dict], tools: list[dict]) -> tuple[str, Optional[dict]]:
        """Resposta em texto ou o pedido de UMA ferramenta: (texto, {"name", "arguments"} | None).
        `tools` no formato de função da OpenAI, que o Ollama também aceita."""
        with timed("llm_seconds", {"kind": "tools", "provider": self._provider}):
            return self._complete_tools(messages, tools)

    def _complete_tools(self, messages: list[dict], tools: list[dict]) -> tuple[str, Optional[dict]]:
        try:
            if self.ollama:
                message = self._ollama_message(messages, as_json=False, tools=tools)
                calls = message.get("tool_calls") or []
                call = calls[0]["function"] if calls else None
                if call and isinstance(call.get("arguments"), str):
                    call = {**call, "arguments": json.loads(call["arguments"])}
                return message.get("content") or "", call and {"name": call["name"], "arguments": call["arguments"]}
            self._ensure_openai()
            resp = self._openai.chat.completions.create(model=self._model, messages=messages, temperature=0.2,
                                                         tools=tools)
            message = resp.choices[0].message
            if message.tool_calls:
                fn = message.tool_calls[0].function
                return message.content or "", {"name": fn.name, "arguments": json.loads(fn.arguments or "{}")}
            return message.content or "", None
        except LLMServiceError:
            raise
        except Exception as exc:
            raise LLMServiceError(f"Falha ao chamar o modelo: {exc}") from exc

    def _ollama(self, messages: list[dict], as_json: bool) -> str:
        return self._ollama_message(messages, as_json)["content"]

    def _ollama_message(self, messages: list[dict], as_json: bool, tools: Optional[list[dict]] = None) -> dict:
        # ~2,6 caracteres por token em português (medido); 2,4 dá folga. As ferramentas também ocupam a janela.
        chars = sum(len(m["content"]) for m in messages) + (len(json.dumps(tools)) if tools else 0)
        needed = int(chars / 2.4) + NUM_PREDICT
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
        if tools:
            payload["tools"] = tools
        base = self._s.ollama_base_url.rstrip("/").removesuffix("/v1")
        resp = requests.post(f"{base}/api/chat", json=payload, timeout=600)
        resp.raise_for_status()
        return resp.json()["message"]

    def _compatible(self, messages: list[dict], as_json: bool) -> str:
        self._ensure_openai()
        extra: dict = {"response_format": {"type": "json_object"}} if as_json else {}
        resp = self._openai.chat.completions.create(model=self._model, messages=messages, temperature=0.2, **extra)
        return resp.choices[0].message.content or ""

    def _ensure_openai(self) -> None:
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
