from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from typing import Any

import requests

from .config import TranslationConfig
from .models import SubtitleSegment

LOGGER = logging.getLogger(__name__)


class Translator:
    def __init__(self, config: TranslationConfig) -> None:
        self.config = config

    def translate(self, segments: list[SubtitleSegment]) -> list[SubtitleSegment]:
        if not self.config.enabled or self.config.provider == "none":
            LOGGER.info("Translation disabled; Chinese subtitles will mirror Japanese text")
            for segment in segments:
                segment.translation = segment.text
            return segments

        provider = self.config.provider
        if provider == "openai_responses":
            translations = self._translate_openai_responses([item.text for item in segments])
        elif provider == "openai_compatible":
            translations = self._translate_openai_compatible([item.text for item in segments])
        elif provider == "deepl":
            translations = self._translate_deepl([item.text for item in segments])
        elif provider == "argos":
            translations = self._translate_argos([item.text for item in segments])
        elif provider == "transformers_seq2seq":
            translations = self._translate_transformers_seq2seq([item.text for item in segments])
        elif provider == "external":
            translations = self._translate_external([item.text for item in segments])
        else:
            raise ValueError(f"Unsupported translation provider: {provider}")

        if len(translations) != len(segments):
            raise RuntimeError(
                f"Translator returned {len(translations)} items for {len(segments)} segments"
            )
        for segment, text in zip(segments, translations):
            segment.translation = (text or segment.text).strip()
        return segments

    def _translate_openai_responses(self, texts: list[str]) -> list[str]:
        cfg = self.config.openai_responses
        base_url = str(cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        api_key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"Environment variable {api_key_env} is required for OpenAI translation")

        model = str(cfg.get("model", "gpt-5.5"))
        timeout = float(cfg.get("timeout_seconds", 180))
        batch_size = int(self.config.batch_size or 20)
        results: list[str] = []
        for batch in _chunks(texts, batch_size):
            results.extend(
                self._retry(
                    lambda: self._openai_responses_batch(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        timeout=timeout,
                        batch=batch,
                        cfg=cfg,
                    )
                )
            )
        return results

    def _openai_responses_batch(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        batch: list[str],
        cfg: dict[str, Any],
    ) -> list[str]:
        payload: dict[str, Any] = {
            "model": model,
            "instructions": (
                "Translate Japanese livestream subtitles into natural Simplified Chinese. "
                "Preserve names, nicknames, song titles, jokes, tone, and implied subjects. "
                "Keep each output aligned with the input item at the same id. "
                "Do not add explanations, timestamps, brackets, or extra items."
            ),
            "input": json.dumps(
                {"segments": [{"id": idx, "text": text} for idx, text in enumerate(batch)]},
                ensure_ascii=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "subtitle_translations",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "translations": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["translations"],
                    },
                }
            },
        }
        reasoning_effort = cfg.get("reasoning_effort", "high")
        if reasoning_effort:
            payload["reasoning"] = {"effort": str(reasoning_effort)}
        max_output_tokens = cfg.get("max_output_tokens", 8192)
        if max_output_tokens:
            payload["max_output_tokens"] = int(max_output_tokens)
        temperature = cfg.get("temperature")
        if temperature is not None:
            payload["temperature"] = float(temperature)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        org_env = str(cfg.get("organization_env", "OPENAI_ORG_ID"))
        project_env = str(cfg.get("project_env", "OPENAI_PROJECT_ID"))
        org = os.getenv(org_env, "")
        project = os.getenv(project_env, "")
        if org:
            headers["OpenAI-Organization"] = org
        if project:
            headers["OpenAI-Project"] = project

        session = requests.Session()
        session.trust_env = bool(cfg.get("trust_env", False))
        response = session.post(
            f"{base_url}/responses",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI Responses translation failed {response.status_code}: {response.text[:1000]}")
        data = response.json()
        content = _extract_response_text(data)
        parsed = _parse_json_from_text(content)
        translations = parsed.get("translations") if isinstance(parsed, dict) else parsed
        if not isinstance(translations, list):
            raise RuntimeError(f"Unexpected OpenAI translation response: {content[:500]}")
        if len(translations) != len(batch):
            raise RuntimeError(f"Expected {len(batch)} translations, got {len(translations)}")
        return [str(item).strip() for item in translations]

    def _translate_openai_compatible(self, texts: list[str]) -> list[str]:
        cfg = self.config.openai_compatible
        base_url = str(cfg.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        api_key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"Environment variable {api_key_env} is required for translation")

        model = str(cfg.get("model", "gpt-4o-mini"))
        timeout = float(cfg.get("timeout_seconds", 120))
        batch_size = int(self.config.batch_size or 20)
        results: list[str] = []
        for batch in _chunks(texts, batch_size):
            results.extend(
                self._retry(
                    lambda: self._openai_batch(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        timeout=timeout,
                        batch=batch,
                        temperature=float(cfg.get("temperature", 0.1)),
                        json_mode=bool(cfg.get("json_mode", False)),
                    )
                )
            )
        return results

    def _openai_batch(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        batch: list[str],
        temperature: float,
        json_mode: bool,
    ) -> list[str]:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate Japanese livestream subtitles into natural Simplified Chinese. "
                        "Keep meaning, names, jokes, and tone. Do not add explanations. "
                        "Return JSON only in the shape {\"translations\":[\"...\"]}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"segments": [{"id": idx, "text": text} for idx, text in enumerate(batch)]},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        session = requests.Session()
        session.trust_env = bool(self.config.openai_compatible.get("trust_env", False))
        response = session.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _parse_json_from_text(content)
        translations = parsed.get("translations") if isinstance(parsed, dict) else parsed
        if not isinstance(translations, list):
            raise RuntimeError(f"Unexpected translation response: {content[:500]}")
        if len(translations) != len(batch):
            raise RuntimeError(f"Expected {len(batch)} translations, got {len(translations)}")
        return [str(item).strip() for item in translations]

    def _translate_deepl(self, texts: list[str]) -> list[str]:
        cfg = self.config.deepl
        api_key_env = str(cfg.get("api_key_env", "DEEPL_API_KEY"))
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"Environment variable {api_key_env} is required for DeepL")
        endpoint = str(cfg.get("endpoint", "https://api-free.deepl.com/v2/translate"))
        target_lang = str(cfg.get("target_lang", "ZH-HANS"))
        results: list[str] = []
        for batch in _chunks(texts, int(self.config.batch_size or 20)):
            payload: list[tuple[str, str]] = [("auth_key", api_key), ("source_lang", "JA"), ("target_lang", target_lang)]
            payload.extend(("text", text) for text in batch)
            def post_deepl():
                session = requests.Session()
                session.trust_env = bool(cfg.get("trust_env", False))
                return session.post(endpoint, data=payload, timeout=120)

            data = self._retry(post_deepl)
            data.raise_for_status()
            body = data.json()
            results.extend(item["text"] for item in body.get("translations", []))
        return results

    def _translate_argos(self, texts: list[str]) -> list[str]:
        cfg = self.config.argos
        from_lang = str(cfg.get("from_lang", "ja"))
        to_lang = str(cfg.get("to_lang", "zh"))
        try:
            import argostranslate.translate
        except ImportError as exc:
            raise RuntimeError("argos-translate is not installed") from exc
        return [argostranslate.translate.translate(text, from_lang, to_lang) for text in texts]

    def _translate_transformers_seq2seq(self, texts: list[str]) -> list[str]:
        cfg = self.config.transformers
        model_path = str(cfg.get("model_path", "")).strip()
        if not model_path:
            raise RuntimeError("translation.transformers.model_path is required")
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Local Transformers translation requires: pip install -r requirements-local.txt"
            ) from exc

        local_files_only = bool(cfg.get("local_files_only", True))
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=local_files_only)
        source_lang = str(cfg.get("source_lang", "")).strip()
        target_lang = str(cfg.get("target_lang", "")).strip()
        if source_lang and hasattr(tokenizer, "src_lang"):
            tokenizer.src_lang = source_lang

        device = str(cfg.get("device", "auto"))
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        model_kwargs: dict[str, Any] = {"local_files_only": local_files_only}
        torch_dtype = str(cfg.get("torch_dtype", "")).strip()
        if torch_dtype and device != "cpu":
            dtype = getattr(torch, torch_dtype, None)
            if dtype is None:
                raise RuntimeError(f"Unsupported torch dtype: {torch_dtype}")
            model_kwargs["torch_dtype"] = dtype

        model = AutoModelForSeq2SeqLM.from_pretrained(model_path, **model_kwargs)
        model.to(device)
        model.eval()

        batch_size = int(cfg.get("batch_size", self.config.batch_size or 8))
        max_input_tokens = int(cfg.get("max_input_tokens", 256))
        max_new_tokens = int(cfg.get("max_new_tokens", 160))
        num_beams = int(cfg.get("num_beams", 4))
        results: list[str] = []
        for batch in _chunks(texts, batch_size):
            encoded = tokenizer(
                batch,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=max_input_tokens,
            )
            encoded = {key: value.to(device) for key, value in encoded.items()}
            gen_kwargs: dict[str, Any] = {
                "max_new_tokens": max_new_tokens,
                "num_beams": num_beams,
            }
            forced_bos_token_id = _target_lang_token_id(tokenizer, target_lang)
            if forced_bos_token_id is not None:
                gen_kwargs["forced_bos_token_id"] = forced_bos_token_id
            with torch.no_grad():
                output_ids = model.generate(**encoded, **gen_kwargs)
            results.extend(
                item.strip()
                for item in tokenizer.batch_decode(output_ids, skip_special_tokens=True)
            )
        return results

    def _translate_external(self, texts: list[str]) -> list[str]:
        cfg = self.config.external
        command = cfg.get("command") or []
        if isinstance(command, str):
            command = shlex.split(command)
        if not command:
            raise RuntimeError("translation.external.command is empty")
        payload = json.dumps(texts, ensure_ascii=False)
        completed = subprocess.run(
            command,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"External translator failed: {completed.stderr}")
        parsed = _parse_json_from_text(completed.stdout)
        if not isinstance(parsed, list):
            raise RuntimeError("External translator must output a JSON array of strings")
        return [str(item) for item in parsed]

    def _retry(self, func):
        last_exc: Exception | None = None
        for attempt in range(1, int(self.config.retries or 1) + 1):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= int(self.config.retries or 1):
                    break
                delay = min(30, 2**attempt)
                LOGGER.warning("Translation attempt %d failed: %s; retrying in %ss", attempt, exc, delay)
                time.sleep(delay)
        raise last_exc or RuntimeError("Translation failed")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), max(1, size))]


