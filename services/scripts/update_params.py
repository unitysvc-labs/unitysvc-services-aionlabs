#!/usr/bin/env python3
"""
Param-file generator for AionLabs.

Fetches live models from the AionLabs ``/v1/models`` endpoint and writes one
compact param file per model (``specs/aionlabs/<model>.json`` = ``{parameters}``)
that the ``specs`` pipeline re-renders ephemerally through ``templates/``.
``service_id``s are preserved via ``<model>.service.json`` sidecars.

Unlike most provider scripts, AionLabs' ``/v1/models`` already returns pricing
and context length, so no LiteLLM lookup is needed — every field comes straight
from the upstream catalog. Models carrying an ``expires_at`` (a deprecated model
with a ``replacement_model_id``) are skipped.

Usage: python scripts/update_params.py
"""

import os
import sys
from pathlib import Path
from typing import Iterator

import httpx

from unitysvc_sellers.params_render import write_params_from_iterator

# Provider configuration
PROVIDER_NAME = "aionlabs"
PROVIDER_DISPLAY_NAME = "AionLabs"
API_BASE_URL = "https://api.aionlabs.ai"
ENV_API_KEY_NAME = "AIONLABS_API_KEY"
MODELS_URL = f"{API_BASE_URL}/v1/models"

# Provider-wide rate limits (AionLabs applies these per account).
RATE_LIMITS = [
    {"description": "API request rate limit", "limit": 15, "unit": "requests", "window": "minute"},
    {"description": "Token consumption rate limit", "limit": 50000, "unit": "tokens", "window": "minute"},
]

# Standard "how to use" paragraph — identical for every model (both channels
# share one endpoint; the llm_translator serves both API dialects).
_HOWTO = (
    "Call it in either the OpenAI or Anthropic API style — the gateway auto-detects "
    "the dialect and translates as needed. Two channels share one endpoint: `managed` "
    "(pay per token using the platform's key, nothing to configure) and `byok` (bring "
    'your own AionLabs key and route free). See the code examples and "How to use this '
    'model" for setup.'
)

SCRIPT_DIR = Path(__file__).parent


def _fmt_price(per_million: float) -> str:
    """Per-1M-token price as a string, dropping a trailing .0 on whole numbers."""
    return str(int(per_million)) if per_million == int(per_million) else str(round(per_million, 4))


def _clean_name(raw: str, offering_name: str) -> str:
    """Human display name: strip the ``AionLabs:`` vendor prefix the API adds."""
    name = (raw or offering_name).split(":", 1)[-1].strip()
    return name or offering_name


def iter_models(api_key: str) -> Iterator[dict]:
    """Yield one template-variable dict per live, non-expiring AionLabs model."""
    print(f"Fetching models from {PROVIDER_DISPLAY_NAME} API ({MODELS_URL})...")
    r = httpx.get(MODELS_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    r.raise_for_status()
    models = r.json().get("models", [])
    print(f"Found {len(models)} models\n")

    for i, m in enumerate(models, 1):
        model_id = m.get("id", "")
        if not model_id:
            continue
        # listing.name = <provider>/<bare>; offering + routing use the full id.
        offering_name = model_id.split("/", 1)[-1]
        print(f"[{i}/{len(models)}] {model_id}")

        if m.get("expires_at"):
            print(f"  Skipped: expires {m['expires_at']} "
                  f"(replacement: {m.get('replacement_model_id') or 'none'})")
            continue

        pricing = m.get("pricing") or {}
        prompt = float(pricing.get("prompt", 0) or 0) * 1_000_000
        completion = float(pricing.get("completion", 0) or 0) * 1_000_000
        list_price = {
            "type": "one_million_tokens",
            "input": _fmt_price(prompt),
            "output": _fmt_price(completion),
            "description": f"Pricing Per 1M Input/Output Tokens: ${_fmt_price(prompt)}/${_fmt_price(completion)}",
        }

        context_length = m.get("context_length")
        details = {
            "context_length": context_length,
            "max_completion_tokens": m.get("max_completion_tokens"),
            "model_name": model_id,
            "modality": (m.get("architecture") or {}).get("modality", "text->text"),
            "parameter_count": None,
            "reasoning": bool(m.get("reasoning", False)),
        }

        display_name = _clean_name(m.get("name", ""), offering_name)
        upstream_desc = (m.get("description") or "").strip()
        brief = f"{display_name} is a large language model from AionLabs, available through the UnitySVC gateway."
        detail = (
            f"{upstream_desc} It is served OpenAI-compatibly through the UnitySVC gateway, "
            "which relays your chat-completion requests to the AionLabs API and meters usage "
            f"per token (context window {context_length} tokens)."
        ).strip()
        # Closing pricing paragraph. This service is MULTI-CHANNEL, so unlike a
        # BYOK-only service it has to explain both channels: `managed` is metered
        # by UnitySVC at the per-token rate below, while `byok` is free through
        # the gateway because the customer's own key pays AionLabs directly.
        pricing_para = (
            "Pricing — two channels: `managed` bills through UnitySVC at "
            f"${_fmt_price(prompt)} / ${_fmt_price(completion)} per 1M input/output tokens, "
            "while `byok` is free through the UnitySVC gateway (your own AionLabs key "
            "pays AionLabs directly)."
        )
        description = f"{brief}\n\n{detail}\n\n{_HOWTO}\n\n{pricing_para}"

        yield {
            # Path / identity (stripped from the written parameters).
            "name": f"{PROVIDER_NAME}/{offering_name}",
            "provider_name": PROVIDER_NAME,
            # Offering fields
            "offering_name": offering_name,
            "display_name": display_name,
            "description": description,
            "service_type": "llm",
            "capabilities": ["llm"],
            "status": "ready",
            "details": details,
            "payout_price": {
                "type": "revenue_share",
                "percentage": "100.00",
                "description": "No platform commission",
            },
            "rate_limits": RATE_LIMITS,
            # Listing / channel fields
            "list_price": list_price,
            "provider_display_name": PROVIDER_DISPLAY_NAME,
            "api_base_url": API_BASE_URL,
            "env_api_key_name": ENV_API_KEY_NAME,
        }
        print("  OK")


def main() -> None:
    api_key = os.environ.get(ENV_API_KEY_NAME)
    if not api_key:
        print(f"Error: {ENV_API_KEY_NAME} not set")
        sys.exit(1)

    stats = write_params_from_iterator(
        iterator=iter_models(api_key),
        output_dir=SCRIPT_DIR.parent / "specs",
    )
    print(f"\nDone: {stats}")


if __name__ == "__main__":
    main()
