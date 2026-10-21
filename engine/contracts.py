"""Typed input contracts for the engine's tools (Pydantic v2).

The tools were written against plain dictionaries, which keeps them easy to call from anywhere. These
models sit at the edges: every CLI validates its input file with them first, so a typo in a field
name or a rate written as 12 instead of 0.12 fails at once with the field named, instead of
producing a quietly wrong verdict. The JSON Schemas generated from these models are in schemas/
(regenerate with python3 engine/contracts.py --write-schemas), for anyone producing the inputs from
other systems.

    from engine.contracts import validate_deliverability
    doc = validate_deliverability(json.loads(text))      # raises InputError listing every problem
"""
from __future__ import annotations

if __package__ in (None, ""):  # run as a file (python3 engine/contracts.py): make the repository importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[1]
Rate = Field(None, ge=0, le=1, description="a share between 0 and 1 (0.003 is 0.3%)")


class InputError(ValueError):
    """An input file that does not match its contract; the message lists every field that is wrong."""


def _raise(e: ValidationError, what: str) -> None:
    problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()[:12])
    raise InputError(f"{what}: {problems}") from None


# ----------------------------------------------------------------------------------------------
# deliverability (engine/deliverability/analyze.py analyze)
# ----------------------------------------------------------------------------------------------

class DnsRecords(BaseModel):
    """What is published for one domain. Leave a key out when it was not looked up; an empty list means looked up, none found."""
    model_config = ConfigDict(extra="forbid")
    txt: Optional[List[str]] = Field(None, description="TXT records at the domain itself (SPF lives here)")
    dmarc: Optional[Union[str, List[str]]] = Field(None, description="the TXT record at _dmarc.<domain>; a list when more than one is published (an error)")
    mx: Optional[List[str]] = None
    mta_sts: Optional[Union[str, List[str]]] = Field(None, description="the TXT record at _mta-sts.<domain>")
    tlsrpt: Optional[Union[str, List[str]]] = Field(None, description="the TXT record at _smtp._tls.<domain>")
    bimi: Optional[Union[str, List[str]]] = Field(None, description="the TXT record at default._bimi.<domain>")


class HeadersRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class Signals(BaseModel):
    """Reputation and engagement signals for the sending gate. Every rate is a share between 0 and 1."""
    model_config = ConfigDict(extra="forbid")
    complaint_rate: Optional[float] = Rate
    hard_bounce_rate: Optional[float] = Rate
    domain_reputation_bounces_90d: Optional[int] = Field(None, ge=0)
    domain_reputation_bounces_trend: Optional[Literal["rising", "flat", "falling"]] = None
    filtered_share: Optional[float] = Rate
    postmaster_spam_rate: Optional[float] = Rate
    postmaster_compliance: Optional[Literal["pass", "fail"]] = None
    postmaster_domain_reputation: Optional[Literal["high", "medium", "low", "bad"]] = None
    postmaster_reputation_trend: Optional[str] = None
    consumer_engagement_rate: Optional[float] = Rate
    enterprise_engagement_rate: Optional[float] = Rate
    unengaged_share: Optional[float] = Rate
    auth_state: Optional[Literal["verified", "partial", "failing", "unknown"]] = None


class Volume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_to_gmail: Optional[int] = Field(None, ge=0)
    daily_to_microsoft_consumer: Optional[int] = Field(None, ge=0)
    daily_total: Optional[int] = Field(None, ge=0)