def _target_lang_token_id(tokenizer: Any, target_lang: str) -> int | None:
    if not target_lang:
        return None
    if hasattr(tokenizer, "get_lang_id"):
        return int(tokenizer.get_lang_id(target_lang))
    lang_code_to_id = getattr(tokenizer, "lang_code_to_id", None)
    if isinstance(lang_code_to_id, dict) and target_lang in lang_code_to_id:
        return int(lang_code_to_id[target_lang])
    token_id = tokenizer.convert_tokens_to_ids(target_lang)
    unk_token_id = getattr(tokenizer, "unk_token_id", None)
    if isinstance(token_id, int) and token_id >= 0 and token_id != unk_token_id:
        return token_id
    return None


def _parse_json_from_text(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        return json.loads(fenced.group(1).strip())
    start_candidates = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
    if not start_candidates:
        raise ValueError(f"No JSON object or array found in translator response: {text[:500]}")
    start = min(start_candidates)
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)
    if end < start:
        raise ValueError(f"No complete JSON object or array found in translator response: {text[:500]}")
    return json.loads(text[start : end + 1])


def _extract_response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    parts: list[str] = []
    for output_item in data.get("output", []):
        if not isinstance(output_item, dict):
            continue
        for content in output_item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(text, dict) and isinstance(text.get("value"), str):
                parts.append(text["value"])
    if parts:
        return "\n".join(parts)
    raise RuntimeError(f"Could not extract text from OpenAI Responses payload: {json.dumps(data)[:1000]}")
