import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitz
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from PIL import ExifTags, Image, UnidentifiedImageError
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
REPORT_FOLDER = BASE_DIR / "reports"
CLEANED_FOLDER = BASE_DIR / "cleaned"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf", "docx"}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024
BATCH_SCAN_TTL_SECONDS = 60 * 60
Image.MAX_IMAGE_PIXELS = 25_000_000

HIGH_RISK_FIELDS = {
    "gps latitude",
    "gps longitude",
    "author",
    "last modified by",
    "camera model",
    "camera make",
}
MEDIUM_RISK_FIELDS = {
    "software",
    "device software",
    "creation date",
    "creation datetime",
    "created date",
    "modification date",
    "modified date",
    "creator",
    "producer",
}

CATEGORY_FIELDS = {
    "Identity Metadata": {"author", "last modified by", "creator"},
    "Location Metadata": {"gps latitude", "gps longitude"},
    "Device Metadata": {"camera make", "camera model", "software", "device software"},
    "Time Metadata": {"creation date", "creation datetime", "created date", "modification date", "modified date"},
    "File Information": {"file name", "file type", "file size", "sha-256 hash", "page count", "image size"},
}

CATEGORY_ORDER = [
    "Identity Metadata",
    "Location Metadata",
    "Device Metadata",
    "Time Metadata",
    "File Information",
    "Other Metadata",
]

risk_priority = {
    "High": 3,
    "Medium": 2,
    "Low": 1,
}

BATCH_SCANS = {}


class ScanError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "metadata-scanner-local-dev-secret")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["REPORT_FOLDER"] = REPORT_FOLDER
app.config["CLEANED_FOLDER"] = CLEANED_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

