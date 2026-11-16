"""Offline intake of reviewed public closing evidence. Standard library only."""
import argparse
import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
PANEL = "THEPAPER_WIND_SH_SZ_BJ__SH_SZ_CNY"
ASSET = "SSE:000300"
VAL_FILES = {
    "protocol.json": "849b6c12abd84fb69d0f3392adb54c58eddfcafb6a1a764dbd26d1e52f2a9c2b",
    "S1_VAL_01_PREREGISTRATION.md": "68f6018faceba125f83f65857bffd96ef3e3b92b22482bd09a19e1b47944bb78",
}
PAPER = {"www.thepaper.cn", "m.thepaper.cn", "thepaper.cn"}
INDEX_HOSTS = {"news.10jqka.com.cn", "cn.investing.com", "closelook.net", "www.sse.com.cn", "www.csindex.com.cn"}
FAILURES = {"SOURCE_ACCESS_BLOCKED", "TIMEOUT", "EMPTY_RESPONSE", "STALE_DATE", "PARSING_FAILURE",
            "UNIT_CHANGE", "SOURCE_CONFLICT", "MISSED_CAPTURE", "MISSING_OUTCOME"}


class Rejected(ValueError):
    pass


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(path, data):
    """Publish complete bytes atomically, reusing only byte-identical content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise Rejected(f"Immutable key collision: {path.name}")
        return
    temporary = path.parent / f".pending-{uuid4().hex}"
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise Rejected(f"Immutable key collision: {path.name}")
    finally:
        temporary.unlink()


def aware(value):
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise Rejected("Timestamp must include a time zone")
    return dt.astimezone(CST)


def close_time(day):
    return datetime.combine(day, time(15), CST)


def from_calendar(day, calendar):
    year = str(day.year)
    config = calendar["historical"].get(year)
    if config is None:
        return "CALENDAR_UNVERIFIED"
    if day.weekday() >= 5 or any(lo <= day.isoformat()[5:] <= hi for lo, hi in config["closed"]):
        return "NON_SESSION"
    return "SESSION"


def validate_lock(val):
    lock = load(val / "protocol_lock.json")
    if lock["frozen_at_cst"] != "2026-11-13T13:51:37.0063273+08:00" or lock["sha256"] != VAL_FILES:
        raise Rejected("Unexpected S1-VAL-01 lock")
    for name, digest in VAL_FILES.items():
        if sha((val / name).read_bytes()) != digest:
            raise Rejected(f"Frozen protocol changed: {name}")
    return load(val / "protocol.json")


def formal_calendar(val):
    spec = validate_lock(val)
    config = val / "formal_sessions.json"
    if not config.is_file():
        return set()
    data = load(config)
    link = urlsplit(data["official_calendar_url"])
    if link.scheme != "https" or link.hostname != "www.sse.com.cn" or not link.path.startswith("/disclosure/"):
        raise Rejected("Formal calendar requires a reviewed SSE notice")
    sessions = list(map(date.fromisoformat, data["sessions_with_adjacent_anchors"]))
    first, last = map(date.fromisoformat, spec["formal_prospective_dates"])
    if not sessions or sessions != sorted(set(sessions)) or not sessions[0] < first <= last < sessions[-1]:
        raise Rejected("Invalid calendar, including surrounding anchors")
    if any(day.weekday() >= 5 for day in sessions):
        raise Rejected("Unexpected weekend session")
    return {day for day in sessions if first <= day <= last}


def positive(value):
    v = Decimal(str(value))
    if not v.is_finite() or v <= 0:
        raise Rejected("Positive finite number required")
    return v


def source_block(block, day, now, hosts, phase):
    source = urlsplit(block["url"])
    if source.scheme != "https" or source.hostname not in hosts or source.username or source.password:
        raise Rejected("Source outside the approved document list")
    if phase == "market" and not source.path.startswith("/newsDetail_forward_"):
        raise Rejected("Use a dated The Paper report")
    collected = aware(block["collected_at"])
    if not close_time(day) <= collected <= now:
        raise Rejected("Capture outside market close/current time")
    published = block.get("published_at")
    precision = block.get("published_precision", "UNKNOWN")
    if published:
        if precision not in {"MINUTE", "SECOND"} or not close_time(day) <= aware(published) <= collected:
            raise Rejected("Invalid publication chronology or precision")
    elif precision != "UNKNOWN":
        raise Rejected("Publication precision must be UNKNOWN without a timestamp")
    if not block.get("locator"):
        raise Rejected("Human-verifiable source locator required")
    return collected


def normalized(packet, day, now):
    if packet["trade_date"] != str(day) or packet["market_as_of"] != close_time(day).isoformat():
        raise Rejected("Stale trade date or non-close market timestamp")
    if packet["asset"] != ASSET or packet["panel"] != PANEL:
        raise Rejected("Incompatible asset or panel")
    market, index = packet["market"], packet["index"]
    if market["breadth_scope"] != "WIND_SH_SZ_BJ" or market["turnover_scope"] != "SH_SZ_STOCKS_REPORTED":
        raise Rejected("Incompatible breadth or turnover scope")
    if market["turnover_unit"] not in {"CNY", "CNY_100M", "CNY_1TN"}:
        raise Rejected("Unrecognized monetary unit")
    scale = {"CNY": 1, "CNY_100M": 100000000, "CNY_1TN": 1000000000000}[market["turnover_unit"]]
    count = [market[key] for key in ("up", "down", "flat")]
    if any(type(x) is not int or x < 0 for x in count) or not sum(count):
        raise Rejected("Invalid breadth denominator")
    close = positive(index["close"])
    opening = positive(index["open"]) if index.get("open") is not None else None
    low = positive(index["low"]) if index.get("low") is not None else None
    high = positive(index["high"]) if index.get("high") is not None else None
    if (low is None) != (high is None) or (low is not None and (
        opening is None or not low <= min(opening, close) <= max(opening, close) <= high
    )):
        raise Rejected("Incomplete or invalid OHLC range")
    dates = [source_block(market, day, now, PAPER, "market"), source_block(index, day, now, INDEX_HOSTS, "index")]
    latest = max(dates)
    deadline = datetime.combine(day, time(20), CST)
    if opening is not None and index.get("open_url"):
        url = urlsplit(index["open_url"])
        if url.scheme != "https" or url.hostname not in INDEX_HOSTS:
            raise Rejected("Open price source outside the approved document list")
    return {
        "trade_date": str(day), "asset": ASSET, "panel": PANEL, "up": count[0],
        "down": count[1], "flat": count[2], "breadth_n": sum(count),
        "breadth": str(Decimal(count[0]) / sum(count)), "turnover_cny": str(positive(market["turnover"]) * scale),
        "raw_turnover": str(market["turnover"]), "raw_turnover_unit": market["turnover_unit"],
        "open": str(opening) if opening is not None else None, "high": str(high) if high is not None else None,
        "low": str(low) if low is not None else None, "close": str(close),
        "market_url": market["url"], "market_published_at": market.get("published_at"),
        "market_published_precision": market["published_precision"], "market_collected_at": dates[0].isoformat(),
        "index_url": index["url"], "index_published_at": index.get("published_at"),
        "index_published_precision": index["published_precision"], "index_collected_at": dates[1].isoformat(),
        "first_observed_at": latest.isoformat(), "market_as_of": packet["market_as_of"],
        "within_original_cutoff": latest <= deadline, "universe_detail": "UNKNOWN", "breadth_path": "VENDOR_REPORTED",
    }


def states(root, day):
    path = root / "data/validated" / str(day)
    return [(p, load(p)) for p in sorted(path.glob("*.json"))]


def log(root, day, now, status, **fields):
    p = root / "data/runs" / f"{day}-{uuid4().hex}.json"
    put(p, json_bytes({"trade_date": str(day), "run_at": now.isoformat(), "status": status, **fields}))
    return p


def record_failure(root, day, now, reason, source, query, detail):
    if reason not in FAILURES:
        raise Rejected("Unknown failure category")
    return log(root, day, now, "FAILED", error_type=reason, source=source, query=query, detail=detail)


def intake(root, day, now, calendar, path, mode, prior_failure=None, val=None):
    if mode == "FORMAL":
        if val is None or day not in formal_calendar(val):
            log(root, day, now, "CALENDAR_UNVERIFIED", mode=mode)
            return "CALENDAR_UNVERIFIED", None
    elif from_calendar(day, calendar) != "SESSION":
        status = from_calendar(day, calendar)
        log(root, day, now, status, mode=mode)
        return status, None
    if now < close_time(day):
        log(root, day, now, "WAIT_SOURCE", mode=mode)
        return "WAIT_SOURCE", None
    try:
        raw = path.read_bytes()
    except OSError as error:
        log(root, day, now, "FAILED", error_type="SOURCE_ACCESS_BLOCKED", source=str(path), detail=str(error), mode=mode)
        return "FAILED", None
    raw_hash = sha(raw)
    put(root / "data/raw" / f"{raw_hash}.json", raw)
    if prior_failure is not None:
        f = (root / "data/runs" / prior_failure).resolve(strict=True)
        if f.parent != (root / "data/runs").resolve() or load(f).get("trade_date") != str(day) or load(f).get("status") != "FAILED":
            raise Rejected("Link to the original failure for this date")
    try:
        row = normalized(json.loads(raw.decode("utf-8-sig")), day, now)
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        log(root, day, now, "QUARANTINED", raw_sha256=raw_hash, detail=str(error), mode=mode, prior_failure=prior_failure)
        return "QUARANTINED", None
    code_hash = sha(Path(__file__).read_bytes())
    snapshot_key = sha(json_bytes([raw_hash, mode, code_hash]))
    destination = root / "data/validated" / str(day) / f"{snapshot_key}.json"
    if destination.exists():
        existing = load(destination)
        if existing["raw_sha256"] != raw_hash:
            raise Rejected("Snapshot hash disagrees with receipt")
        log(root, day, now, "DUPLICATE", snapshot=snapshot_key, prior_failure=prior_failure)
        return "DUPLICATE", destination
    captured_live = (mode == "PILOT" and day.year == 2026 and now.date() == day and
                     now <= datetime.combine(day, time(20), CST) and row["within_original_cutoff"] and
                     bool(row["market_published_at"] and row["index_published_at"]))
    formal_candidate = (mode == "FORMAL" and now.date() == day and now <= datetime.combine(day, time(20), CST)
                        and row["within_original_cutoff"] and row["market_published_at"] and row["index_published_at"])
    previous = states(root, day)
    differences = []
    for _, prior in previous:
        fields = [k for k in ("up", "down", "flat", "turnover_cny", "open", "close")
                  if prior["row"][k] != row[k]]
        if fields:
            differences.append({"receipt": prior["raw_sha256"], "fields": fields})
    status = ("CONFLICT" if differences else "PILOT_CAPTURED" if captured_live else
              "FORMAL_CANDIDATE" if formal_candidate else
              "LATE_RECONSTRUCTION" if mode in {"PILOT", "FORMAL"} else "HISTORICAL_RECONSTRUCTION")
    receipt = {"raw_sha256": raw_hash, "code_sha256": code_hash, "snapshot": snapshot_key,
               "normalized_at": now.isoformat(), "validation_at": now.isoformat(), "snapshot_created_at": now.isoformat(),
               "status": status, "sample_identity": ("PILOT_COLLECTION" if captured_live else "PENDING_VAL_REGISTRATION"
                                                     if formal_candidate else "POST_FREEZE_HISTORICAL_RECONSTRUCTION"),
               "formal_prospective": False, "prior_failure": prior_failure, "conflicts": differences, "row": row}
    put(destination, json_bytes(receipt))
    log(root, day, now, status, snapshot=snapshot_key, raw_sha256=raw_hash, prior_failure=prior_failure)
    return status, destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--mode", choices=("PILOT", "HISTORICAL", "FORMAL"), default="PILOT")
    parser.add_argument("--val", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--failed")
    parser.add_argument("--prior-failure")
    parser.add_argument("--source", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--detail", default="")
    args = parser.parse_args()
    now = datetime.now(CST)
    calendar = load(ROOT / "official_calendars.json")
    if args.failed:
        if args.receipt or not args.source or not args.detail:
            parser.error("Failure logging requires --source and --detail, without a receipt")
        print(record_failure(ROOT, args.date, now, args.failed, args.source, args.query, args.detail))
    elif args.receipt or from_calendar(args.date, calendar) != "SESSION" or now < close_time(args.date):
        print(intake(ROOT, args.date, now, calendar, args.receipt, args.mode, args.prior_failure, args.val))
    else:
        parser.error("Supply the reviewed local --receipt or record --failed")


if __name__ == "__main__":
    main()
