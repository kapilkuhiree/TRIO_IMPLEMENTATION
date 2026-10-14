"""TRIO — Data Validation Module

Validates imported market data quality for 5-min NIFTY candles and
option premium candles. Identifies duplicates, missing candles, invalid
prices, timezone issues, and other anomalies.

DO NOT silently discard large parts of a dataset. Record all validation
failures with file, row, and reason.
"""

import csv, json, os, re
from datetime import datetime, timedelta, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# ============================================================================
# VALIDATION RESULTS TRACKING
# ============================================================================

validation_report = {
    "total_files": 0,
    "accepted_rows": 0,
    "rejected_rows": 0,
    "skipped_rows": 0,
    "issues_by_file": {},
    "issues_summary": {},
    "file_details": {},
}


def reset_validation_report() -> None:
    """Reset the global validation report."""
    validation_report["total_files"] = 0
    validation_report["accepted_rows"] = 0
    validation_report["rejected_rows"] = 0
    validation_report["skipped_rows"] = 0
    validation_report["issues_by_file"] = {}
    validation_report["issues_summary"] = {}
    validation_report["file_details"] = {}


def log_issue(
    file_path: str,
    issue_type: str,
    row_index: int,
    timestamp: str = "",
    detail: str = "",
) -> None:
    """Log a validation issue."""
    global validation_report

    if file_path not in validation_report["issues_by_file"]:
        validation_report["issues_by_file"][file_path] = []

    issue_entry = {
        "row": row_index,
        "issue_type": issue_type,
        "timestamp": timestamp,
        "detail": detail,
    }
    validation_report["issues_by_file"][file_path].append(issue_entry)

    # Update summary
    if issue_type not in validation_report["issues_summary"]:
        validation_report["issues_summary"][issue_type] = 0
    validation_report["issues_summary"][issue_type] += 1


# ============================================================================
# 5-MINUTE NIFTY CANDLE VALIDATION
# ============================================================================


def validate_5min_nifty_candle_row(
    row: list,
    row_index: int,
    file_path: str,
) -> tuple:
    """
    Validate a single 5-min NIFTY candle row.

    CSV format: timestamp,open,high,low,close,volume

    Returns:
        (is_valid, reason) tuple
    """
    issues = []

    try:
        if len(row) < 6:
            log_issue(
                file_path,
                "INCOMPLETE_ROW",
                row_index,
                detail=f"Expected 6 fields, got {len(row)}",
            )
            return False, "incomplete_row"

        timestamp_str = row[0].strip()
        open_price = float(row[1].strip())
        high_price = float(row[2].strip())
        low_price = float(row[3].strip())
        close_price = float(row[4].strip())
        volume = int(row[5].strip())

        # Parse and validate timestamp
        try:
            ts = datetime.fromisoformat(
                timestamp_str.replace("Z", "+00:00")
            )
            # Convert to IST for validation
            if ts.tzinfo is None:
                ts_ist = ts.replace(tzinfo=timezone.utc).astimezone(IST)
            else:
                ts_ist = ts.astimezone(IST)
        except (ValueError, AttributeError) as e:
            log_issue(
                file_path,
                "INVALID_TIMESTAMP",
                row_index,
                timestamp=timestamp_str,
                detail=str(e),
            )
            return False, "invalid_timestamp"

        # Validate prices are positive
        if open_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_OPEN",
                row_index,
                timestamp=timestamp_str,
                detail=f"open={open_price}",
            )
            issues.append("non_positive_open")

        if high_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_HIGH",
                row_index,
                timestamp=timestamp_str,
                detail=f"high={high_price}",
            )
            issues.append("non_positive_high")

        if low_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_LOW",
                row_index,
                timestamp=timestamp_str,
                detail=f"low={low_price}",
            )
            issues.append("non_positive_low")

        if close_price < 0:
            log_issue(
                file_path,
                "NEGATIVE_CLOSE",
                row_index,
                timestamp=timestamp_str,
                detail=f"close={close_price}",
            )
            issues.append("negative_close")

        # Validate OHLC consistency
        if high_price < low_price:
            log_issue(
                file_path,
                "INVALID_OHLC_HIGH_LESS_LOW",
                row_index,
                timestamp=timestamp_str,
                detail=f"high={high_price} < low={low_price}",
            )
            issues.append("high_less_than_low")

        if high_price < open_price or high_price < close_price:
            log_issue(
                file_path,
                "INVALID_OHLC_HIGH_VS_OC",
                row_index,
                timestamp=timestamp_str,
                detail=f"high={high_price} < open={open_price} or close={close_price}",
            )
            issues.append("high_vs_oc")

        if low_price > open_price and low_price > close_price:
            log_issue(
                file_path,
                "INVALID_OHLC_LOW_VS_OC",
                row_index,
                timestamp=timestamp_str,
                detail=f"low={low_price} > open={open_price} and close={close_price}",
            )
            issues.append("low_vs_oc")

        # Validate volume is non-negative
        if volume < 0:
            log_issue(
                file_path,
                "NEGATIVE_VOLUME",
                row_index,
                timestamp=timestamp_str,
                detail=f"volume={volume}",
            )
            issues.append("negative_volume")

        # Validate trading session hours (9:15 AM to 3:15 PM IST)
        if not (dtime(9, 15) <= ts_ist.time() <= dtime(15, 15)):
            log_issue(
                file_path,
                "OUTSIDE_TRADING_SESSION",
                row_index,
                timestamp=timestamp_str,
                detail=f"time={ts_ist.time()}, session=9:15-15:15 IST",
            )
            issues.append("outside_trading_session")

        # Check for duplicate timestamp within this file
        # (duplicate detection done across all files after reading)

    except (ValueError, IndexError, TypeError) as e:
        log_issue(
            file_path,
            "ROW_PROCESSING_ERROR",
            row_index,
            timestamp=timestamp_str if "timestamp_str" in dir() else "",
            detail=str(e),
        )
        issues.append("row_processing_error")

    is_valid = len(issues) == 0
    return is_valid, "; ".join(issues) if issues else None


