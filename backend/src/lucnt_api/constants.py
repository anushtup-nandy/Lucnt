"""Request-input bounds shared by the schemas.

Two kinds of constant live here, and the distinction matters:

* **Column mirrors** — the bound equals the width of the column the field lands
  in. Without it the schema is *looser* than the column, so oversized input
  sails through validation and fails in Postgres as a 500 `DataError` instead of
  a 422 naming the offending field. When a column widens, this is the one place
  to follow it.
* **Payload policy** — the target column is `TEXT`, which Postgres will happily
  fill to 1GB. Nothing downstream breaks, so the cap exists to stop a single
  request inflating storage, log lines and event telemetry. These are judgement
  calls, marked as such below.
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

# A spend rationale is a sentence, not a document. Generous enough that no honest
# caller trips it; small enough that it cannot bloat the event stream, where
# `reason` is echoed into `agent.spend.created`.
REASON_MAX: Final = 2_000

# Free-text an operator types when resolving a recon exception or annotating a
# journal. Same reasoning as REASON_MAX.
NOTES_MAX: Final = 2_000

# URLs are bounded by de-facto browser/proxy limits, not by us.
URL_MAX: Final = 2_048

# --- Collection bounds ----------------------------------------------------

# A merchant allow/block list is hand-authored configuration. The policy engine
# scans it linearly per authorize(), so an unbounded list is both a payload and a
# latency problem on the blocking money path.
MERCHANT_LIST_MAX: Final = 1_000

# Keys permitted in a policy `config` blob, and the size of one. `config` is
# validated per rule type by the engine's typed models; these are the outer
# guards that stop an arbitrarily large JSON object being stored at all.
POLICY_CONFIG_MAX_KEYS: Final = 50
POLICY_CONFIG_VALUE_MAX: Final = 500

# Lines in one manual journal / funding request.
JOURNAL_LINES_MAX: Final = 500
