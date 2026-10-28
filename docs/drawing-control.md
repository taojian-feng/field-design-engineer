# Finding and verifying drawings

Field Design Engineers lose hours every week to two jobs:

1. **Finding the drawing.** The alignment sheet is in document control, the valve GA is in a vendor portal or an
   e-mail transmittal, the GIS feature links to a revision nobody remembers, and the copy the crew has came from a
   shared drive folder called `Field Packages (old)`. Document numbers are typed five different ways.
2. **Trusting the drawing.** Is this the current revision? Does the vendor drawing match what we designed? Was the
   as-built ever reconciled with PODS? Is the part number on the design the one procurement ordered?

The `fde_agent.drawings` package does both. It reads every source into one index, works out the controlled current
revision of each drawing, and runs the cross-checks an experienced FDE would run by hand.

## How it fits together

```
 document control   vendor submittals   GIS feature links   shared drives / project folders
 (EDMS register)    (submittal log)     (drawing_number,    (any file tree; doc number and
        │                  │             drawing_rev)        rev from title block + file name)
        ▼                  ▼                  ▼                       ▼
 EdmsRegisterSource  VendorSubmittalSource  GisLinkSource         FolderSource(extractors=...)
        └──────────────────┴────────┬─────────┴───────────────────────┘
                                    ▼  DrawingRecord (a copy of a revision) / DrawingRef (a citation)
                               DrawingIndex
              families keyed by normalized document number ("SYN101_al_1" == "SYN-101-AL-001")
              current revision = latest live revision in the system of record
                     │                   │                     │
               search / get          verify()            field_package()
                                 + PodsStore records
```

| Piece | File | What it does |
|---|---|---|
| Data model | `drawings/model.py` | `DrawingRecord`, `DrawingRef`, document number and revision normalization (`Rev B`, `R1`, `rev.0`), revision ordering (A, B, ... before 0, 1, ...), title block parser |
| Source adapters | `drawings/sources.py` | One class per source. Each yields records and references; nothing else in the agent knows where a drawing came from |
| Index | `drawings/index.py` | Groups every copy into document families, picks the current revision, searches by number, keywords, pipeline, station range, type or asset tag |
| Checks | `drawings/verify.py` | Revision control, copy control, design vs vendor, drawings vs asset records; `compare()` for field-by-field diffs; `field_package()` for a crew's drawing set |
| Synthetic sources | `drawings/synth.py` | Builds all four sources for the synthetic system with planted problems and an answer key |

## Checks

| Finding | Severity | What it means | Found by comparing |
|---|---|---|---|
| `superseded_reference` | high | GIS (or another drawing) still points at an old revision | citation rev vs controlled current rev |
| `uncontrolled_revision` | high | A newer revision exists only on a shared drive (field redline never checked in) | copy rev vs controlled revs |
| `conflicting_copy` | high | Same revision number, different content | content fingerprint of copy vs controlled file |
| `vendor_rating_mismatch` | high | Vendor GA shows a different pressure class / rating than the design | design valve data vs cited vendor revision |
| `vendor_not_approved` | high | Design relies on a vendor drawing that was rejected or sent back | submittal log status of the cited vendor revision |
| `record_mismatch` | high | Current drawing disagrees with PODS (wall, grade, seam, station, rating) | drawing data block vs `pipe_segment` / `point_feature` |
| `part_number_mismatch` | medium | Design part number differs from the vendor drawing | design vs vendor data block |
| `broken_reference` | medium | A link points to a document number no source has (usually a typo) | citations vs index |
| `title_block_mismatch` | medium | Register or file name says one revision, the title block another | metadata vs parsed title block |
| `revision_gap` | medium | The controlled history skips a revision | controlled revision sequence |
| `stale_copy` | info | An old revision is sitting on a shared drive | copy rev vs current rev |

Every finding carries the document, revision, source, file location, a plain-language detail and the recommended
action, so the agent can turn them straight into a hold list, an RFI or a document-control request.

## Agent tools

| Tool | Use it for |
|---|---|
| `search_drawings` | "Find the valve detail for SYN-101-V02", "what drawings cover station 200+00 to 260+00", messy numbers like `syn101 vd 2` |
| `get_drawing` | Revision history, current data, every copy and everything that cites it |
| `compare_drawings` | Rev 0 vs rev 1, design vs vendor GA, a shared-drive copy vs the controlled file |
| `verify_drawings` | All checks for a line, a station range or one document |
| `drawing_package` | The verified set for a field crew: current revision and location of each drawing, vendor drawings with submittal status, blocking issues, and copies not to use |

Without the LLM: `python -m fde_agent.drawings data/synthetic/drawings --data data/synthetic` prints the index size,
time taken and every finding as a markdown table.

## Connecting real sources

Write a class with `name`, `records()` and `refs()` and pass it to `DrawingIndex([...], store)`. Typical mappings:

| Real system | Adapter approach |
|---|---|
| ProjectWise, Meridian, OpenText, SharePoint document library | Pull the register (doc number, rev, status, dates, file URL) from the API or a scheduled export; mark records `controlled=True`. Download files lazily, only when a check needs the content |
| Vendor portal, Aconex, Procore submittals | Map each submittal revision to a record with `status` = review code (Approved, Approved as Noted, Resubmit, Rejected) and `meta.tag` = equipment tag |
| ArcGIS / PODS / Utility Network | Query layers for features that carry drawing-number and revision attributes; yield `DrawingRef`s |
| Shared drives, OneDrive, Teams folders | `FolderSource` as is, with extractors for the file types you have |

Extraction is pluggable per file type. Plain text is built in. For production add `{".pdf": pdf_text}` with
pdfplumber for vector PDFs, a CAD attribute export for DWG/DGN title blocks, and OCR or a Claude vision call for
scanned drawings that returns the same title block keys and data rows. Real data will need two more pieces of
configuration: the operator's document numbering pattern (`FILENAME_DOC`) and a field mapping from each drawing
type's data block to PODS columns (`SEGMENT_FIELDS`, `FEATURE_FIELDS`, `VENDOR_FIELDS`).

## Next steps

- Read title blocks and BOM tables from PDFs and scans (vision extraction), with a confidence score per field.
- Persist the index (SQLite) and refresh incrementally from source change feeds instead of re-reading everything.
- Watch mode: re-run checks when a source changes and post new high-severity findings to the engineer.
- Draft the document-control request, vendor RFI or GIS correction for each finding, approval-gated like asset writes.