UPLOAD_FOLDER.mkdir(exist_ok=True)
REPORT_FOLDER.mkdir(exist_ok=True)
CLEANED_FOLDER.mkdir(exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def file_extension(filename):
    return filename.rsplit(".", 1)[1].lower()


def validate_file_signature(file_path, extension):
    with open(file_path, "rb") as file_obj:
        header = file_obj.read(16)

    if extension in {"jpg", "jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == "png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == "pdf":
        return header.startswith(b"%PDF")
    if extension == "docx":
        if not header.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(file_path) as archive:
                return "[Content_Types].xml" in archive.namelist()
        except zipfile.BadZipFile:
            return False
    return False


def sha256_hash(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_file_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def timezone_suffix(dt_obj):
    offset = dt_obj.utcoffset()
    if offset is None:
        return ""

    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f" (UTC{sign}{hours:02d}:{minutes:02d})"


def format_datetime(dt_obj):
    hour = dt_obj.hour % 12 or 12
    am_pm = "AM" if dt_obj.hour < 12 else "PM"
    return f"{dt_obj.strftime('%B')} {dt_obj.day}, {dt_obj.year} at {hour}:{dt_obj.minute:02d} {am_pm}{timezone_suffix(dt_obj)}"


def parse_pdf_date(value):
    value = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", value)
    match = re.match(
        r"^D:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?\d{2}'?)?$",
        value,
    )
    if not match:
        return None

    year, month, day, hour, minute, second, tz_value = match.groups()
    dt_obj = datetime(
        int(year),
        int(month or 1),
        int(day or 1),
        int(hour or 0),
        int(minute or 0),
        int(second or 0),
    )
    if tz_value == "Z":
        return dt_obj.replace(tzinfo=timezone.utc)
    if tz_value:
        clean_tz = tz_value.replace("'", "")
        sign = 1 if clean_tz[0] == "+" else -1
        offset = timedelta(hours=int(clean_tz[1:3]), minutes=int(clean_tz[3:5]))
        return dt_obj.replace(tzinfo=timezone(sign * offset))
    return dt_obj


def parse_exif_date(value):
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:?\d{2})?$", value)
    if not match:
        return None
    year, month, day, hour, minute, second = [int(part) for part in match.groups()[:6]]
    dt_obj = datetime(year, month, day, hour, minute, second)
    tz_value = match.group(7)
    if tz_value == "Z":
        return dt_obj.replace(tzinfo=timezone.utc)
    if tz_value:
        clean_tz = tz_value.replace(":", "")
        sign = 1 if clean_tz[0] == "+" else -1
        offset = timedelta(hours=int(clean_tz[1:3]), minutes=int(clean_tz[3:5]))
        return dt_obj.replace(tzinfo=timezone(sign * offset))
    return dt_obj


def format_metadata_date(value):
    if value in (None, ""):
        return value

    try:
        if isinstance(value, datetime):
            return format_datetime(value)

        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return value

        parsed = parse_pdf_date(stripped) or parse_exif_date(stripped)
        if parsed:
            return format_datetime(parsed)

        iso_candidate = stripped.replace("Z", "+00:00")
        try:
            return format_datetime(datetime.fromisoformat(iso_candidate))
        except ValueError:
            return value
    except (TypeError, ValueError, OverflowError):
        return value


def is_timestamp_field(field, value):
    normalized = field.lower()
    return isinstance(value, datetime) or "date" in normalized or "time" in normalized


def categorize_field(field):
    normalized = field.lower()
    for category, fields in CATEGORY_FIELDS.items():
        if normalized in fields:
            return category
    return "Other Metadata"


def add_metadata(metadata, field, value):
    if value not in (None, "", [], {}):
        display_value = format_metadata_date(value) if is_timestamp_field(field, value) else value
        metadata.append(
            {
                "field": field,
                "value": str(display_value),
                "risk": classify_field(field),
                "category": categorize_field(field),
            }
        )


def classify_field(field):
    normalized = field.lower()
    if normalized in HIGH_RISK_FIELDS or "gps" in normalized:
        return "High"
    if normalized in MEDIUM_RISK_FIELDS or "date" in normalized or "time" in normalized:
        return "Medium"
    return "Low"


def rational_to_float(value):
    try:
        return float(value[0]) / float(value[1])
    except (TypeError, ZeroDivisionError, IndexError):
        return float(value)


def convert_gps_coordinate(coordinate, reference):
    degrees = rational_to_float(coordinate[0])
    minutes = rational_to_float(coordinate[1])
    seconds = rational_to_float(coordinate[2])
    decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
    if reference in ["S", "W"]:
        decimal = -decimal
    return round(decimal, 6)


def extract_with_exiftool(file_path):
    if not shutil.which("exiftool"):
        return None

    try:
        result = subprocess.run(
            ["exiftool", "-json", str(file_path)],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0 or not result.stdout:
        return None


    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    return records[0] if records else None


def extract_image_metadata(file_path):
    metadata = []
    try:
        with Image.open(file_path) as image:
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ScanError("This image appears to be corrupt or unreadable. Please upload a valid JPG, JPEG, or PNG file.") from exc

    exiftool_data = extract_with_exiftool(file_path)

    if exiftool_data:
        mappings = {
            "GPSLatitude": "GPS Latitude",
            "GPSLongitude": "GPS Longitude",
            "Make": "Camera Make",
            "Model": "Camera Model",
            "Software": "Software",
            "CreateDate": "Creation Date",
            "DateTimeOriginal": "Creation DateTime",
            "ImageSize": "Image Size",
        }
        for source, label in mappings.items():
            add_metadata(metadata, label, exiftool_data.get(source))
    else:
        try:
            with Image.open(file_path) as image:
                add_metadata(metadata, "Image Size", f"{image.width} x {image.height}")
                exif = image.getexif()
                decoded = {ExifTags.TAGS.get(tag, tag): value for tag, value in exif.items()}
                add_metadata(metadata, "Camera Make", decoded.get("Make"))
                add_metadata(metadata, "Camera Model", decoded.get("Model"))
                add_metadata(metadata, "Software", decoded.get("Software"))
                add_metadata(metadata, "Creation DateTime", decoded.get("DateTimeOriginal") or decoded.get("DateTime"))

                gps_info = decoded.get("GPSInfo")
                if gps_info:
                    gps = {ExifTags.GPSTAGS.get(key, key): value for key, value in gps_info.items()}
                    if "GPSLatitude" in gps and "GPSLatitudeRef" in gps:
                        add_metadata(metadata, "GPS Latitude", convert_gps_coordinate(gps["GPSLatitude"], gps["GPSLatitudeRef"]))
                    if "GPSLongitude" in gps and "GPSLongitudeRef" in gps:
                        add_metadata(metadata, "GPS Longitude", convert_gps_coordinate(gps["GPSLongitude"], gps["GPSLongitudeRef"]))
        except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise ScanError("This image appears to be corrupt or unreadable. Please upload a valid JPG, JPEG, or PNG file.") from exc

    add_metadata(metadata, "File Size", human_file_size(file_path.stat().st_size))
    add_metadata(metadata, "SHA-256 Hash", sha256_hash(file_path))
    return metadata


def extract_pdf_metadata(file_path):
    metadata = []
    try:
        with fitz.open(file_path) as document:
            pdf_meta = document.metadata or {}
            add_metadata(metadata, "Author", pdf_meta.get("author"))
            add_metadata(metadata, "Creator", pdf_meta.get("creator"))
            add_metadata(metadata, "Producer", pdf_meta.get("producer"))
            add_metadata(metadata, "Creation Date", pdf_meta.get("creationDate"))
            add_metadata(metadata, "Modification Date", pdf_meta.get("modDate"))
            add_metadata(metadata, "Page Count", document.page_count)
    except fitz.FileDataError as exc:
        raise ScanError("This PDF appears to be corrupt or unreadable. Please upload a valid PDF file.") from exc

    add_metadata(metadata, "File Size", human_file_size(file_path.stat().st_size))
    add_metadata(metadata, "SHA-256 Hash", sha256_hash(file_path))
    return metadata


def extract_docx_metadata(file_path):
    metadata = []
    try:
        document = Document(file_path)
    except (PackageNotFoundError, ValueError) as exc:
        raise ScanError("This DOCX file appears to be corrupt or unreadable. Please upload a valid DOCX file.") from exc
    props = document.core_properties
    add_metadata(metadata, "Author", props.author)
    add_metadata(metadata, "Last Modified By", props.last_modified_by)
    add_metadata(metadata, "Created Date", props.created)
    add_metadata(metadata, "Modified Date", props.modified)
    add_metadata(metadata, "Title", props.title)
    add_metadata(metadata, "Subject", props.subject)
    add_metadata(metadata, "File Size", human_file_size(file_path.stat().st_size))
    add_metadata(metadata, "SHA-256 Hash", sha256_hash(file_path))
    return metadata


def extract_metadata(file_path, extension):
    if extension in {"jpg", "jpeg", "png"}:
        return extract_image_metadata(file_path)
    if extension == "pdf":
        return extract_pdf_metadata(file_path)
    if extension == "docx":
        return extract_docx_metadata(file_path)
    raise ValueError("Unsupported file type")


def calculate_risk(metadata):
    sensitive = [item for item in metadata if item["risk"] in {"High", "Medium"}]
    high_count = sum(1 for item in metadata if item["risk"] == "High")
    medium_count = sum(1 for item in metadata if item["risk"] == "Medium")
    score = min(100, (high_count * 35) + (medium_count * 15))

    fields = {item["field"].lower() for item in metadata}
    explanations = []
    recommendations = []

    if {"gps latitude", "gps longitude"} & fields:
        explanations.append("GPS location data can reveal where an image was taken.")
        recommendations.append("Remove GPS metadata before sharing this image publicly.")
    if {"author", "last modified by", "creator"} & fields:
        explanations.append("Identity metadata can expose the creator or editor of a file.")
        recommendations.append("This file may expose the creator's name or editing identity.")
    if {"camera make", "camera model", "software"} & fields:
        explanations.append("Device metadata can reveal the camera, phone, or software used.")
        recommendations.append("Device metadata can reveal the camera, phone, or software used.")
    if {"producer"} & fields:
        explanations.append("Producer metadata can reveal the application used to create or export the file.")
        recommendations.append("Review producer and software metadata before distributing this file.")
    if any("date" in field or "time" in field for field in fields):
        explanations.append("Timestamps can reveal when a file was created or edited.")
        recommendations.append("Timestamps can reveal when a file was created or edited.")

    if high_count:
        level = "High"
    elif medium_count:
        level = "Medium"
        if not explanations:
            explanations.append("This file contains metadata that may reveal workflow or software details.")
            recommendations.append("Review the metadata before sharing this file outside trusted environments.")
    else:
        level = "Low"
        explanations.append("Only basic file information was detected.")
        recommendations.append("No major privacy-risk metadata was detected.")

    explanation = " ".join(dict.fromkeys(explanations))
    recommendation = " ".join(dict.fromkeys(recommendations))

    return {
        "level": level,
        "score": score,
        "sensitive_fields": [item["field"] for item in sensitive],
        "recommendation": recommendation,
        "recommendations": list(dict.fromkeys(recommendations)),
        "explanation": explanation,
    }


def metadata_value(metadata, field):
    for item in metadata:
        if item["field"].lower() == field.lower():
            return item["value"]
    return "Not found"


def group_metadata(metadata):
    grouped = []
    for category_index, category in enumerate(CATEGORY_ORDER):
        items = [item for item in metadata if item["category"] == category]
        if items:
            sorted_items = sorted(items, key=lambda item: risk_priority.get(item["risk"], 0), reverse=True)
            grouped.append(
                {
                    "category": category,
                    "metadata_items": sorted_items,
                    "highest_risk": max(risk_priority.get(item["risk"], 0) for item in sorted_items),
                    "category_index": category_index,
                }
            )
    return sorted(grouped, key=lambda group: (-group["highest_risk"], group["category_index"]))


def compare_metadata(original_metadata, cleaned_metadata):
    cleaned_by_field = {item["field"]: item["value"] for item in cleaned_metadata}
    comparison = []
    for item in original_metadata:
        if item["field"] in {"File Name", "File Type", "File Size", "SHA-256 Hash"}:
            continue
        cleaned_value = cleaned_by_field.get(item["field"], "Not Found")
        if cleaned_value == "Not Found":
            status = "Removed"
        elif cleaned_value == item["value"]:
            status = "Kept"
        else:
            status = "Removed" if cleaned_value in ("", "Not Found") else "Changed"
        comparison.append(
            {
                "field": item["field"],
                "original": item["value"],
                "cleaned": cleaned_value,
                "status": status,
                "risk": item["risk"],
            }
        )
    return sorted(comparison, key=lambda item: risk_priority.get(item["risk"], 0), reverse=True)


def sensitive_metadata_groups(metadata):
    groups = {
        "Location Metadata": [],
        "Identity Metadata": [],
        "Device Metadata": [],
        "Time Metadata": [],
        "Other Metadata": [],
    }
    for item in metadata:
        if item["risk"] not in {"High", "Medium"}:
            continue
        category = item["category"] if item["category"] in groups else "Other Metadata"
        groups[category].append(item)
    return {category: items for category, items in groups.items() if items}


def metadata_found_count(metadata):
    return sum(1 for item in metadata if item["field"] not in {"File Name", "File Type", "File Size", "SHA-256 Hash"})


def make_scan_id():
    return secrets.token_urlsafe(18)


def make_file_id():
    return secrets.token_urlsafe(12)


def cleanup_batch_scans():
    now = datetime.now(timezone.utc)
    expired_scan_ids = [
        scan_id
        for scan_id, scan in BATCH_SCANS.items()
        if (now - scan["created_at"]).total_seconds() > BATCH_SCAN_TTL_SECONDS
    ]
    for scan_id in expired_scan_ids:
        BATCH_SCANS.pop(scan_id, None)


def batch_summary(results):
    return {
        "total": len(results),
        "low": sum(1 for result in results if result["risk"]["level"] == "Low"),
        "medium": sum(1 for result in results if result["risk"]["level"] == "Medium"),
        "high": sum(1 for result in results if result["risk"]["level"] == "High"),
        "with_metadata": sum(1 for result in results if result["metadata_count"] > 0),
        "sanitized": sum(1 for result in results if result.get("cleaned_name")),
    }


def create_batch_scan(results):
    cleanup_batch_scans()
    scan_id = make_scan_id()
    for result in results:
        result["file_id"] = make_file_id()
        result["scan_id"] = scan_id
    BATCH_SCANS[scan_id] = {
        "scan_id": scan_id,
        "created_at": datetime.now(timezone.utc),
        "results": results,
        "result_map": {result["file_id"]: result for result in results},
        "summary": batch_summary(results),
    }
    return BATCH_SCANS[scan_id]


def get_batch_scan_or_redirect(scan_id):
    cleanup_batch_scans()
    scan = BATCH_SCANS.get(scan_id)
    if not scan:
        flash("This batch scan is no longer available. Please run a new scan.", "warning")
        return None
    return scan


def file_result_or_redirect(scan_id, file_id):
    scan = get_batch_scan_or_redirect(scan_id)
    if not scan:
        return None, None
    result = scan["result_map"].get(file_id)
    if not result:
        flash("That file result is no longer available. Please choose a file from the batch summary.", "warning")
        return scan, None
    return scan, result


def sanitize_image(file_path, output_path):
    try:
        with Image.open(file_path) as image:
            cleaned = Image.new(image.mode, image.size)
            cleaned.putdata(list(image.getdata()))
            cleaned.save(output_path)
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ScanError("This image could not be cleaned because it appears corrupt or unreadable.") from exc


def sanitize_pdf(file_path, output_path):
    try:
        with fitz.open(file_path) as document:
            document.set_metadata({})
            document.save(output_path, garbage=4, deflate=True)
    except fitz.FileDataError as exc:
        raise ScanError("This PDF could not be cleaned because it appears corrupt or unreadable.") from exc


def sanitize_docx(file_path, output_path):
    try:
        document = Document(file_path)
    except (PackageNotFoundError, ValueError) as exc:
        raise ScanError("This DOCX file could not be cleaned because it appears corrupt or unreadable.") from exc

    props = document.core_properties
    for attr in ["author", "last_modified_by", "title", "subject"]:
        try:
            setattr(props, attr, "")
        except (TypeError, ValueError):
            pass
    for attr in ["created", "modified"]:
        try:
            setattr(props, attr, None)
        except (TypeError, ValueError):
            pass
    document.save(output_path)


def sanitize_file(file_path, extension, filename):
    safe_base = secure_filename(Path(filename).stem) or "cleaned"
    output_name = f"{safe_base}-cleaned-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.{extension}"
    output_path = CLEANED_FOLDER / output_name

    if extension in {"jpg", "jpeg", "png"}:
        sanitize_image(file_path, output_path)
    elif extension == "pdf":
        sanitize_pdf(file_path, output_path)
    elif extension == "docx":
        sanitize_docx(file_path, output_path)
    else:
        return None, "Unsupported"

    return output_name, "Cleaned"


def generate_report(filename, file_type, metadata, risk, cleaned_status="Not requested", scan_date=None):
    scan_date = scan_date or datetime.now(timezone.utc)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    safe_base = secure_filename(Path(filename).stem) or "scan"
    report_name = f"{safe_base}-{timestamp}.txt"
    report_path = REPORT_FOLDER / report_name

    sensitive_lines = risk["sensitive_fields"] or ["No sensitive metadata detected"]
    content = [
        f"File Name: {filename}",
        f"File Type: {file_type.upper()}",
        f"File Size: {metadata_value(metadata, 'File Size')}",
        f"SHA-256 Hash: {metadata_value(metadata, 'SHA-256 Hash')}",
        f"Scan Date: {format_metadata_date(scan_date)} UTC",
        f"Risk Level: {risk['level']}",
        f"Risk Score: {risk['score']}/100",
        f"Cleaned File Status: {cleaned_status}",
        "",
        "Risk Explanation:",
        risk["explanation"],
        "",
        "Sensitive Metadata Found:",
        *[f"- {field}" for field in sensitive_lines],
        "",
        "Metadata Values:",
        *[f"- {item['field']}: {item['value']} ({item['risk']} Risk)" for item in metadata],
        "",
        "Recommendations:",
        *[f"- {item}" for item in risk["recommendations"]],
    ]
    report_path.write_text("\n".join(content), encoding="utf-8")
    return report_name


def generate_json_report(filename, file_type, metadata, risk, cleaned_status="Not requested", scan_date=None):
    scan_date = scan_date or datetime.now(timezone.utc)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    safe_base = secure_filename(Path(filename).stem) or "scan"
    report_name = f"{safe_base}-{timestamp}.json"
    report_path = REPORT_FOLDER / report_name
    payload = {
        "file_name": filename,
        "file_type": file_type.upper(),
        "file_size": metadata_value(metadata, "File Size"),
        "sha256_hash": metadata_value(metadata, "SHA-256 Hash"),
        "scan_date": format_metadata_date(scan_date),
        "risk_level": risk["level"],
        "risk_score": risk["score"],
        "cleaned_file_status": cleaned_status,
        "risk_explanation": risk["explanation"],
        "sensitive_metadata_found": risk["sensitive_fields"],
        "recommendations": risk["recommendations"],
        "metadata": metadata,
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_name


def generate_html_report(filename, file_type, metadata, risk, cleaned_status="Not requested", scan_date=None):
    scan_date = scan_date or datetime.now(timezone.utc)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    safe_base = secure_filename(Path(filename).stem) or "scan"
    report_name = f"{safe_base}-{timestamp}.html"
    report_path = REPORT_FOLDER / report_name
    sensitive = "".join(f"<li>{html.escape(field)}</li>" for field in risk["sensitive_fields"]) or "<li>No sensitive metadata detected</li>"
    recommendations = "".join(f"<li>{html.escape(item)}</li>" for item in risk["recommendations"])
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(item['field'])}</td>"
        f"<td>{html.escape(item['value'])}</td>"
        f"<td>{html.escape(item['risk'])}</td>"
        "</tr>"
        for item in metadata
    )
    content = f"""<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><title>Metadata Privacy Report</title>
<style>body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6;color:#111827;margin:2rem}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #d1d5db;padding:.75rem;text-align:left}}th{{background:#f3f4f6}}</style></head>
<body>
<h1>Metadata Privacy Report</h1>
<p><strong>File Name:</strong> {html.escape(filename)}</p>
<p><strong>File Type:</strong> {html.escape(file_type.upper())}</p>
<p><strong>File Size:</strong> {html.escape(metadata_value(metadata, 'File Size'))}</p>
<p><strong>SHA-256 Hash:</strong> {html.escape(metadata_value(metadata, 'SHA-256 Hash'))}</p>
<p><strong>Scan Date:</strong> {html.escape(format_metadata_date(scan_date))}</p>
<p><strong>Risk Level:</strong> {html.escape(risk['level'])}</p>
<p><strong>Risk Score:</strong> {risk['score']}/100</p>
<p><strong>Cleaned File Status:</strong> {html.escape(cleaned_status)}</p>
<h2>Sensitive Metadata Found</h2><ul>{sensitive}</ul>
<h2>Recommendations</h2><ul>{recommendations}</ul>
<h2>Metadata Values</h2><table><thead><tr><th>Field</th><th>Value</th><th>Risk</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""
    report_path.write_text(content, encoding="utf-8")
    return report_name


def scan_uploaded_file(uploaded_file, auto_delete=False):
    if not uploaded_file or uploaded_file.filename == "":
        raise ScanError("No file was selected. Choose a JPG, PNG, PDF, or DOCX file to scan.")
    if not allowed_file(uploaded_file.filename):
        raise ScanError("Unsupported file type. Please upload a JPG, JPEG, PNG, PDF, or DOCX file.")

    filename = secure_filename(uploaded_file.filename)
    if not filename:
        raise ScanError("Invalid file name.")

    extension = file_extension(filename)
    saved_path = UPLOAD_FOLDER / f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{filename}"
    uploaded_file.save(saved_path)

    try:
        if not validate_file_signature(saved_path, extension):
            raise ScanError("The file content does not match its extension. Please upload a valid supported file.")

        scan_date = datetime.now(timezone.utc)
        metadata = extract_metadata(saved_path, extension)
        add_metadata(metadata, "File Name", filename)
        add_metadata(metadata, "File Type", extension.upper())
        if not metadata:
            raise ScanError("No metadata could be read from this file.")

        risk = calculate_risk(metadata)
        cleaned_name = None
        cleaned_status = "Not cleaned"
        cleaned_metadata = []
        comparison = []
        try:
            cleaned_name, cleaned_status = sanitize_file(saved_path, extension, filename)
            if cleaned_name:
                cleaned_metadata = extract_metadata(CLEANED_FOLDER / cleaned_name, extension)
                add_metadata(cleaned_metadata, "File Name", cleaned_name)
                add_metadata(cleaned_metadata, "File Type", extension.upper())
                comparison = compare_metadata(metadata, cleaned_metadata)
        except ScanError:
            cleaned_status = "Cleaning failed"

        report_name = generate_report(filename, extension, metadata, risk, cleaned_status, scan_date)
        json_report_name = generate_json_report(filename, extension, metadata, risk, cleaned_status, scan_date)
        html_report_name = generate_html_report(filename, extension, metadata, risk, cleaned_status, scan_date)
        return {
            "filename": filename,
            "file_type": extension.upper(),
            "scan_time": format_metadata_date(scan_date),
            "metadata": metadata,
            "metadata_count": metadata_found_count(metadata),
            "grouped_metadata": group_metadata(metadata),
            "risk": risk,
            "report_name": report_name,
            "json_report_name": json_report_name,
            "html_report_name": html_report_name,
            "cleaned_name": cleaned_name,
            "cleaned_status": cleaned_status,
            "comparison": comparison,
            "file_size": metadata_value(metadata, "File Size"),
            "sha256": metadata_value(metadata, "SHA-256 Hash"),
            "sensitive_count": len(risk["sensitive_fields"]),
            "sensitive_groups": sensitive_metadata_groups(metadata),
        }
    finally:
        if auto_delete:
            saved_path.unlink(missing_ok=True)


@app.errorhandler(413)
def request_entity_too_large(_error):
    flash("File is too large. Please upload a file smaller than 10MB.", "danger")
    return redirect(url_for("index"))


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        uploaded_files = [file for file in request.files.getlist("files") if file and file.filename]
        auto_delete = request.form.get("auto_delete") == "on"
        if not uploaded_files:
            flash("No file was selected. Choose one or more JPG, PNG, PDF, or DOCX files to scan.", "warning")
            return redirect(url_for("index"))

        results = []
        errors = []
        for uploaded_file in uploaded_files:
            try:
                results.append(scan_uploaded_file(uploaded_file, auto_delete=auto_delete))
            except ScanError as error:
                errors.append(f"{uploaded_file.filename}: {error.message}")
            except Exception:
                errors.append(f"{uploaded_file.filename}: The file could not be scanned safely.")

        for error in errors:
            flash(error, "danger")

        if not results:
            return redirect(url_for("index"))

        if len(results) > 1:
            scan = create_batch_scan(results)
            return redirect(url_for("batch_results", scan_id=scan["scan_id"]))

        result = results[0]

        return render_template(
            "results.html",
            **result,
        )

    return render_template("index.html")


@app.route("/results/<scan_id>")
def batch_results(scan_id):
    scan = get_batch_scan_or_redirect(scan_id)
    if not scan:
        return redirect(url_for("index"))
    return render_template(
        "batch_results.html",
        scan_id=scan_id,
        results=scan["results"],
        summary=scan["summary"],
    )


@app.route("/results/<scan_id>/<file_id>")
def batch_file_results(scan_id, file_id):
    scan, result = file_result_or_redirect(scan_id, file_id)
    if not scan:
        return redirect(url_for("index"))
    if not result:
        return redirect(url_for("batch_results", scan_id=scan_id))
    context = {**result, "back_url": url_for("batch_results", scan_id=scan_id)}
    return render_template("batch_file_results.html", **context)


@app.route("/results/<scan_id>/<file_id>/reports/<report_type>")
def download_scoped_report(scan_id, file_id, report_type):
    scan, result = file_result_or_redirect(scan_id, file_id)
    if not result:
        if scan:
            return redirect(url_for("batch_results", scan_id=scan_id))
        return redirect(url_for("index"))
    report_fields = {
        "txt": "report_name",
        "json": "json_report_name",
        "html": "html_report_name",
    }
    report_field = report_fields.get(report_type)
    if not report_field:
        flash("Invalid report type.", "danger")
        return redirect(url_for("batch_file_results", scan_id=scan_id, file_id=file_id))
    return send_from_directory(REPORT_FOLDER, result[report_field], as_attachment=True)


@app.route("/results/<scan_id>/<file_id>/cleaned")
def download_scoped_cleaned(scan_id, file_id):
    scan, result = file_result_or_redirect(scan_id, file_id)
    if not result:
        if scan:
            return redirect(url_for("batch_results", scan_id=scan_id))
        return redirect(url_for("index"))
    if not result.get("cleaned_name"):
        flash("No sanitized file is available for this result.", "warning")
        return redirect(url_for("batch_file_results", scan_id=scan_id, file_id=file_id))
    return send_from_directory(CLEANED_FOLDER, result["cleaned_name"], as_attachment=True)


@app.route("/reports/<path:report_name>")
def download_report(report_name):
    safe_name = secure_filename(report_name)
    if safe_name != report_name:
        flash("Invalid report name.", "danger")
        return redirect(url_for("index"))
    return send_from_directory(REPORT_FOLDER, safe_name, as_attachment=True)


@app.route("/cleaned/<path:cleaned_name>")
def download_cleaned(cleaned_name):
    safe_name = secure_filename(cleaned_name)
    if safe_name != cleaned_name:
        flash("Invalid cleaned file name.", "danger")
        return redirect(url_for("index"))
    return send_from_directory(CLEANED_FOLDER, safe_name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
