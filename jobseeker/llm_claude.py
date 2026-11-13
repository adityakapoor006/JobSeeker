"""Claude (Anthropic API) JSON calls, the same jobs as the OpenAI ones in llm.py.

Needs ANTHROPIC_API_KEY from console.anthropic.com; a Claude.ai subscription does not cover API use."""
import json
import re

import anthropic

from .config import env

MAIN_MODEL = "claude-opus-5-5"
CHEAP_MODEL = "claude-haiku-5-5"
# Models that accept server-side refusal fallbacks; the API reruns a declined request on another model.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16000
SYSTEM = "Reply with one JSON object and nothing else: no Markdown fences and no commentary."


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def complete_json(prompt: str, cheap: bool = False) -> dict:
    """cheap=True runs the low-cost model at low effort, for fit scoring, email and company checks."""
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    model = (env("ANTHROPIC_CHEAP_MODEL") or CHEAP_MODEL) if cheap else (env("ANTHROPIC_MODEL") or MAIN_MODEL)
    request = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM,
               "output_config": {"effort": "low" if cheap else "medium"},
               "messages": [{"role": "user", "content": prompt}]}
    if model in FALLBACK_MODELS:
        response = client.beta.messages.create(**request, betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default")
    else:
        response = client.messages.create(**request)
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude stopped early ({response.stop_reason})")
    return parse_json("".join(block.text for block in response.content if block.type == "text"))