class DeliverabilityInput(BaseModel):
    """Everything engine/deliverability/analyze.py analyze reads. observed_at marks the records as looked up (LIVE PULL) on that date."""
    model_config = ConfigDict(extra="forbid")
    case: Optional[str] = Field(None, description="a note about what this input is")
    observed_at: Optional[date] = None
    message_class: Optional[Literal["marketing", "transactional", "mixed"]] = None
    from_domain: Optional[str] = None
    envelope_domain: Optional[str] = None
    headers: Optional[Union[str, HeadersRef]] = Field(None, description="raw headers of one received message, or {path: file}")
    dns: Dict[str, DnsRecords] = Field(default_factory=dict)
    spf_includes: Dict[str, str] = Field(default_factory=dict, description="SPF records of included domains, for lookup counting")
    dkim_keys: Dict[str, str] = Field(default_factory=dict, description="selector._domainkey.<domain> to its TXT record")
    mta_sts_policies: Dict[str, str] = Field(default_factory=dict, description="domain to the text of https://mta-sts.<domain>/.well-known/mta-sts.txt")
    signals: Signals = Field(default_factory=Signals)
    volume: Volume = Field(default_factory=Volume)

    @field_validator("dns", "spf_includes", "dkim_keys", "mta_sts_policies")
    @classmethod
    def _lower_hosts(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        return {k.lower().rstrip("."): x for k, x in v.items()}


def validate_deliverability(doc: Dict[str, Any]) -> Dict[str, Any]:
    try:
        m = DeliverabilityInput.model_validate(doc)
    except ValidationError as e:
        _raise(e, "deliverability input")
    out = m.model_dump(mode="json", exclude_none=True)
    out["dns"] = {k: v for k, v in out["dns"].items()}
    return out


# ----------------------------------------------------------------------------------------------
# readout (engine/measurement/stats.py incrementality)
# ----------------------------------------------------------------------------------------------

class Arm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(gt=0, description="entrants in the arm")
    conversions: int = Field(ge=0)
    revenue: Optional[float] = Field(None, ge=0, description="total revenue in the outcome window")
    revenue_sd: Optional[float] = Field(None, ge=0, description="per-entrant revenue standard deviation, zeros included")
    largest_transaction: Optional[float] = Field(None, ge=0)

    @field_validator("conversions")
    @classmethod
    def _not_more_than_n(cls, v: int, info: Any) -> int:
        n = info.data.get("n")
        if n is not None and v > n:
            raise ValueError(f"{v} conversions from {n} entrants: count converting entrants, not orders")
        return v


class ReadoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow: str
    period: str
    design: Literal["persistent_holdout", "holdout", "pre_post"] = "persistent_holdout"
    planned_holdout_share: Optional[float] = Field(None, gt=0, lt=1)
    treatment: Arm
    holdout: Arm
    attributed_revenue: Optional[float] = Field(None, ge=0)
    attribution_model: Optional[str] = None
    window_days: Optional[int] = Field(None, gt=0)
    window_reason: Optional[str] = None
    inter_purchase_days_p50: Optional[float] = Field(None, gt=0)
    alpha: float = Field(0.05, gt=0, lt=0.5)
    caveats: List[str] = Field(default_factory=list)


def validate_readout(doc: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return ReadoutInput.model_validate(doc).model_dump(mode="json", exclude_none=True)
    except ValidationError as e:
        _raise(e, "readout input")
    return {}


# ----------------------------------------------------------------------------------------------
# warm-up (engine/deliverability/warmup.py)
# ----------------------------------------------------------------------------------------------

class WarmupTiers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engaged_30d: int = Field(0, ge=0)
    engaged_90d: int = Field(0, ge=0)
    engaged_180d: int = Field(0, ge=0)
    unengaged: int = Field(0, ge=0)


def validate_tiers(doc: Dict[str, Any]) -> Dict[str, int]:
    try:
        return WarmupTiers.model_validate(doc).model_dump()
    except ValidationError as e:
        _raise(e, "warm-up tiers")
    return {}


SCHEMAS = {"deliverability_input.schema.json": DeliverabilityInput, "readout_input.schema.json": ReadoutInput,
           "warmup_tiers.schema.json": WarmupTiers}


def json_schema(model: type) -> Dict[str, Any]:
    s = model.model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://github.com/kyle-olmstead/lifecycle-marketing-claude-engine/schemas/{[k for k, v in SCHEMAS.items() if v is model][0]}",
            **s}


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tool input contracts")
    ap.add_argument("--write-schemas", action="store_true", help="regenerate the JSON Schemas in schemas/")
    a = ap.parse_args(argv)
    if a.write_schemas:
        for name, model in SCHEMAS.items():
            (ROOT / "schemas" / name).write_text(json.dumps(json_schema(model), indent=2) + "\n", encoding="utf-8")
            print(f"wrote schemas/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