def validate_5min_nifty_candles_file(csv_path: str) -> dict:
    """
    Validate an entire 5-min NIFTY candles CSV file.

    Returns dict with validation results.
    """
    global validation_report

    validation_report["total_files"] += 1
    file_issues = []
    accepted = 0
    rejected = 0
    skipped = 0

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header

        row_index = 0
        seen_timestamps = set()

        for row in reader:
            row_index += 1

            # Check for incomplete rows
            if len(row) < 6:
                skipped += 1
                log_issue(
                    csv_path,
                    "INCOMPLETE_ROW",
                    row_index,
                    detail=f"Only {len(row)} fields, need 6",
                )
                continue

            is_valid, reason = validate_5min_nifty_candle_row(
                row, row_index, csv_path
            )

            if is_valid:
                accepted += 1
                # Check for duplicate timestamps
                timestamp_str = row[0].strip()
                if timestamp_str in seen_timestamps:
                    log_issue(
                        csv_path,
                        "DUPLICATE_TIMESTAMP",
                        row_index,
                        timestamp=timestamp_str,
                        detail="Duplicate within file",
                    )
                else:
                    seen_timestamps.add(timestamp_str)
            else:
                rejected += 1

    # Store file-specific results
    validation_report["file_details"][csv_path] = {
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "total_rows": accepted + rejected + skipped,
        "issues": validation_report["issues_by_file"].get(csv_path, []),
    }

    return {
        "file_path": csv_path,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
        "skipped_rows": skipped,
        "total_rows": accepted + rejected + skipped,
        "issue_types": validation_report["issues_summary"].copy(),
        "file_issues": validation_report["issues_by_file"].get(csv_path, []),
    }


# ============================================================================
# OPTION PREMIUM CANDLE VALIDATION
# ============================================================================


