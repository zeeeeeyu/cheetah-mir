"""Offline source-aware panel, prospectively captured register, and locked comparisons."""

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist, median
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
RAW_FIELDS = ("date", "up", "down", "flat", "turnover_cny", "market_url", "market_published_at", "market_collected_at",
              "open", "close", "index_url", "index_published_at", "index_collected_at")
PAPER_HOSTS = {"thepaper.cn", "m.thepaper.cn", "www.thepaper.cn"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if any(None in row or None in row.values() for row in rows):
        raise ValueError(f"Malformed CSV: {path}")
    return rows


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protocol():
    lock = read_json(ROOT / "protocol_lock.json")
    for name in ("protocol.json", "S1_VAL_01_PREREGISTRATION.md"):
        if hash_file(ROOT / name) != lock["sha256"][name]:
            raise ValueError(f"Frozen document changed: {name}")
    return read_json(ROOT / "protocol.json"), lock


def days(first, last):
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def historical_sessions(calendar):
    out = []
    for year, spec in sorted(calendar["historical"].items()):
        holidays = {d for lo, hi in spec["closed"] for d in days(date.fromisoformat(f"{year}-{lo}"), date.fromisoformat(f"{year}-{hi}"))}
        out.extend(d for d in days(date(int(year), 1, 1), date(int(year), 12, 31)) if d.weekday() < 5 and d not in holidays)
    return out


def number(row, key):
    value = row.get(key)
    if value in (None, "", "UNKNOWN"):
        return None
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError(f"Nonfinite {key}")
    return value


def close(row):
    value = number(row, "close")
    if value is not None and value <= 0:
        raise ValueError("Nonpositive price")
    return value


def breadth(row):
    counts = [number(row, key) for key in ("up", "down", "flat")]
    if None in counts:
        return None
    if any(value < 0 or value != value.to_integral_value() for value in counts) or sum(counts) == 0:
        raise ValueError("Invalid breadth counts")
    return counts[0] / sum(counts)


def pct(end, start):
    return None if end is None or start is None else end / start - 1


def yn(flag):
    return "" if flag is None else str(bool(flag)).lower()


def display(value):
    return "" if value is None else str(value.quantize(Decimal("0.00000001")))


def publication_precision(value):
    if not value:
        return "UNKNOWN"
    return "MINUTE" if value[17:19] == "00" else "SECOND"


def classify(prior, current):
    before, today = close(prior), close(current)
    up_day = today > before if today is not None and before is not None else None
    before_vol, today_vol = number(prior, "turnover_cny"), number(current, "turnover_cny")
    if any(v is not None and v <= 0 for v in (before_vol, today_vol)):
        raise ValueError("Nonpositive turnover")
    volume_up = today_vol > before_vol if today_vol is not None and before_vol is not None else None
    before_b, today_b = breadth(prior), breadth(current)
    narrower = today_b < before_b if today_b is not None and before_b is not None else None
    complete = None not in (up_day, volume_up, narrower)
    if up_day is False:
        group = "NOT_UP"
    elif not complete:
        group = "INPUT_MISSING"
    elif not volume_up:
        group = "UP_NOT_EXPANDING"
    else:
        group = "TRIGGER" if narrower else "PRIMARY_CONTROL"
    return group, complete, up_day, volume_up, narrower


def market_state(prior, six_back, spec):
    state_return = pct(close(prior), close(six_back))
    cutoff = Decimal(str(spec["state_cutoff_abs_return"]))
    if state_return is None:
        return "UNKNOWN", None
    return ("STRONG" if state_return >= cutoff else "WEAK" if state_return <= -cutoff else "NEUTRAL"), state_return


def historical_panel(spec, schedule, raw, legacy):
    rows = []
    for i, day in enumerate(schedule):
        key = str(day)
        if i == len(schedule) - 1:
            continue
        if "2021-01-01" <= key <= spec["historical_exploration"][1]:
            partition = "HISTORICAL_EXPLORATION"
        elif spec["historical_validation"][0] <= key <= spec["historical_validation"][1]:
            partition = "HISTORICAL_VALIDATION"
        elif key in legacy:
            partition = legacy[key]
        else:
            continue
        prev_day, next_day = (schedule[i - 1] if i else None), schedule[i + 1]
        prior, current, following = (raw.get(str(d), {}) for d in (prev_day, day, next_day))
        group, complete, up_day, volume_up, narrower = classify(prior, current)
        state, state_return = market_state(prior, raw.get(str(schedule[i - 6]), {}) if i >= 6 else {}, spec)
        next_close, next_open = close(following), number(following, "open")
        if next_open is not None and next_open <= 0:
            raise ValueError("Nonpositive opening price")
        daily_return = pct(close(current), close(prior))
        primary = pct(next_close, close(current))
        rows.append({"date": key, "partition": partition, "asset": spec["asset"], "panel": spec["primary_panel"],
                     "previous_session": str(prev_day or ""), "next_session": str(next_day),
                     **{field: current.get(field, "") for field in RAW_FIELDS if field != "date"},
                     "market_publication_precision": publication_precision(current.get("market_published_at")),
                     "index_publication_precision": publication_precision(current.get("index_published_at")),
                     "prior_close": prior.get("close", ""), "prior_turnover_cny": prior.get("turnover_cny", ""),
                     "prior_breadth": display(breadth(prior)), "breadth": display(breadth(current)),
                     "return": display(daily_return), "up_day": yn(up_day), "turnover_increased": yn(volume_up),
                     "breadth_narrowed": yn(narrower), "complete_input": yn(complete), "group": group,
                     "market_state": state, "prior_5d_return": display(state_return),
                     "next_open": following.get("open", ""), "next_close": following.get("close", ""),
                     "next_index_url": following.get("index_url", ""), "next_index_published_at": following.get("index_published_at", ""),
                     "next_index_collected_at": following.get("index_collected_at", ""), "next_return": display(primary),
                     "next_down": yn(primary < 0 if primary is not None else None), "next_overnight": display(pct(next_open, close(current))),
                     "next_open_to_close": display(pct(next_close, next_open)),
                     "case_label": ("FAILURE" if group == "TRIGGER" and primary is not None and primary >= 0 else
                                    "MISSED_PRIMARY" if group == "PRIMARY_CONTROL" and primary is not None and primary < 0 else
                                    "MISSED_OTHER_UP" if up_day and group == "UP_NOT_EXPANDING" and primary is not None and primary < 0 else ""),
                     "scope_breadth": "SH_SZ_BJ", "scope_turnover": "SH_SZ_CNY", "stock_pool_version": "UNKNOWN",
                     "availability": "HISTORICAL_RECONSTRUCTION" if current else "NOT_ACQUIRED"})
    return rows


def wilson(k, n, level):
    p = k / n
    z = NormalDist().inv_cdf((1 + level) / 2)
    midpoint = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / (1 + z * z / n)
    return midpoint - half, midpoint + half


def newcombe(x, n, y, m, level):
    a, b = x / n, y / m
    l1, u1 = wilson(x, n, level)
    l2, u2 = wilson(y, m, level)
    return max(-1, a - b - math.hypot(a - l1, u2 - b)), min(1, a - b + math.hypot(u1 - a, b - l2))


def fisher(x, n, y, m):
    total = x + y
    denominator = math.comb(n + m, total)
    weights = [math.comb(n, a) * math.comb(m, total - a) for a in range(max(0, total - m), min(n, total) + 1)]
    observed = math.comb(n, x) * math.comb(m, y)
    return sum(weight for weight in weights if weight <= observed) / denominator


def counts(rows, predicate):
    chosen = [r for r in rows if predicate(r)]
    scored = [r for r in chosen if r["next_down"] in {"true", "false"}]
    return {"inputs": len(chosen), "scored": len(scored), "next_down": sum(r["next_down"] == "true" for r in scored)}


def percentile(values, fraction):
    k = (len(values) - 1) * fraction
    low, high = math.floor(k), math.ceil(k)
    return values[low] + (values[high] - values[low]) * (k - low)


def moving_block(rows, spec):
    size = spec["bootstrap_block_sessions"]
    iterations = spec["bootstrap_repetitions"]
    if len(rows) < size:
        return None, 0
    rng = random.Random(spec["bootstrap_seed"])
    entries = [(r["group"], r["next_down"]) for r in rows]
    values = []
    for _ in range(iterations):
        result = Counter()
        indices = [rng.randrange(len(entries) - size + 1) for _ in range(math.ceil(len(entries) / size))]
        resample = [row for index in indices for row in entries[index:index + size]][:len(entries)]
        result.update(resample)
        n = result["TRIGGER", "true"] + result["TRIGGER", "false"]
        m = result["PRIMARY_CONTROL", "true"] + result["PRIMARY_CONTROL", "false"]
        if n and m:
            values.append(result["TRIGGER", "true"] / n - result["PRIMARY_CONTROL", "true"] / m)
    if len(values) / iterations < spec["bootstrap_min_valid_fraction"]:
        return None, len(values)
    values.sort()
    alpha = (1 - spec["ci_level"]) / 2
    return [percentile(values, alpha), percentile(values, 1 - alpha)], len(values)


def summarize(rows, spec, eligible_for_inference=False):
    trigger = counts(rows, lambda r: r["group"] == "TRIGGER")
    control = counts(rows, lambda r: r["group"] == "PRIMARY_CONTROL")
    up = counts(rows, lambda r: r["up_day"] == "true")
    complete = sum(r["complete_input"] == "true" for r in rows)
    complete_up = sum(r["up_day"] == r["complete_input"] == "true" for r in rows)
    n, m = trigger["scored"], control["scored"]
    x, y = trigger["next_down"], control["next_down"]
    effect = x / n - y / m if n and m else None
    ci = newcombe(x, n, y, m, spec["ci_level"]) if effect is not None else None
    pvalue = fisher(x, n, y, m) if effect is not None else None
    enough = bool(rows) and complete / len(rows) >= spec["minimum_input_coverage"] and min(n, m) >= spec["minimum_group_n"]
    block, valid_draws = moving_block(rows, spec) if eligible_for_inference and enough else (None, 0)
    status = "INSUFFICIENT_SAMPLE" if not enough else "INCONCLUSIVE"
    if block and ci and min(block[0], ci[0]) > 0:
        status = "POSITIVE_ASSOCIATION"
    elif block and ci and max(block[1], ci[1]) < 0:
        status = "REVERSE_ASSOCIATION"
    def outcome_medians(group):
        selected = [r for r in rows if r["group"] == group]
        return {key: (float(median(values)) if (values := [Decimal(r[key]) for r in selected if r[key]]) else None)
                for key in ("next_overnight", "next_open_to_close")}

    return {"sessions": len(rows), "complete_inputs": complete, "missing_inputs": len(rows) - complete,
            "confirmed_up": up, "complete_up": complete_up,
            "comparable_up_expanding": trigger["inputs"] + control["inputs"], "trigger": trigger, "primary_control": control,
            "trigger_rates": {label: (trigger["inputs"] / denominator if denominator else None) for label, denominator in
                              (("all_complete", complete), ("complete_up", complete_up), ("comparable_up_expanding", trigger["inputs"] + control["inputs"]))},
            "nontrigger_up_confirmed": counts(rows, lambda r: r["group"] in {"PRIMARY_CONTROL", "UP_NOT_EXPANDING"}),
            "risk_difference": effect, "newcombe_95_descriptive": ci, "fisher_two_sided_descriptive": pvalue,
            "moving_block_95": block, "bootstrap_valid_draws": valid_draws, "status": status,
            "secondary_medians": {key: outcome_medians(key) for key in ("TRIGGER", "PRIMARY_CONTROL")},
            "states": {state: {group: counts(rows, lambda r: r["market_state"] == state and r["group"] == group)
                               for group in ("TRIGGER", "PRIMARY_CONTROL")} for state in ("STRONG", "NEUTRAL", "WEAK", "UNKNOWN")},
            "exceptions": [{"date": r["date"], "label": r["case_label"], "next_return": r["next_return"]}
                           for r in rows if r["case_label"]]}


def aware(value):
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise ValueError("Timezone required")
    return dt.astimezone(CST)


def check_public_row(row):
    day = date.fromisoformat(row["date"])
    for fields, url, published, collected in ((["up", "down", "flat", "turnover_cny"], "market_url", "market_published_at", "market_collected_at"),
                                               (["open", "close"], "index_url", "index_published_at", "index_collected_at")):
        if not any(row.get(field) for field in fields):
            continue
        if urlsplit(row.get(url, "")).scheme != "https" or not row.get(collected):
            raise ValueError(f"Untraceable source: {row['date']} {url}")
        captured = aware(row[collected])
        if captured < datetime.combine(day, time(15), CST):
            raise ValueError("Close observation captured before close")
        if row.get(published) and not datetime.combine(day, time(15), CST) <= aware(row[published]) <= captured:
            raise ValueError("Publication chronology invalid")
    url = urlsplit(row.get("market_url", ""))
    if row.get("market_url") and (url.hostname not in PAPER_HOSTS or not url.path.startswith("/newsDetail_forward_")):
        raise ValueError("Primary breadth requires a The Paper report")


def project_legacy(path):
    market = read_csv(path / "R11_MARKET_DAILY.csv")
    prices = {r["date"]: r for r in read_csv(path / "R11_INDEX_OHLC.csv") if (r["exchange"], r["asset_code"]) == ("SSE", "000300")}
    for r in market:
        p = prices[r["date"]]
        yield dict(date=r["date"], up=r["wind_up"], down=r["wind_down"], flat=r["wind_flat"], turnover_cny=r["turnover_sh_sz_cny"],
                   market_url=r["turnover_source_url"], market_published_at=r["turnover_published_at"], market_collected_at=r["turnover_collected_at"],
                   open=p["open"], close=p["close"], index_url=p["source_url"], index_published_at="", index_collected_at=p["collected_at"])
    for r in read_csv(path / "historical_h5_july_source.csv"):
        yield dict(date=r["date"], up=r["up_sh_sz_bj"], down=r["down_sh_sz_bj"], flat=r["flat_sh_sz_bj"],
                   turnover_cny=str(Decimal(r["turnover_sh_sz_cny_100m"]) * 100000000) if r["turnover_sh_sz_cny_100m"] else "",
                   market_url=r["breadth_and_turnover_source"], market_published_at=r["breadth_and_turnover_published_cst"],
                   market_collected_at="", open="", close=r["csi300_close"], index_url=r["csi300_source"], index_published_at="", index_collected_at="")


def build_historical(spec, schedule, legacy_path):
    legacy_rows = list(project_legacy(legacy_path))
    legacy = {r["date"]: "HYPOTHESIS_GENERATION" if r["date"] == "2026-11-11" else "LEGACY_EXPLORATORY" for r in legacy_rows
              if r["date"] not in {"2026-07-17", "2026-07-27"}}
    raw = {r["date"]: r for r in legacy_rows}
    for path in sorted((ROOT / "inputs").glob("history_*.csv")):
        for row in read_csv(path):
            if row["date"] in raw or date.fromisoformat(row["date"]) not in schedule:
                raise ValueError(f"Duplicate or unscheduled session: {row['date']}")
            check_public_row(row)
            raw[row["date"]] = row
    # A post-cutoff pilot receipt is kept in the pilot register, not the historical validation set.
    rows = historical_panel(spec, schedule, raw, legacy)
    write_csv(ROOT / "S1_VAL_01_HISTORICAL_SAMPLE.csv", rows)
    return {key: summarize([r for r in rows if r["partition"] == key], spec, key == "HISTORICAL_VALIDATION")
            for key in ("HISTORICAL_EXPLORATION", "HISTORICAL_VALIDATION", "LEGACY_EXPLORATORY", "HYPOTHESIS_GENERATION")}


def write_once(path, value):
    path.parent.mkdir(exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def formal_schedule(spec):
    path = ROOT / "formal_sessions.json"
    if not path.exists():
        return None
    document = read_json(path)
    url = urlsplit(document["official_calendar_url"])
    if url.scheme != "https" or url.hostname != "www.sse.com.cn":
        raise ValueError("Provide an official SSE calendar URL")
    planned = list(map(date.fromisoformat, document["sessions_with_adjacent_anchors"]))
    first, last = map(date.fromisoformat, spec["formal_prospective_dates"])
    if not planned or planned != sorted(set(planned)) or any(d.weekday() > 4 for d in planned) or not planned[0] < first <= last < planned[-1]:
        raise ValueError("Calendar must contain unique chronological sessions and surrounding anchors")
    return planned


def formal_capture(spec, lock, path, now, missing_reason=None):
    schedule = formal_schedule(spec)
    first, last = map(date.fromisoformat, spec["formal_prospective_dates"])
    day = now.date()
    if not schedule or day not in schedule or not first <= day <= last or not time(15) <= now.time() <= time(20):
        raise ValueError("Confirmed scheduled session and same-day capture hours required")
    index = schedule.index(day)
    if missing_reason:
        payload = {"group": "INPUT_MISSING", "reason": missing_reason, "complete_input": "false"}
    else:
        packet = read_json(path)
        prev, cur = packet["previous"], packet["current"]
        if packet["asset"] != spec["asset"] or packet["panel"] != spec["primary_panel"]:
            raise ValueError("Wrong panel or asset")
        for row, expected in ((prev, schedule[index - 1]), (cur, day)):
            check_public_row(row)
            if row["date"] != str(expected) or any(not row.get(field) for field in ("market_published_at", "index_published_at", "market_collected_at", "index_collected_at")):
                raise ValueError("Same-session receipts with traceable publication required")
            if any(aware(row[field]) > now for field in ("market_collected_at", "index_collected_at")):
                raise ValueError("Future receipt")
        group, complete, up_day, volume, narrow = classify(prev, cur)
        state, state_return = "UNKNOWN", None
        if index >= 6:
            old_file = ROOT / "formal_signals" / f"{schedule[index - 5]}.json"
            if old_file.exists():
                prior_record = read_json(old_file)
                old_prior = prior_record.get("inputs", {}).get("previous", {})
                if old_prior.get("date") == str(schedule[index - 6]):
                    state, state_return = market_state(prev, old_prior, spec)
        payload = dict(group=group, complete_input=yn(complete), up_day=yn(up_day), turnover_increased=yn(volume),
                       breadth_narrowed=yn(narrow), market_state=state, prior_5d_return=display(state_return), inputs=packet,
                       receipt_sha256=hash_file(path))
    payload.update(date=str(day), next_session=str(schedule[index + 1]), recorded_at=now.isoformat(),
                   protocol_sha256=lock["sha256"]["protocol.json"], calendar_sha256=hash_file(ROOT / "formal_sessions.json"))
    write_once(ROOT / "formal_signals" / f"{day}.json", payload)


def formal_settle(spec, lock, path, now):
    packet = read_json(path)
    signal_path = ROOT / "formal_signals" / f"{date.fromisoformat(packet['signal_date'])}.json"
    signal = read_json(signal_path)
    if signal["protocol_sha256"] != lock["sha256"]["protocol.json"] or signal["calendar_sha256"] != hash_file(ROOT / "formal_sessions.json"):
        raise ValueError("Calendar/protocol changed since recording")
    row = packet["outcome"]
    if packet["asset"] != spec["asset"] or row["date"] != signal["next_session"] or "inputs" not in signal:
        raise ValueError("Wrong outcome identity, date, or unavailable signal")
    check_public_row(row)
    if not row.get("index_published_at") or aware(row["index_collected_at"]) > now:
        raise ValueError("Outcome receipt must have a past, explicit publication time")
    last, end, opn = close(signal["inputs"]["current"]), close(row), number(row, "open")
    if last is None or end is None or (opn is not None and opn <= 0):
        raise ValueError("Missing or invalid outcome prices")
    write_once(ROOT / "formal_outcomes" / signal_path.name, {
        "signal_sha256": hash_file(signal_path), "recorded_at": now.isoformat(), "evidence": packet,
        "next_down": yn(end < last), "next_return": display(pct(end, last)), "next_overnight": display(pct(opn, last)),
        "next_open_to_close": display(pct(end, opn)), "case_label": "FAILURE" if signal["group"] == "TRIGGER" and end >= last else
        "MISSED_PRIMARY" if signal["group"] == "PRIMARY_CONTROL" and end < last else
        "MISSED_OTHER_UP" if signal["group"] == "UP_NOT_EXPANDING" and end < last else "",
    })


def prospective_register(spec, old, now):
    pilot = read_json(old / "R11_FORWARD_PROTOCOL.json")["planned_signal_sessions"]
    close_receipt = read_json(ROOT / "inputs/pilot_20261113_late.json")
    check_public_row(close_receipt["current"])
    confirmed = formal_schedule(spec)
    first, last = map(date.fromisoformat, spec["formal_prospective_dates"])
    formal = [d for d in confirmed if first <= d <= last] if confirmed else [d for d in days(first, last) if d.weekday() < 5]
    rows = []
    for window, sessions in (("R1_1_PILOT", [date.fromisoformat(p) for p in pilot]), ("FORMAL_PROSPECTIVE", formal)):
        for day in sessions:
            formal_row = window == "FORMAL_PROSPECTIVE"
            passed = now >= datetime.combine(day, time(15), CST)
            row = dict(window=window, date=str(day), calendar_status="OFFICIAL_PENDING" if formal_row and not confirmed else "SCHEDULED",
                       passed_close=yn(passed), eligible_up="", expanding_up="", trigger="", complete_input="", group="",
                       outcome_available="", status="CALENDAR_PENDING" if formal_row and not confirmed else "MISSED_CAPTURE" if now > datetime.combine(day, time(20), CST) else "AWAITING_INPUT" if passed else "PENDING_FUTURE",
                       next_session="", next_down="", next_return="", next_overnight="", next_open_to_close="", market_state="UNKNOWN",
                       case_label="", recorded_at="", observed_close="", source="")
            if not formal_row:
                pilot_file = old / "forward_records" / f"{day}.json"
                pilot_outcome = old / "forward_outcomes" / f"{day}.json"
                if pilot_file.exists():
                    signal = read_json(pilot_file)
                    recorded = aware(signal["recorded_at"])
                    valid = (recorded.date() == day and time(15) <= recorded.time() <= time(20) and
                             signal["protocol_sha256"] == hash_file(old / "R11_FORWARD_PROTOCOL.json"))
                    if valid:
                        row.update(status="PILOT_CAPTURED", recorded_at=signal["recorded_at"], group=signal["flag_status"],
                                   source=str(pilot_file), next_session=signal["outcome_session_scheduled"])
                        if "inputs" in signal:
                            previous, current = signal["inputs"]["previous"], signal["inputs"]["current"]
                            up = Decimal(current["close"]) > Decimal(previous["close"])
                            volume = Decimal(current["turnover"]) > Decimal(previous["turnover"])
                            row.update(eligible_up=yn(up), expanding_up=yn(up and volume), complete_input="true",
                                       trigger=yn(signal["flag_status"] == "TRIGGER"), observed_close=current["close"])
                        if pilot_outcome.exists():
                            outcome = read_json(pilot_outcome)
                            if outcome["signal_sha256"] != hash_file(pilot_file):
                                raise ValueError("Pilot signal changed after settlement")
                            row.update(outcome_available="true", next_down=yn(outcome["down"]), next_return=outcome["close_to_close"],
                                       next_open_to_close=outcome["open_to_close"])
                    else:
                        row.update(status="PILOT_INVALID_CAPTURE", source=str(pilot_file))
            if not formal_row and row["status"] != "PILOT_CAPTURED" and str(day) == close_receipt["current"]["date"] and passed:
                group, complete, up, volume, narrower = classify(close_receipt["previous"], close_receipt["current"])
                row.update(status="LATE_RECONSTRUCTION_NOT_PROSPECTIVE", observed_close=close_receipt["current"]["close"],
                           source="inputs/pilot_20261113_late.json", eligible_up=yn(up), expanding_up=yn(up and volume),
                           trigger=yn(group == "TRIGGER"), complete_input=yn(complete), group=group)
            signal_file = ROOT / "formal_signals" / f"{day}.json"
            outcome_file = ROOT / "formal_outcomes" / f"{day}.json"
            if formal_row and confirmed and signal_file.exists():
                signal = read_json(signal_file)
                for key in ("complete_input", "group", "market_state", "recorded_at", "next_session"):
                    row[key] = signal.get(key, "")
                if signal["calendar_sha256"] != hash_file(ROOT / "formal_sessions.json") or signal["protocol_sha256"] != hash_file(ROOT / "protocol.json"):
                    raise ValueError("Formal record has a different calendar or protocol hash")
                up, volume = signal.get("up_day", ""), signal.get("turnover_increased", "")
                complete = signal["complete_input"] == "true"
                row.update(status="CAPTURED" if complete else "CAPTURED_INCOMPLETE", eligible_up=up,
                           expanding_up="false" if "false" in (up, volume) else "true" if up == volume == "true" else "",
                           trigger=yn(signal["group"] == "TRIGGER") if complete else "", source=str(signal_file.relative_to(ROOT)))
                if outcome_file.exists():
                    outcome = read_json(outcome_file)
                    if outcome["signal_sha256"] != hash_file(signal_file):
                        raise ValueError("Signal bytes changed after outcome")
                    row.update(outcome_available="true", **{key: outcome[key] for key in ("next_down", "next_return", "next_overnight", "next_open_to_close", "case_label")})
            rows.append(row)
    write_csv(ROOT / "S1_VAL_01_PROSPECTIVE_REGISTER.csv", rows)
    transformed = [{**r, "up_day": r["eligible_up"]} for r in rows if r["window"] == "FORMAL_PROSPECTIVE"]
    final_due = confirmed and now > datetime.combine(confirmed[-1], time(15), CST)
    return {"formal": summarize(transformed, spec, bool(final_due)),
            "formal_calendar_confirmed": bool(confirmed), "pilot_captured_in_window": sum(r["status"] == "PILOT_CAPTURED" for r in rows),
            "pilot_sessions": len(pilot), "formal_signals_captured": sum(r["status"] == "CAPTURED" for r in transformed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, default=ROOT.parent / "S1-EX-01/versions/R1.1_20261113_validation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", type=Path)
    mode.add_argument("--skip")
    mode.add_argument("--settle", type=Path)
    args = parser.parse_args()
    spec, lock = protocol()
    now = datetime.now(CST)
    if args.capture or args.skip:
        formal_capture(spec, lock, args.capture, now, args.skip)
    if args.settle:
        formal_settle(spec, lock, args.settle, now)
    results = build_historical(spec, historical_sessions(read_json(ROOT / "calendars.json")), args.legacy)
    results["prospective"] = prospective_register(spec, args.legacy, now)
    (ROOT / "validation_summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Built historical panel, coverage/statistics and prospective register.")


if __name__ == "__main__":
    main()
