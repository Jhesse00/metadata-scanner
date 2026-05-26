# Project Overview

Metadata Privacy Scanner is a cybersecurity and privacy-focused web application that scans uploaded files for hidden metadata. The goal is to help users understand what personal or operational information may be embedded in files before they share those files publicly.

The app presents scan results with human-readable dates, grouped metadata categories, a risk explanation, privacy recommendations, before/after sanitizer comparisons, and downloadable TXT, JSON, and HTML reports.

## Cybersecurity Use Case

Metadata is a common source of accidental information disclosure. Images can contain GPS coordinates and camera details. PDFs and DOCX files can contain author names, usernames, software versions, creation dates, and modification history. This information can support open-source intelligence gathering, social engineering, and targeted attacks.

## Privacy Use Case

People often share files without realizing they include location data, real names, organization details, or device identifiers. This app provides a simple way to inspect files and receive a clear recommendation before posting, emailing, or publishing them.

## Secure Design Choices

- Uploaded filenames are sanitized with Werkzeug `secure_filename`.
- Only specific file extensions are accepted.
- Supported files are checked with lightweight file-signature validation before parsing.
- Uploads are limited to 10MB.
- Pillow image pixel limits help reduce decompression-bomb risk.
- Uploaded files are stored in `uploads/`, not in Flask's public static folder.
- Reports are generated with safe filenames in `reports/`.
- Cleaned files are generated with safe filenames in `cleaned/`.
- Files are parsed as data only and are never executed.
- Errors are handled with generic user-facing messages to avoid exposing internals.
- Corrupt images, PDFs, and DOCX files are handled with user-friendly messages.

## Portfolio Value

This project demonstrates practical application security skills, secure file handling, privacy risk analysis, Python web development, Docker deployment, and clear technical documentation.
