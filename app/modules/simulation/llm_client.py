"""Shared OpenAI chat-completions HTTP client for the simulation module."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMRequestError(RuntimeError):
    """Raised for any failure of the OpenAI chat completion call: network, HTTP, timeout, or a malformed response."""


def call_openai_chat_json(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response_format: dict,
    timeout: int,
) -> dict:
    """POST a chat.completions request and return the parsed JSON object found in
    choices[0].message.content. Raises LLMRequestError on any transport, HTTP,
    timeout, or parse failure. Does not interpret or validate the parsed content —
    schema-specific validation stays with the caller."""
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": response_format,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)
    except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise LLMRequestError(f"OpenAI chat completion failed: {type(error).__name__}") from error
