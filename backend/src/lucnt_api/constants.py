"""Request-input bounds shared by the schemas.

Column mirrors must track column widths (else a 500 `DataError`, not a 422);
payload-policy caps bound TEXT columns against storage/telemetry bloat.
"""

from __future__ import annotations

from typing import Final

# --- Column mirrors -------------------------------------------------------

AGENT_SLUG_MAX: Final = 100  # agents.slug — String(100)
ENTITY_CODE_MAX: Final = 50  # entities.code — String(50)
VENDOR_MAX: Final = 255  # transactions.vendor / authorizations.vendor
CURRENCY_LEN: Final = 3  # *.currency — String(3), ISO-4217 alpha
INTENT_MAX: Final = 255  # transactions.intent
PROJECT_MAX: Final = 255  # transactions.project
CATEGORY_MAX: Final = 50  # transactions.category
TAX_CATEGORY_MAX: Final = 20  # transactions.tax_category
MODEL_NAME_MAX: Final = 100  # transactions.model_used
NAME_MAX: Final = 255  # vendors.name and friends
ACCOUNT_CODE_MAX: Final = 20  # chart-of-accounts code

# --- Payload policy (TEXT columns — no DB ceiling to mirror) ---------------

# `reason` is echoed into `agent.spend.created`.
REASON_MAX: Final = 2_000

NOTES_MAX: Final = 2_000

URL_MAX: Final = 2_048

# --- Collection bounds ----------------------------------------------------

# Scanned linearly on every authorize(); bounds money-path latency.
MERCHANT_LIST_MAX: Final = 1_000

# Outer size guards on a policy `config` blob; the engine validates its shape.
POLICY_CONFIG_MAX_KEYS: Final = 50
POLICY_CONFIG_VALUE_MAX: Final = 500

JOURNAL_LINES_MAX: Final = 500