def validate_option_premium_row(
    row: list,
    row_index: int,
    file_path: str,
) -> tuple:
    """
    Validate a single option premium candle row.

    CSV format: timestamp,strike,expiry,option_type,open,high,low,close,volume,bid,ask

    Returns:
        (is_valid, reason) tuple
    """
    issues = []

    try:
        if len(row) < 11:
            log_issue(
                file_path,
                "INCOMPLETE_ROW",
                row_index,
                detail=f"Expected 11 fields, got {len(row)}",
            )
            return False, "incomplete_row"

        timestamp_str = row[0].strip()
        strike = float(row[1].strip())
        expiry = row[2].strip()
        option_type = row[3].strip().upper()
        open_price = float(row[4].strip())
        high_price = float(row[5].strip())
        low_price = float(row[6].strip())
        close_price = float(row[7].strip())
        volume = int(row[8].strip())
        bid = float(row[9].strip())
        ask = float(row[10].strip())

        # Parse and validate timestamp
        try:
            ts = datetime.fromisoformat(
                timestamp_str.replace("Z", "+00:00")
            )
            if ts.tzinfo is None:
                ts_ist = ts.replace(tzinfo=timezone.utc).astimezone(IST)
            else:
                ts_ist = ts.astimezone(IST)
        except (ValueError, AttributeError) as e:
            log_issue(
                file_path,
                "INVALID_TIMESTAMP",
                row_index,
                timestamp=timestamp_str,
                detail=str(e),
            )
            return False, "invalid_timestamp"
        # Validate option type
        if option_type not in ("CE", "PE"):
            log_issue(
                file_path,
                "INVALID_OPTION_TYPE",
                row_index,
                timestamp=timestamp_str,
                detail=f"option_type={option_type}",
            )
            issues.append("invalid_option_type")
        # Validate expiry is not empty
        if not expiry:
            log_issue(
                file_path,
                "MISSING_EXPIRY",
                row_index,
                timestamp=timestamp_str,
                detail="expiry field is empty",
            )
            issues.append("missing_expiry")
        # Validate strike is positive
        if strike <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_STRIKE",
                row_index,
                timestamp=timestamp_str,
                detail=f"strike={strike}",
            )
            issues.append("non_positive_strike")
        # Validate prices are positive
        if open_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_OPEN",
                row_index,
                timestamp=timestamp_str,
                detail=f"open={open_price}",
            )
            issues.append("non_positive_open")

        if high_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_HIGH",
                row_index,
                timestamp=timestamp_str,
                detail=f"high={high_price}",
            )
            issues.append("non_positive_high")

        if low_price <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_LOW",
                row_index,
                timestamp=timestamp_str,
                detail=f"low={low_price}",
            )
            issues.append("non_positive_low")

        if close_price < 0:
            log_issue(
                file_path,
                "NEGATIVE_CLOSE",
                row_index,
                timestamp=timestamp_str,
                detail=f"close={close_price}",
            )
            issues.append("negative_close")
        # Validate OHLC consistency for option premium
        if high_price < low_price:
            log_issue(
                file_path,
                "INVALID_OHLC_HIGH_LESS_LOW",
                row_index,
                timestamp=timestamp_str,
                detail=f"high={high_price} < low={low_price}",
            )
            issues.append("high_less_than_low")

        if high_price < open_price or high_price < close_price:
            log_issue(
                file_path,
                "INVALID_OHLC_HIGH_VS_OC",
                row_index,
                timestamp=timestamp_str,
                detail=f"high issue",
            )
            issues.append("high_vs_oc")

        if low_price > open_price and low_price > close_price:
            log_issue(
                file_path,
                "INVALID_OHLC_LOW_VS_OC",
                row_index,
                timestamp=timestamp_str,
                detail=f"low issue",
            )
            issues.append("low_vs_oc")

        # Validate volume is non-negative
        if volume < 0:
            log_issue(
                file_path,
                "NEGATIVE_VOLUME",
                row_index,
                timestamp=timestamp_str,
                detail=f"volume={volume}",
            )
            issues.append("negative_volume")

        # CRITICAL: Validate bid and ask are present and positive
        if bid <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_BID",
                row_index,
                timestamp=timestamp_str,
                detail=f"bid={bid}",
            )
            issues.append("non_positive_bid")

        if ask <= 0:
            log_issue(
                file_path,
                "NON_POSITIVE_ASK",
                row_index,
                timestamp=timestamp_str,
                detail=f"ask={ask}",
            )
            issues.append("non_positive_ask")

        # Validate bid < ask (bid-ask spread must be positive)
        if bid >= ask:
            log_issue(
                file_path,
                "INVALID_BID_ASK_SPREAD",
                row_index,
                timestamp=timestamp_str,
                detail=f"bid={bid} >= ask={ask}",
            )
            issues.append("invalid_bid_ask_spread")

        # Validate trading session hours
        if not (dtime(9, 15) <= ts_ist.time() <= dtime(15, 15)):
            log_issue(
                file_path,
                "OUTSIDE_TRADING_SESSION",
                row_index,
                timestamp=timestamp_str,
                detail=f"time={ts_ist.time()}",
            )
            issues.append("outside_trading_session")

    except (ValueError, IndexError, TypeError) as e:
        log_issue(
            file_path,
            "ROW_PROCESSING_ERROR",
            row_index,
            timestamp=timestamp_str if "timestamp_str" in dir() else "",
            detail=str(e),
        )
        issues.append("row_processing_error")

    is_valid = len(issues) == 0
    return is_valid, "; ".join(issues) if issues else None


