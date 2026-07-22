"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    environment: str = "development"
    # Max ratio a capture may settle over its authorized amount (T0-3). The
    # policy verdict + budget reservation were computed on the authorized figure;
    # a true cost beyond this tolerance must be re-authorized, not silently
    # settled. 1.25 = allow up to 125% of the estimate.
    settlement_buffer: Decimal = Decimal("1.25")
    # India s.195 withholding on payments to non-residents (TE-3). PROVISIONAL —
    # confirm with a CA before a pilot: the DTAA treaty rate (with a valid TRC +
    # Form 10F) vs the domestic default (incl. surcharge/cess), and the ₹5-lakh
    # Form-15CB threshold above which a CA certificate is required. Rates move with
    # notifications and treaties; keep them config, not code.
    withholding_treaty_rate: Decimal = Decimal("0.15")
    withholding_domestic_rate: Decimal = Decimal("0.20")
    withholding_15cb_threshold: Decimal = Decimal("500000")


# pydantic-settings fills required fields from the environment at runtime.
settings = Settings()  # type: ignore[call-arg]
