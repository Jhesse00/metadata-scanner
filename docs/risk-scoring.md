# Risk Scoring

The scanner uses a simple, explainable scoring model. Each extracted metadata field is assigned a risk category based on the privacy impact of that field.

## Low Risk

Low-risk metadata includes basic technical information that is usually not personally identifying by itself.

Examples:

- File size
- File type
- Page count
- SHA-256 hash
- Image dimensions

Low-risk fields do not increase the risk score.

## Medium Risk

Medium-risk metadata may reveal workflow, timing, or software details that could help with profiling or targeted attacks.

Examples:

- Software used
- Creation timestamps
- Modification timestamps
- PDF creator
- PDF producer

Each medium-risk field adds 15 points.

## High Risk

High-risk metadata can directly identify a person, device, or location.

Examples:

- GPS latitude
- GPS longitude
- Author name
- Last modified by
- Camera make
- Camera model

Each high-risk field adds 35 points.

## Final Risk Level

- Low Risk: no medium-risk or high-risk metadata is found.
- Medium Risk: at least one medium-risk field is found and no high-risk fields are found.
- High Risk: at least one high-risk field is found.

The final numeric score is capped at 100 to keep the result easy to understand.

## Risk Explanation

The results page includes a short explanation of why the file received its risk level. For example, GPS metadata is explained as a location risk, identity fields are explained as creator/editor exposure, and device fields are explained as camera, phone, or software exposure.

## Metadata Categories

Results are grouped into readable categories:

- Identity Metadata
- Location Metadata
- Device Metadata
- Time Metadata
- File Information
- Other Metadata

## Recommendations

- Low Risk: continue using caution when sharing files.
- Medium Risk: review timestamps, software, producer, and creator metadata before sharing.
- High Risk: remove identifying metadata such as GPS coordinates, author names, and device details before public sharing.