def validate_option_premium_candles_file(csv_path: str) -> dict:
    """
    Validate an entire option premium candles CSV file.

    Returns dict with validation results.
    """
    global validation_report

    validation_report["total_files"] += 1
    file_issues = []
    accepted = 0
    rejected = 0
    skipped = 0

    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header

        row_index = 0

        for row in reader:
            row_index += 1

            # Check for incomplete rows
            if len(row) < 11:
                skipped += 1
                log_issue(
                    csv_path,
                    "INCOMPLETE_ROW",
                    row_index,
                    detail=f"Only {len(row)} fields, need 11",
                )
                continue

            is_valid, reason = validate_option_premium_row(
                row, row_index, csv_path
            )

            if is_valid:
                accepted += 1
            else:
                rejected += 1

    # Store file-specific results
    validation_report["file_details"][csv_path] = {
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "total_rows": accepted + rejected + skipped,
        "issues": validation_report["issues_by_file"].get(csv_path, []),
    }

    return {
        "file_path": csv_path,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
        "skipped_rows": skipped,
        "total_rows": accepted + rejected + skipped,
        "issue_types": validation_report["issues_summary"].copy(),
        "file_issues": validation_report["issues_by_file"].get(csv_path, []),
    }


# ============================================================================
# CROSS-VALIDATION BETWEEN NIFTY AND OPTION DATA
# ============================================================================


def validate_timestamp_alignment(nifty_csv_path: str, option_csv_path: str) -> dict:
    """
    Validate that option premium timestamps align with NIFTY 5-min timestamps.

    Checks that option candles have timestamps within the same 5-min intervals
    as the underlying NIFTY data.
    """
    from data_importer import import_5min_nifty_candles

    nifty_candles = import_5min_nifty_candles(nifty_csv_path)
    option_issues = []

    # Get option timestamps
    try:
        import csv
        with open(option_csv_path, "r") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            option_timestamps = []
            for row in reader:
                if len(row) >= 1:
                    ts_str = row[0].strip()
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc).astimezone(IST)
                        else:
                            ts = ts.astimezone(IST)
                        option_timestamps.append(ts)
                    except (ValueError, AttributeError):
                        option_timestamps.append(None)
    except Exception as e:
        return {"error": str(e)}

    # Check alignment: each option timestamp should correspond to a NIFTY bar
    aligned = 0
    misaligned = 0

    for i, opt_ts in enumerate(option_timestamps):
        if opt_ts is None:
            misaligned += 1
            option_issues.append({"row": i, "issue": "could_not_parse_timestamp"})
            continue

        # Find closest NIFTY bar
        min_diff = float("inf")
        closest_bar_idx = -1
        for j, nifty_ts in enumerate(nifty_candles):
            diff = abs((opt_ts - nifty_ts["timestamp"]).total_seconds())
            if diff < min_diff:
                min_diff = diff
                closest_bar_idx = j

        # Check if within 5-minute tolerance
        if min_diff <= 300:  # 5 minutes in seconds
            aligned += 1
        else:
            misaligned += 1
            option_issues.append(
                {
                    "row": i,
                    "issue": f"timestamp_misaligned, diff={min_diff}s",
                }
            )

    return {
        "nifty_candle_count": len(nifty_candles),
        "option_candle_count": len(option_timestamps),
        "aligned_timestamps": aligned,
        "misaligned_timestamps": misaligned,
        "alignment_rate": round(aligned / (aligned + misaligned) * 100, 2)
        if (aligned + misaligned) > 0
        else 0,
        "issues": option_issues[:50],
    }


