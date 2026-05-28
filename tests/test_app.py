import io
import json
import re
from datetime import datetime
from pathlib import Path

from PIL import Image

from app import (
    add_metadata,
    allowed_file,
    calculate_risk,
    format_metadata_date,
    generate_html_report,
    generate_json_report,
    generate_report,
    group_metadata,
    sanitize_file,
    validate_file_signature,
)


def write_png(path):
    image = Image.new("RGB", (1, 1), color="white")
    image.save(path, format="PNG")


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color="white").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def test_allowed_file_validation():
    assert allowed_file("image.jpg")
    assert allowed_file("document.PDF")
    assert not allowed_file("script.exe")
    assert not allowed_file("noextension")


def test_file_signature_validation_rejects_mismatch(tmp_path):
    fake_image = tmp_path / "fake.jpg"
    fake_image.write_text("not really an image", encoding="utf-8")
    assert not validate_file_signature(fake_image, "jpg")

    png = tmp_path / "real.png"
    write_png(png)
    assert validate_file_signature(png, "png")


def test_risk_scoring_high_for_author():
    metadata = []
    add_metadata(metadata, "Author", "Jane Analyst")
    add_metadata(metadata, "File Size", "10 KB")
    risk = calculate_risk(metadata)
    assert risk["level"] == "High"
    assert risk["score"] >= 35


def test_date_formatting_pdf_exif_and_datetime():
    assert format_metadata_date("D:20260429124157-04'00'") == "April 29, 2026 at 12:41 PM (UTC-04:00)"
    assert format_metadata_date("2026:04:29 12:41:57") == "April 29, 2026 at 12:41 PM"
    assert format_metadata_date(datetime(2026, 4, 29, 12, 41, 57)) == "April 29, 2026 at 12:41 PM"
    assert format_metadata_date("not-a-date") == "not-a-date"


def test_metadata_categorization_and_risk_ordering():
    metadata = []
    add_metadata(metadata, "File Size", "10 KB")
    add_metadata(metadata, "Creation Date", "2026:04:29 12:41:57")
    add_metadata(metadata, "Author", "Jane Analyst")
    grouped = group_metadata(metadata)
    assert grouped[0]["category"] == "Identity Metadata"
    assert grouped[0]["metadata_items"][0]["risk"] == "High"


def test_report_generation(tmp_path, monkeypatch):
    import app

    monkeypatch.setattr(app, "REPORT_FOLDER", tmp_path)
    metadata = []
    add_metadata(metadata, "File Size", "10 KB")
    add_metadata(metadata, "SHA-256 Hash", "abc123")
    risk = calculate_risk(metadata)
    report_name = generate_report("sample.png", "png", metadata, risk, "Cleaned", datetime(2026, 4, 29, 12, 41, 57))
    report_path = Path(tmp_path) / report_name
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "File Name: sample.png" in content
    assert "Cleaned File Status: Cleaned" in content


def test_json_and_html_report_generation(tmp_path, monkeypatch):
    import app

    monkeypatch.setattr(app, "REPORT_FOLDER", tmp_path)
    metadata = []
    add_metadata(metadata, "File Size", "10 KB")
    add_metadata(metadata, "SHA-256 Hash", "abc123")
    risk = calculate_risk(metadata)

    json_name = generate_json_report("sample.png", "png", metadata, risk, "Cleaned", datetime(2026, 4, 29, 12, 41, 57))
    html_name = generate_html_report("sample.png", "png", metadata, risk, "Cleaned", datetime(2026, 4, 29, 12, 41, 57))

    payload = json.loads((tmp_path / json_name).read_text(encoding="utf-8"))
    assert payload["file_name"] == "sample.png"
    assert payload["cleaned_file_status"] == "Cleaned"
    assert "Metadata Privacy Report" in (tmp_path / html_name).read_text(encoding="utf-8")


def test_sanitize_image_creates_cleaned_file(tmp_path, monkeypatch):
    import app

    monkeypatch.setattr(app, "CLEANED_FOLDER", tmp_path)
    source = tmp_path / "source.png"
    write_png(source)

    cleaned_name, status = sanitize_file(source, "png", "source.png")

    assert status == "Cleaned"
    assert cleaned_name.endswith(".png")
    assert (tmp_path / cleaned_name).exists()


