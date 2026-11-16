"""Offline breadth from explicitly scoped, dated security identities and price evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


RULE_VERSION = "DATA01_SH_SZ_A_COMPARABLE_CLOSE_V1"
LOCAL_SCOPE = "SH_SZ_A_ORDINARY"
VALUE_BASIS = "OFFICIAL_COMPARABLE_PREVIOUS_CLOSE"
ASIA_SHANGHAI = timezone(timedelta(hours=8))
PRICE_FIELDS = ("open", "high", "low", "close")
MONEY_TO_CNY = {"CNY": Decimal(1), "CNY_10000": Decimal(10000), "CNY_100M": Decimal(100000000)}
VOLUME_TO_SHARES = {"SHARES": Decimal(1), "10000_SHARES": Decimal(10000), "LOTS": Decimal(100), "10000_LOTS": Decimal(1000000)}
SAMPLE_VALIDATED_AT = "2026-11-13T21:26:26+08:00"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as data:
        return list(csv.DictReader(data))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def text_decimal(value):
    return format(value, "f") if value is not None else ""


def number(value, label, positive=False):
    if value in (None, "", "UNKNOWN"):
        return None
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}: {value}") from exc
    if not n.is_finite() or n < 0 or positive and n == 0:
        raise ValueError(f"invalid {label}: {value}")
    return n


def fraction(n, denominator):
    if not denominator:
        return ""
    with localcontext() as ctx:
        ctx.prec = 28
        return text_decimal(Decimal(n) / denominator)


def moment(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("a full timestamp needs an explicit zone")
    return parsed


def assert_identity(rows):
    """Reject repeated IDs and overlapping intervals for identical exchange/code/type."""
    ids = set()
    keys = defaultdict(list)
    for row in rows:
        if row["asset_id"] in ids:
            raise ValueError(f"duplicate asset identity {row['asset_id']}")
        ids.add(row["asset_id"])
        code, exchange, security_type = row["local_code"], row["exchange"], row["security_type"]
        if not re.fullmatch(r"\d{6}", code) or not exchange or not security_type:
            raise ValueError(f"invalid identity {row['asset_id']}")
        start = date.fromisoformat(row["listing_date"])
        end = date.fromisoformat(row["delisting_date_exclusive"]) if row["delisting_date_exclusive"] else date.max
        if start >= end:
            raise ValueError(f"invalid listing interval {row['asset_id']}")
        last = row.get("last_trade_date")
        if last and not start <= date.fromisoformat(last) < end:
            raise ValueError(f"invalid last trade {row['asset_id']}")
        keys[exchange, code, security_type].append((start, end))
    for key, spans in keys.items():
        spans.sort()
        if any(prev[1] > current[0] for prev, current in zip(spans, spans[1:])):
            raise ValueError(f"overlapping identity intervals for {key}")


def listed(row, day):
    return row["listing_date"] <= day and (not row["delisting_date_exclusive"] or day < row["delisting_date_exclusive"])


def in_scope(row):
    return row["exchange"] in {"SSE", "SZSE"} and row["security_type"] == "A_ORDINARY"


def normalize_price(raw, asset, sessions):
    """Preserve prices in their reported adjustment basis and quantities in original precision."""
    day = raw["trade_date"]
    if day not in sessions:
        raise ValueError(f"unverified session {day}")
    if raw["asset_id"] != asset["asset_id"] or not listed(asset, day):
        raise ValueError(f"out of listing interval {raw['asset_id']} {day}")
    if asset.get("last_trade_date") and asset["last_trade_date"] < day:
        raise ValueError(f"price after last trade {raw['asset_id']} {day}")
    as_of = moment(raw["market_as_of"]).astimezone(ASIA_SHANGHAI)
    observed = moment(raw["observed_at"])
    if as_of.date().isoformat() != day or observed < as_of:
        raise ValueError(f"inconsistent market/collection time {raw['asset_id']} {day}")
    published = raw["published_at"]
    if published and (moment(published) > observed if "T" in published else date.fromisoformat(published) > observed.astimezone(ASIA_SHANGHAI).date()):
        raise ValueError("publication after actual capture")
    opn, high, low, close = (number(raw[key], key, positive=True) for key in PRICE_FIELDS)
    if raw["observation_role"] == "PRICE_BAR" and (None in (opn, high, low, close) or not low <= min(opn, close) <= max(opn, close) <= high):
        raise ValueError(f"invalid OHLC {raw['asset_id']} {day}")
    volume = number(raw["volume_raw"], "volume")
    amount = number(raw["amount_raw"], "amount")
    vol_unit, amt_unit = raw["volume_unit"], raw["amount_unit"]
    return {
        **raw, "exchange": asset["exchange"], "local_code": asset["local_code"], "security_type": asset["security_type"],
        "volume_shares": text_decimal(volume * VOLUME_TO_SHARES[vol_unit]) if volume is not None and vol_unit in VOLUME_TO_SHARES else "",
        "amount_cny": text_decimal(amount * MONEY_TO_CNY[amt_unit]) if amount is not None and amt_unit in MONEY_TO_CNY else "",
        "unadjusted_close": raw["close"] if raw["price_basis"] == "NONE" else "",
        "comparable_preclose": "", "preclose_basis": "UNKNOWN", "trade_status": "PRICE_OBSERVED_STATUS_UNREVIEWED",
        "st_as_of": "UNKNOWN", "corporate_action_status": "UNKNOWN", "historical_reconstruction": True,
        "published_at_precision": "TIMESTAMP_AS_REPORTED" if "T" in published else "DATE" if published else "UNKNOWN",
        "availability_at": observed.isoformat(), "data_status": "PROVENANCE_PRESERVED_CROSSCHECK_ONLY" if raw["observation_role"] == "CLOSE_CHECK" else "PROVENANCE_PRESERVED",
    }


def direction(row, cutoff=None):
    """Require a provider-confirmed prior comparison basis and date-specific evidence."""
    if cutoff and (not row.get("availability_at") or moment(row["availability_at"]) > cutoff):
        return "NOT_AVAILABLE_AT_CUTOFF"
    if row["trade_status"] in {"SUSPENDED", "NO_TRADING", "LISTING_SUSPENDED"}:
        return row["trade_status"]
    if row["trade_status"] != "TRADED":
        return "TRADING_STATUS_UNVERIFIED"
    if row.get("price_basis") != "NONE":
        return "UNADJUSTED_BASIS_UNVERIFIED"
    if row.get("corporate_action_status") not in {"NONE_CONFIRMED", "EX_RIGHTS_CONFIRMED"}:
        return "CORPORATE_ACTION_UNVERIFIED"
    if row.get("preclose_basis") != VALUE_BASIS:
        return "COMPARABLE_PRECLOSE_UNVERIFIED"
    previous = number(row.get("comparable_preclose"), "comparable preclose", positive=True)
    close = number(row.get("close"), "close", positive=True)
    if previous is None or close is None:
        return "MISSING_COMPARABLE_PRICE"
    return "UP" if close > previous else "DOWN" if close < previous else "FLAT"


def reconstruct(day, assets, prices, lifecycle, sessions, *, scope, universe_complete, cutoff=None):
    """Calculate scope-specific direction counts; no market ratio for an uncertified pool."""
    if day not in sessions:
        raise ValueError(f"unverified session {day}")
    if scope != LOCAL_SCOPE:
        raise ValueError("local reconstruction only supports Shanghai/Shenzhen A shares")
    assert_identity(assets)
    cutoff = moment(cutoff) if cutoff else None
    per_asset = defaultdict(list)
    for raw in prices:
        if raw["trade_date"] == day and raw["observation_role"] == "PRICE_BAR" and (
                cutoff is None or raw.get("availability_at") and moment(raw["availability_at"]) <= cutoff):
            per_asset[raw["asset_id"]].append(raw)
    active = [a for a in assets if in_scope(a) and listed(a, day) and
              (cutoff is None or a.get("availability_at") and moment(a["availability_at"]) <= cutoff)]
    records = []
    for a in active:
        security = a["asset_id"]
        rows = per_asset.get(security, [])
        suspension = any(e["subject"] == security and e["relation"] == "LISTING_SUSPENDED" and e["valid_from"] <= day and
                         (not e["valid_to_exclusive"] or day < e["valid_to_exclusive"]) and
                         (cutoff is None or e.get("observed_at") and moment(e["observed_at"]) <= cutoff) for e in lifecycle)
        if suspension:
            status = "SOURCE_CONFLICT" if rows else "LISTING_SUSPENDED"
        elif a["listing_date"] == day:
            status = "IPO_FIRST_DAY_EXCLUDED"
        elif a.get("last_trade_date") and a["last_trade_date"] < day:
            status = "AFTER_LAST_TRADE_BEFORE_DELISTING"
        elif not rows:
            status = "PRICE_MISSING"
        else:
            flags = [direction(row, cutoff) for row in rows]
            pairs = {(number(r.get("close"), "close"), number(r.get("comparable_preclose"), "comparable preclose"))
                     for r, flag in zip(rows, flags) if flag in {"UP", "DOWN", "FLAT"}}
            if len(pairs) > 1 or pairs and any(flag in {"SUSPENDED", "LISTING_SUSPENDED", "NO_TRADING"} for flag in flags):
                status = "SOURCE_CONFLICT"
            else:
                status = next((flag for flag in flags if flag in {"UP", "DOWN", "FLAT"}), flags[0])
        records.append({"trade_date": day, "asset_id": security, "exchange": a["exchange"], "board": a["board"],
                        "in_tested_pool": True, "historical_st_status": "UNKNOWN",
                        "listing_start": a["listing_date"], "delisting_date_exclusive": a["delisting_date_exclusive"],
                        "last_trade_date": a.get("last_trade_date", ""), "status": status,
                        "price_observations": len(rows), "identity_source": a["source_url"]})
    counts = Counter(row["status"] for row in records)
    comparable = counts["UP"] + counts["DOWN"] + counts["FLAT"]
    excluded = sum(counts[s] for s in ("SUSPENDED", "NO_TRADING", "LISTING_SUSPENDED", "IPO_FIRST_DAY_EXCLUDED", "AFTER_LAST_TRADE_BEFORE_DELISTING"))
    missing = len(active) - comparable - excluded
    full = universe_complete and cutoff is None  # full historical snapshot declaration only; never upcast retrospective timestamps
    output = {"trade_date": day, "rule_version": RULE_VERSION, "market_scope": scope, "universe_complete": full,
              "theoretical_whole_market_pool": len(active) if full else "", "tested_subset_pool": len(active),
              "comparable_count": comparable, "observed_excluded_count": excluded, "missing_or_unverified_count": missing,
              "subset_comparable_coverage": fraction(comparable, len(active)), "subset_missing_or_unverified_ratio": fraction(missing, len(active)),
              "up": counts["UP"], "down": counts["DOWN"], "flat": counts["FLAT"],
              "subset_up_ratio": fraction(counts["UP"], comparable),
              "local_market_up_ratio": fraction(counts["UP"], comparable) if full and not missing else "",
              "quality": "INCOMPLETE_UNIVERSE" if not full else "NO_COMPARABLE" if not comparable else "INCOMPLETE_DATA" if missing else "READY_FOR_LOCAL_RESEARCH",
              "s1_val_01_compatible": False, "status_counts": json.dumps(counts, sort_keys=True, separators=(",", ":"))}
    return output, records


def asset_rows(sample):
    first = {
        row["subject"]: row for row in read_csv(sample / "identity_events.csv") if row["relation"] == "INITIAL_LISTING"
    }
    rows = []
    for record in read_csv(sample / "pilot_assets.csv"):
        entry = first.get(record["asset_id"], {})
        rows.append({"asset_id": record["asset_id"], "exchange": record["exchange"], "local_code": record["code"],
                     "canonical_code": f"{record['exchange']}:{record['code']}", "security_type": "A_ORDINARY",
                     "listing_date": record["listed_from"], "delisting_date_exclusive": record["listed_to_exclusive"],
                     "last_trade_date": record["last_trade_date"], "current_company_name": record["company_name"],
                     "name_valid_from": "", "name_valid_to_exclusive": "", "board": record["board"],
                     "board_valid_from": "", "board_valid_to_exclusive": "", "status_in_archived_identity": record["current_status"],
                     "identity_evidence_date": record["published_date"], "identity_publication_precision": "DATE" if record["published_date"] else "UNKNOWN",
                     "source_url": record["source_url"], "initial_listing_source": entry.get("source_url", record["source_url"]),
                     "source_class": record["source_class"], "availability_at": SAMPLE_VALIDATED_AT,
                     "availability_basis": "LATEST_PILOT_ARCHIVE_REPLAY_CEILING",
                     "verification_status": "SAMPLE01_SOURCE_SUPPORTED_SUBSET", "limitations": record["scope_note"]})
    assert_identity(rows)
    return rows


def vendor_rows(path):
    output = []
    seen = set()
    for row in read_csv(path):
        key = row["date"], row["source"]
        if key in seen:
            raise ValueError(f"duplicate provider/date {key}")
        seen.add(key)
        counts = {k: number(row[k], k) for k in ("up", "down", "flat")}
        if any(v is None or v != v.to_integral_value() for v in counts.values()):
            raise ValueError(f"invalid reported counts {key}")
        total = sum(counts.values())
        if total != number(row["denominator"], "denominator", positive=True):
            raise ValueError(f"denominator mismatch {key}")
        output.append({**row, "source_scope": row["market_scope"], "reported_up_ratio": row["up_ratio"],
                       "recomputed_report_up_ratio": fraction(counts["up"], total), "measurement": "PROVIDER_REPORTED_COUNTS",
                       "historic_stock_pool_rebuilt": False})
    return output


def breadth_comparison(local, vendor):
    by_day = defaultdict(list)
    for row in vendor:
        by_day[row["date"]].append(row)
    out = []
    for row in local:
        refs = by_day[row["trade_date"]]
        for ref in refs or [{}]:
            out.append({"trade_date": row["trade_date"], "local_scope": row["market_scope"], "local_tested_subset": row["tested_subset_pool"],
                        "local_comparable": row["comparable_count"], "local_market_ratio": row["local_market_up_ratio"],
                        "vendor_source": ref.get("source", ""), "vendor_scope": ref.get("source_scope", ""),
                        "vendor_up": ref.get("up", ""), "vendor_denominator": ref.get("denominator", ""), "vendor_up_ratio": ref.get("recomputed_report_up_ratio", ""),
                        "vendor_evidence_url": ref.get("source_url", ""), "local_minus_vendor_pp": "",
                        "status": "NONCOMPARABLE_PARTIAL_POOL_AND_PROVIDER_FILTERS" if ref else "NO_FROZEN_VENDOR_OBSERVATION",
                        "s1_val_01_compatible": False})
    return out


def price_checks(prices):
    by_day = defaultdict(list)
    for row in prices:
        by_day[row["asset_id"], row["trade_date"]].append(row)
    output = []
    for (asset, day), rows in sorted(by_day.items()):
        if len(rows) < 2:
            continue
        first, second = rows
        price_diffs = {k: number(first[k], k) - number(second[k], k) for k in PRICE_FIELDS if first[k] and second[k]}
        first_ohlc = all(first[k] for k in PRICE_FIELDS)
        second_ohlc = all(second[k] for k in PRICE_FIELDS)
        equivalent = first["price_basis"] == second["price_basis"] == "NONE"
        issues = []
        if any(price_diffs.values()):
            issues.append("PRICE_CONFLICT")
        if not equivalent:
            issues.append("PRECISE_PRICE_BASIS_EQUIVALENCE_UNVERIFIED")
        if not (first_ohlc and second_ohlc):
            issues.append("CLOSE_ONLY_CROSSCHECK")
        units_a = number(first["amount_cny"], "amount cny")
        units_b = number(second["amount_cny"], "amount cny")
        amount_difference = units_a - units_b if units_a is not None and units_b is not None else None
        if amount_difference:
            issues.append("AMOUNT_PRECISION_DIFFERS_UNRESOLVED")
        output.append({"asset_id": asset, "trade_date": day, "source_url_1": first["source_url"], "source_url_2": second["source_url"],
                        "price_basis_1": first["price_basis"], "price_basis_2": second["price_basis"],
                        "ohlc_fields_compared": "/".join(price_diffs), "price_diff_close": text_decimal(price_diffs["close"]),
                        "amount_cny_1": first["amount_cny"], "amount_cny_2": second["amount_cny"],
                        "amount_1_minus_2_cny": text_decimal(amount_difference),
                        "status": ";".join(issues) if issues else "DISPLAYED_OHLC_AGREES_INDEPENDENCE_UNKNOWN"})
    return output


def build(sample, vendor_file, out):
    out.mkdir(parents=True, exist_ok=True)
    master = asset_rows(sample)
    ids = {a["asset_id"]: a for a in master}
    lifecycle = read_csv(sample / "identity_events.csv")
    raw = read_csv(sample / "pilot_prices_raw.csv")
    sessions = {r["trade_date"] for r in raw if r["observation_role"] == "PRICE_BAR" and date.fromisoformat(r["trade_date"]).weekday() < 5}
    prices = [normalize_price(r, ids[r["asset_id"]], sessions) for r in raw]
    counts = Counter((r["asset_id"], r["trade_date"], r["source_url"], r["observation_role"]) for r in prices)
    if any(n != 1 for n in counts.values()):
        raise ValueError("duplicated archived price key")
    local, universe = [], []
    for day in sorted(sessions):
        result, securities = reconstruct(day, master, prices, lifecycle, sessions, scope=LOCAL_SCOPE, universe_complete=False)
        local.append(result)
        universe.extend(securities)
    vendor = vendor_rows(vendor_file)
    tables = {
        "DATA01_ASSET_MASTER.csv": master,
        "DATA01_ASSET_LIFECYCLE.csv": lifecycle,
        "DATA01_HISTORICAL_UNIVERSE.csv": universe,
        "DATA01_EQUITY_DAILY.csv": prices,
        "DATA01_EQUITY_SOURCE_COMPARISON.csv": price_checks(prices),
        "DATA01_BREADTH_VENDOR_BASELINE.csv": vendor,
        "DATA01_BREADTH_RECONSTRUCTED.csv": local,
        "DATA01_BREADTH_COMPARISON.csv": breadth_comparison(local, vendor),
    }
    for file, rows in tables.items():
        write_csv(out / file, rows)
    sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (sample / "pilot_assets.csv", sample / "identity_events.csv", sample / "pilot_prices_raw.csv", vendor_file, Path(__file__))}
    result = {"rule_version": RULE_VERSION, "input_sha256": sources, "table_row_counts": {f: len(r) for f, r in tables.items()},
              "pilot_sessions_evidence": "DATE_LABELLED_ARCHIVED_OHLC_AND_WEEKDAY_CHECK_NOT_WHOLE_MARKET_CALENDAR",
              "full_universe": False, "validated_direction_count": sum(r["comparable_count"] for r in local), "s1_val_01_eligible_rows": 0}
    (out / "DATA01_BREADTH_REBUILD_REPORT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["table_row_counts"], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--vendor", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    build(args.sample.resolve(strict=True), args.vendor.resolve(strict=True), args.out.resolve())


if __name__ == "__main__":
    main()