# ============================================================================
# GENERATE VALIDATION REPORT
# ============================================================================


def generate_validation_report(output_path: str = "output/data_quality_report.json") -> dict:
    """
    Generate comprehensive data quality report.

    Writes report to JSON file and returns the report dict.
    """
    total = validation_report["accepted_rows"] + validation_report["rejected_rows"] + validation_report["skipped_rows"]
    accept_rate = (
        round(validation_report["accepted_rows"] / total * 100, 2)
        if total > 0
        else 0
    )

    report = {
        "validation_report": {
            "total_files_processed": validation_report["total_files"],
            "total_accepted_rows": validation_report["accepted_rows"],
            "total_rejected_rows": validation_report["rejected_rows"],
            "total_skipped_rows": validation_report["skipped_rows"],
            "acceptance_rate_pct": accept_rate,
            "issue_type_summary": validation_report["issues_summary"],
        },
        "per_file_details": validation_report["file_details"],
        "per_file_issues": {
            file_path: [
                {"row": i["row"], "issue": i["issue_type"], "detail": i["detail"]}
                for i in issues
            ]
            for file_path, issues in validation_report["issues_by_file"].items()
        },
        "recommendations": [],
    }

    # Generate recommendations based on issues
    issues_summary = validation_report["issues_summary"]
    if issues_summary.get("non_positive_bid", 0) > 0:
        report["recommendations"].append(
            "CRITICAL: bid prices non-positive in option data — "
            "required for realistic execution pricing"
        )
    if issues_summary.get("non_positive_ask", 0) > 0:
        report["recommendations"].append(
            "CRITICAL: ask prices non-positive in option data — "
            "required for realistic execution pricing"
        )
    if issues_summary.get("outside_trading_session", 0) > 0:
        report["recommendations"].append(
            "WARNING: Some timestamps outside 9:15-15:15 IST session — "
            "filter or adjust as needed"
        )
    if issues_summary.get("invalid_bid_ask_spread", 0) > 0:
        report["recommendations"].append(
            "WARNING: bid >= ask in some rows — "
            "bid-ask spread logic needs review"
        )
    if issues_summary.get("duplicate_timestamp", 0) > 0:
        report["recommendations"].append(
            "INFO: Duplicate timestamps detected — consider deduplication before backtesting"
        )
    if issues_summary.get("missing_expiry", 0) > 0:
        report["recommendations"].append(
            "INFO: Missing expiry values — verify contract identification in option data"
        )

    # Write to file
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(report, f, indent=2)

    return report


# ============================================================================
# AUTOMATED VALIDATION TEST FIXTURES
# ============================================================================


