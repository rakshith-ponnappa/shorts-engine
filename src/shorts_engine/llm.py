"""Optional OpenAI / Azure OpenAI wrapper for scoring, copywriting, and TTS."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(self, settings: dict):
        self.provider = (settings.get("provider") or "none").lower()
        self.model = settings.get("model", "gpt-4o-mini")
        self.tts_model = settings.get("tts_model", "gpt-4o-mini-tts")
        self.tts_voice = settings.get("tts_voice", "alloy")
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.provider in ("openai", "azure")

    @property
    def client(self):
        if not self.enabled:
            raise LLMUnavailable("llm.provider is 'none'")
        if self._client is None:
            try:
                import openai
            except ImportError as e:
                raise LLMUnavailable("pip install 'shorts-engine[llm]'") from e
            if self.provider == "azure":
                self._client = openai.AzureOpenAI(
                    api_key=os.environ["AZURE_OPENAI_API_KEY"],
                    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                )
            else:
                self._client = openai.OpenAI()
        return self._client

    def chat_json(self, system: str, user: str, temperature: float = 0.7) -> Any:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return json.loads(resp.choices[0].message.content or "{}")

    def tts(self, text: str, out_path: Path) -> Path:
        with self.client.audio.speech.with_streaming_response.create(
            model=self.tts_model, voice=self.tts_voice, input=text, response_format="mp3"
        ) as resp:
            resp.stream_to_file(out_path)
        return out_path
