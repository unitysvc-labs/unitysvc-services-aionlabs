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
from decimal import Decimal, ROUND_HALF_UP
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

# What the platform adds on top of the upstream rate for the MANAGED channel,
# where UnitySVC's key pays AionLabs and the customer pays UnitySVC. The byok
# channel is unaffected — the customer's key pays AionLabs directly, so there is
# nothing to mark up and nothing to pay out.
#
# The seller's own list price, computed here at populate time rather than by the
# platform, so the stored rate is exactly the rate billed.
PLATFORM_MARKUP = Decimal("1.15")

# 3dp: measured across this catalog's rates it keeps the effective markup inside
# 15.0-15.7%, where 2dp would swing a cheap model as wide as 25%. `_fmt_price`
# drops trailing zeros, so a rate that needs no third decimal never shows one.
PRICE_PLACES = Decimal("0.001")

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
    # An empty enumeration is an upstream/credential failure, not an emptied
    # catalog. Absence now RETIRES a service (``deprecate_missing``), so
    # returning quietly here would either deprecate everything or write nothing
    # at all — indistinguishable from "no changes today". Fail the run instead.
    if not models:
        print(f"Error: {PROVIDER_DISPLAY_NAME} returned no models")
        sys.exit(1)

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
        # AionLabs quotes per TOKEN, so the x1e6 is the unit conversion, not a
        # markup. Keep it in Decimal from here on: the float path produced rates
        # like 0.09999999999999999 in a sibling catalog.
        up_in = (Decimal(str(pricing.get("prompt", 0) or 0)) * 1_000_000)
        up_out = (Decimal(str(pricing.get("completion", 0) or 0)) * 1_000_000)
        mk_in = (up_in * PLATFORM_MARKUP).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)
        mk_out = (up_out * PLATFORM_MARKUP).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)

        # What the CUSTOMER pays on the managed channel: upstream + markup.
        list_price = {
            "type": "one_million_tokens",
            "input": _fmt_price(mk_in),
            "output": _fmt_price(mk_out),
            "description": f"${_fmt_price(mk_in)}/${_fmt_price(mk_out)} / 1M input/output tokens",
        }
        # What the PLATFORM owes the SELLER on the managed channel: the upstream
        # rate, unmarked. Absolute rather than a share of the list price, so it
        # does not follow the markup, a listing override, or a promotion — none
        # of which change what AionLabs billed (unitysvc/unitysvc#1892).
        upstream_price = {
            "type": "one_million_tokens",
            "input": _fmt_price(up_in),
            "output": _fmt_price(up_out),
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
            f"${_fmt_price(mk_in)} / ${_fmt_price(mk_out)} per 1M input/output tokens, "
            "while `byok` is free through the UnitySVC gateway (your own AionLabs key "
            "pays AionLabs directly)."
        )
        description = f"{brief}\n\n{detail}\n\n{_HOWTO}\n\n{pricing_para}"

        yield {
            # Path / identity (stripped from the written parameters).
            "service_name": f"{PROVIDER_NAME}/{offering_name}",
            "provider_name": PROVIDER_NAME,
            # Offering fields
            "offering_name": offering_name,
            "display_name": display_name,
            "description": description,
            "service_type": "llm",
            "capabilities": ["chat"],
            "status": "ready",
            "details": details,
            # Channel-keyed: the two channels owe the seller different amounts.
            # A flat 100% revenue_share paid out everything the customer paid —
            # which, now that the list price carries a markup, would hand the
            # seller the markup too.
            #
            # NOTE: `calculate_seller_payout` does not thread the resolved
            # channel into `calculate_cost`, so this resolves to `default` for
            # every request (#1892 Phase 1). Correct in THIS shape only because
            # `default` is the paid channel and byok rows are dropped earlier by
            # the `seller_charge == 0` guard.
            "payout_price": {
                "type": "channel",
                "default": "managed",
                "channels": {
                    "byok": {
                        "type": "constant",
                        "price": "0",
                        "description": f"No payout - the customer's own key pays {PROVIDER_DISPLAY_NAME}",
                    },
                    "managed": dict(
                        upstream_price,
                        description=f"Upstream {PROVIDER_DISPLAY_NAME} rate",
                    ),
                },
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

    # ``deprecate_missing`` defaults to True and is left on: this script
    # enumerates the WHOLE upstream catalog on every run (no --limit), and the
    # only skips are deterministic reads of the catalog itself, not failures —
    # a blank id, and an ``expires_at`` model, which AionLabs has itself marked
    # for retirement and which SHOULD therefore be deprecated here if it is
    # already committed. Every path that could shorten the list by accident
    # (a non-2xx from /v1/models, an empty catalog) exits non-zero instead.
    stats = write_params_from_iterator(
        iterator=iter_models(api_key),
        output_dir=SCRIPT_DIR.parent / "specs",
    )
    print(f"\nDone: {stats}")
    print(f"New: {stats['new']}, deprecated: {stats['deprecated']}")


if __name__ == "__main__":
    main()