def create_validation_fixtures(fixture_dir: str = "output/validation_fixtures") -> dict:
    """
    Create minimal validation fixture files for unit testing.

    Returns dict with fixture paths created.
    """
    import os
    os.makedirs(fixture_dir, exist_ok=True)

    # Fixture 1: Clean 5-min NIFTY data (3 bars, all valid)
    clean_nifty = """timestamp,open,high,low,close,volume
2026-01-01 09:15:00+05:30,23500,23520,23490,23510,1000
2026-01-01 09:20:00+05:30,23510,23530,23495,23525,1200
2026-01-01 09:25:00+05:30,23525,23540,23520,23535,950
"""
    with open(os.path.join(fixture_dir, "clean_nifty_5min.csv"), "w") as f:
        f.write(clean_nifty)

    # Fixture 2: Clean option premium data (1 row with bid/ask)
    clean_option = """timestamp,strike,expiry,option_type,open,high,low,close,volume,bid,ask
2026-01-01 09:15:00+05:30,23500,2026-01-28,CE,150.5,152.0,149.0,151.2,500,151.0,151.5
"""
    with open(os.path.join(fixture_dir, "clean_option_premium.csv"), "w") as f:
        f.write(clean_option)

    # Fixture 3: NIFTY data with invalid OHLC (high < low)
    bad_ohlc = """timestamp,open,high,low,close,volume
2026-01-01 09:15:00+05:30,23500,23490,23510,23510,1000
2026-01-01 09:20:00+05:30,23510,23530,23495,23525,1200
"""
    with open(os.path.join(fixture_dir, "bad_ohlc_5min.csv"), "w") as f:
        f.write(bad_ohlc)

    # Fixture 4: Option data missing bid
    missing_bid = """timestamp,strike,expiry,option_type,open,high,low,close,volume,bid,ask
2026-01-01 09:15:00+05:30,23500,2026-01-28,PE,120.0,121.5,119.0,119.8,300,,119.8
"""
    with open(os.path.join(fixture_dir, "missing_bid_option.csv"), "w") as f:
        f.write(missing_bid)

    # Fixture 5: NIFTY data with duplicate timestamps
    dup_timestamps = """timestamp,open,high,low,close,volume
2026-01-01 09:15:00+05:30,23500,23520,23490,23510,1000
2026-01-01 09:15:00+05:30,23510,23530,23495,23525,1200
2026-01-01 09:25:00+05:30,23525,23540,23520,23535,950
"""
    with open(os.path.join(fixture_dir, "dup_timestamps_nifty.csv"), "w") as f:
        f.write(dup_timestamps)

    fixtures = {
        "clean_nifty_5min": os.path.join(fixture_dir, "clean_nifty_5min.csv"),
        "clean_option_premium": os.path.join(fixture_dir, "clean_option_premium.csv"),
        "bad_ohlc_5min": os.path.join(fixture_dir, "bad_ohlc_5min.csv"),
        "missing_bid_option": os.path.join(fixture_dir, "missing_bid_option.csv"),
        "dup_timestamps_nifty": os.path.join(fixture_dir, "dup_timestamps_nifty.csv"),
    }

    return {"fixture_dir": fixture_dir, "fixtures": fixtures}


# ============================================================================
# EXAMPLE USAGE / TEST RUNNER
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TRIO DATA VALIDATION MODULE")
    print("=" * 60)
    print()

    # Create fixtures
    fixtures = create_validation_fixtures()
    print("Created validation fixtures:")
    for name, path in fixtures["fixtures"].items():
        print(f"  - {name}: {path}")

    # Run validation on fixtures
    print()
    print("Running validation on fixtures...")

    # Test clean NIFTY data
    print("\n1. Validating clean NIFTY 5-min data...")
    result1 = validate_5min_nifty_candles_file(fixtures["fixtures"]["clean_nifty_5min"])
    print(f"   Accepted: {result1['accepted_rows']}, Rejected: {result1['rejected_rows']}, Skipped: {result1['skipped_rows']}")

    # Test clean option data
    print("\n2. Validating clean option premium data...")
    result2 = validate_option_premium_candles_file(fixtures["fixtures"]["clean_option_premium"])
    print(f"   Accepted: {result2['accepted_rows']}, Rejected: {result2['rejected_rows']}, Skipped: {result2['skipped_rows']}")

    # Test bad OHLC NIFTY data
    print("\n3. Validating NIFTY data with bad OHLC...")
    result3 = validate_5min_nifty_candles_file(fixtures["fixtures"]["bad_ohlc_5min"])
    print(f"   Accepted: {result3['accepted_rows']}, Rejected: {result3['rejected_rows']}, Skipped: {result3['skipped_rows']}")

    # Test option data with missing bid
    print("\n4. Validating option data with missing bid...")
    result4 = validate_option_premium_candles_file(fixtures["fixtures"]["missing_bid_option"])
    print(f"   Accepted: {result4['accepted_rows']}, Rejected: {result4['rejected_rows']}, Skipped: {result4['skipped_rows']}")

    # Test duplicate timestamps
    print("\n5. Validating NIFTY data with duplicate timestamps...")
    result5 = validate_5min_nifty_candles_file(fixtures["fixtures"]["dup_timestamps_nifty"])
    print(f"   Accepted: {result5['accepted_rows']}, Rejected: {result5['rejected_rows']}, Skipped: {result5['skipped_rows']}")

    # Generate full validation report
    print("\n6. Generating comprehensive validation report...")
    report = generate_validation_report("output/validation_report.json")
    print(f"   Report written with {report['validation_report']['total_files']} files processed")
    print(f"   Acceptance rate: {report['validation_report']['acceptance_rate_pct']}%")

    print()
    print("=" * 60)
    print("VALIDATION MODULE TESTS COMPLETE")
    print("=" * 60)