def test_upload_route_rejects_empty_and_unsupported():
    import app

    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        empty_response = test_client.post("/", data={}, follow_redirects=True)
        assert b"No file was selected" in empty_response.data

        unsupported_response = test_client.post(
            "/",
            data={"files": (io.BytesIO(b"hello"), "notes.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert b"Unsupported file type" in unsupported_response.data


def test_upload_route_handles_corrupt_supported_file():
    import app

    app.app.config["TESTING"] = True
    corrupt_png = b"\x89PNG\r\n\x1a\nnot a valid png payload"
    with app.app.test_client() as test_client:
        response = test_client.post(
            "/",
            data={"files": (io.BytesIO(corrupt_png), "broken.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert b"corrupt or unreadable" in response.data


def test_upload_route_renders_valid_png_results(tmp_path, monkeypatch):
    import app

    upload_dir = tmp_path / "uploads"
    report_dir = tmp_path / "reports"
    cleaned_dir = tmp_path / "cleaned"
    upload_dir.mkdir()
    report_dir.mkdir()
    cleaned_dir.mkdir()
    monkeypatch.setattr(app, "UPLOAD_FOLDER", upload_dir)
    monkeypatch.setattr(app, "REPORT_FOLDER", report_dir)
    monkeypatch.setattr(app, "CLEANED_FOLDER", cleaned_dir)

    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        response = test_client.post(
            "/",
            data={"files": (png_bytes(), "sample.png"), "auto_delete": "on"},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert b"Scan Complete" in response.data
    assert b"Download TXT" in response.data
    assert b"Download Sanitized File" in response.data
    assert not any(upload_dir.iterdir())
    assert len(list(report_dir.iterdir())) == 3
    assert len(list(cleaned_dir.iterdir())) == 1


def test_batch_scan_links_to_individual_file_details(tmp_path, monkeypatch):
    import app

    upload_dir = tmp_path / "uploads"
    report_dir = tmp_path / "reports"
    cleaned_dir = tmp_path / "cleaned"
    upload_dir.mkdir()
    report_dir.mkdir()
    cleaned_dir.mkdir()
    monkeypatch.setattr(app, "UPLOAD_FOLDER", upload_dir)
    monkeypatch.setattr(app, "REPORT_FOLDER", report_dir)
    monkeypatch.setattr(app, "CLEANED_FOLDER", cleaned_dir)
    app.BATCH_SCANS.clear()

    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        batch_response = test_client.post(
            "/",
            data={
                "files": [
                    (png_bytes(), "first.png"),
                    (png_bytes(), "second.png"),
                ],
                "auto_delete": "on",
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert batch_response.status_code == 200
        assert b"Batch result files" in batch_response.data
        assert b"View Details" in batch_response.data
        assert b"Download Sanitized" in batch_response.data

        detail_urls = re.findall(rb'href="(/results/[^"]+/[^"]+)"', batch_response.data)
        assert detail_urls

        detail_response = test_client.get(detail_urls[0].decode())
        assert detail_response.status_code == 200
        assert b"File Details" in detail_response.data
        assert b"Back to Batch Results" in detail_response.data
        assert b"Category" in detail_response.data
        assert b"Download Sanitized File" in detail_response.data

        scoped_txt_url = re.search(rb'href="(/results/[^"]+/[^"]+/reports/txt)"', detail_response.data)
        assert scoped_txt_url
        download_response = test_client.get(scoped_txt_url.group(1).decode())
        assert download_response.status_code == 200

        scoped_cleaned_url = re.search(rb'href="(/results/[^"]+/[^"]+/cleaned)"', detail_response.data)
        assert scoped_cleaned_url
        cleaned_response = test_client.get(scoped_cleaned_url.group(1).decode())
        assert cleaned_response.status_code == 200

    assert not any(upload_dir.iterdir())
    assert len(list(report_dir.iterdir())) == 6
    assert len(list(cleaned_dir.iterdir())) == 2


def test_missing_batch_scan_shows_clean_message():
    import app

    app.BATCH_SCANS.clear()
    app.app.config["TESTING"] = True
    with app.app.test_client() as test_client:
        response = test_client.get("/results/missing-scan", follow_redirects=True)

    assert response.status_code == 200
    assert b"This batch scan is no longer available" in response.data
