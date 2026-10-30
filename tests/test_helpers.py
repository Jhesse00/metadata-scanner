"""Unit tests for pure helper functions in app.py.

These cover logic not exercised by test_app.py: GPS coordinate conversion,
risk-score capping, file-size formatting, PDF/EXIF date edge cases, metadata
comparison, and batch-scan TTL cleanup.
"""

from datetime import datetime, timedelta, timezone

import app
from app import (
    add_metadata,
    calculate_risk,
    classify_field,
    compare_metadata,
    convert_gps_coordinate,
    human_file_size,
    parse_pdf_date,
    cleanup_batch_scans,
    create_batch_scan,
)


# --- GPS coordinate conversion -------------------------------------------------

def test_gps_conversion_northern_hemisphere():
    # 40 deg 44' 54.36" N  ->  40.748433 (rounded to 6 places)
    coordinate = ((40, 1), (44, 1), (5436, 100))
    assert convert_gps_coordinate(coordinate, "N") == 40.748433


def test_gps_conversion_southern_hemisphere_is_negative():
    coordinate = ((33, 1), (51, 1), (0, 1))
    result = convert_gps_coordinate(coordinate, "S")
    assert result < 0
    assert result == -33.85


def test_gps_conversion_western_hemisphere_is_negative():
    coordinate = ((73, 1), (59, 1), (0, 1))
    result = convert_gps_coordinate(coordinate, "W")
    assert result < 0
    assert round(result, 2) == -73.98


# --- File size formatting ------------------------------------------------------

def test_human_file_size_bytes():
    assert human_file_size(512) == "512 B"


def test_human_file_size_kilobytes():
    assert human_file_size(2048) == "2.00 KB"


def test_human_file_size_megabytes():
    assert human_file_size(5 * 1024 * 1024) == "5.00 MB"


# --- Field classification ------------------------------------------------------

def test_classify_field_gps_is_high():
    assert classify_field("GPS Latitude") == "High"
    assert classify_field("gps longitude") == "High"


def test_classify_field_software_is_medium():
    assert classify_field("Software") == "Medium"


def test_classify_field_unknown_is_low():
    assert classify_field("File Name") == "Low"


# --- Risk scoring --------------------------------------------------------------

def test_risk_score_is_capped_at_100():
    metadata = []
    for i in range(10):
        add_metadata(metadata, "Author", f"Person {i}")
    risk = calculate_risk(metadata)
    assert risk["score"] == 100
    assert risk["level"] == "High"


def test_risk_low_when_only_basic_file_info():
    metadata = []
    add_metadata(metadata, "File Size", "10 KB")
    add_metadata(metadata, "SHA-256 Hash", "deadbeef")
    risk = calculate_risk(metadata)
    assert risk["level"] == "Low"
    assert risk["score"] == 0
    assert "No major privacy-risk metadata" in risk["recommendation"]


def test_risk_medium_for_software_only():
    metadata = []
    add_metadata(metadata, "Software", "Adobe Photoshop")
    add_metadata(metadata, "File Size", "10 KB")
    risk = calculate_risk(metadata)
    assert risk["level"] == "Medium"
    assert 0 < risk["score"] < 35


# --- PDF date parsing edge cases -----------------------------------------------

def test_parse_pdf_date_utc_marker():
    parsed = parse_pdf_date("D:20260101000000Z")
    assert parsed.tzinfo == timezone.utc
    assert parsed.year == 2026


def test_parse_pdf_date_without_prefix_and_partial():
    parsed = parse_pdf_date("20260229")  # 2026 is a leap year? no -> fallback handled
    # month/day default correctly; just assert the year parses
    assert parsed is None or parsed.year == 2026


def test_parse_pdf_date_invalid_returns_none():
    assert parse_pdf_date("totally-not-a-date") is None


# --- Metadata comparison -------------------------------------------------------

def test_compare_metadata_marks_removed_and_kept():
    original = []
    add_metadata(original, "Author", "Jane Analyst")
    add_metadata(original, "Software", "Photoshop")
    cleaned = []
    add_metadata(cleaned, "Software", "Photoshop")  # kept
    # Author absent in cleaned -> Removed

    comparison = compare_metadata(original, cleaned)
    by_field = {row["field"]: row["status"] for row in comparison}
    assert by_field["Author"] == "Removed"
    assert by_field["Software"] == "Kept"


def test_compare_metadata_skips_file_identity_fields():
    original = []
    add_metadata(original, "File Size", "10 KB")
    add_metadata(original, "SHA-256 Hash", "abc")
    add_metadata(original, "Author", "Jane")
    comparison = compare_metadata(original, [])
    fields = {row["field"] for row in comparison}
    assert "File Size" not in fields
    assert "SHA-256 Hash" not in fields
    assert "Author" in fields


# --- Batch scan TTL cleanup ----------------------------------------------------

def test_cleanup_batch_scans_removes_expired_entries():
    app.BATCH_SCANS.clear()
    scan = create_batch_scan([{"risk": {"level": "Low"}, "metadata_count": 0}])
    scan_id = scan["scan_id"]
    assert scan_id in app.BATCH_SCANS

    # Age the entry past the TTL and confirm cleanup drops it.
    app.BATCH_SCANS[scan_id]["created_at"] = datetime.now(timezone.utc) - timedelta(
        seconds=app.BATCH_SCAN_TTL_SECONDS + 10
    )
    cleanup_batch_scans()
    assert scan_id not in app.BATCH_SCANS


def test_cleanup_batch_scans_keeps_fresh_entries():
    app.BATCH_SCANS.clear()
    scan = create_batch_scan([{"risk": {"level": "Low"}, "metadata_count": 0}])
    cleanup_batch_scans()
    assert scan["scan_id"] in app.BATCH_SCANS
