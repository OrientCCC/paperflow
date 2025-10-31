# PaperFlow

PaperFlow is a production-oriented batch downloader that wraps
[`pypaperretriever`](https://pypi.org/project/pypaperretriever/) with extra guardrails for
high-volume paper retrieval workflows. It adds configuration management, resilient retry
logic, progress checkpoints, logging, and flexible file naming so teams can automate PDF
collection from heterogeneous metadata sources.

## Key Features

- **Flexible input formats** &mdash; consume either Excel (`.xlsx`, `.xls`) or CSV files.
- **Custom filename strategy** &mdash; choose any metadata column (e.g. `UT`, `PMID`, `DOI`)
  as the final PDF basename.
- **Thread-safe concurrency** &mdash; configurable worker pool with global rate limiting and
  jitter to stay within provider quotas.
- **Smart retry classification** &mdash; distinguishes rate limiting, access denial, temporary
  network issues, and permanent failures to apply the right backoff policy.
- **Checkpointing and resume** &mdash; incrementally saves successful downloads so interrupted
  runs can continue without duplication.
- **Comprehensive logging** &mdash; structured console output plus timestamped log files and
  CSV exports for auditing success and failure cases.

## Requirements

- Python 3.9 or newer
- `pypaperretriever` and dependencies listed in `requirements.txt` (install with
  `pip install -r requirements.txt`)
- A valid contact email for services that require it (e.g. Crossref, Unpaywall)
- Optional: institutional access or VPN when attempting to fetch paywalled content

## Installation

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Or with Conda:

```bash
conda create -n paperflow python=3.10 -y
conda activate paperflow
pip install -r requirements.txt
```

## Input Expectations

The input spreadsheet must contain, at minimum:

| Column | Purpose |
| --- | --- |
| `DOI` | Canonical identifier used by `pypaperretriever` to locate the paper. |
| *Custom* | Column specified via `--id-column`; its values become the final PDF filenames. |

Optional columns include `Pubmed Id`, `Publication Year`, `Article Title`, and any other
metadata you want persisted in the output CSV reports. Publication year is only used for
statistics when the column is present.

### Supported file formats

- Excel (`.xlsx`, `.xls`) &mdash; processed with `pandas.read_excel`
- CSV (`.csv`) &mdash; processed with `pandas.read_csv`

## Usage

Invoke the main entry point `paperflow.py` with the required arguments:

```bash
python paperflow.py \
  --input /path/to/input.xlsx \
  --output /path/to/downloads \
  --email you@example.com \
  --id-column "UT (Unique WOS ID)"
```

### Required arguments

| Flag | Description |
| --- | --- |
| `--input, -i` | Path to the input Excel or CSV file. |
| `--output, -o` | Directory where normalized PDFs will be written. Created if missing. |
| `--email, -e` | Contact email used by upstream APIs. |
| `--id-column, -c` | Column whose sanitized values become the PDF basenames. |

### Common optional arguments

| Flag | Default | Description |
| --- | --- | --- |
| `--workers, -w` | `3` | Number of concurrent download workers. |
| `--rate-limit, -r` | `1.2` | Minimum seconds between requests per worker. |
| `--random-delay` | `0.8,2.0` | Additional random sleep (min,max) appended to each request. |
| `--max-retries` | `3` | Maximum retry attempts for transient failures. |
| `--no-scihub` | *off* | Disable Sci-Hub lookups. |
| `--no-skip-existing` | *off* | Re-download PDFs even if a file already exists. |
| `--no-cleanup` | *off* | Keep temporary directories produced by `pypaperretriever`. |
| `--log-dir` | `logs/` | Destination for timestamped log files. |

Run `python paperflow.py --help` for the complete list.

## Output Artifacts

During execution PaperFlow generates:

| File | Description |
| --- | --- |
| `successful_downloads.csv` | All successful downloads including identifiers, metadata, and final filenames. |
| `failed_to_download.csv` | Failures with error classifications, retry counts, and timestamps. |
| `checkpoint.csv` | Rolling snapshot of successful records, enabling resume after interruption. |
| `logs/download_YYYYmmdd_HHMMSS.log` | Detailed per-request logs for auditing. |
| `<output_dir>/<basename>.pdf` | Final normalized PDFs named after the column specified via `--id-column`. |

All CSV outputs are UTF-8 encoded with BOM (`utf-8-sig`) to preserve compatibility with
Excel. Checkpoint and success CSVs are also read on start-up to avoid redundant downloads.

## Operational Tips

- Provide the most stable identifier available (e.g. DOI or UT) to `--id-column` to avoid
  collisions after sanitization. PaperFlow logs duplicates and skips them by default.
- When you expect long-running batches, leave `--skip-existing` enabled so reruns only
  download missing items.
- If the process reports consecutive network failures, verify VPN/proxy settings. The app
  pauses automatically on HTTP 429 responses to respect rate limits.
- Logs include the source domain (e.g. Crossref, Unpaywall, Sci-Hub). Use this to monitor
  reliance on different providers.

## Resuming Interrupted Runs

PaperFlow persists progress in `checkpoint.csv` and reuses existing `successful_downloads.csv`
entries. To resume work:

1. Ensure the previous output directory and CSV files remain intact.
2. Re-run the command with the same `--output` and `--id-column`.
3. The downloader will skip completed filenames automatically and pick up where it left off.

## Troubleshooting

| Symptom | Suggested Action |
| --- | --- |
| Immediate exit citing a missing identifier column | Verify the column name passed to `--id-column` matches the spreadsheet header exactly (case-sensitive). |
| High rate of access-denied errors | Confirm you have institutional rights or disable Sci-Hub (`--no-scihub`) if policy dictates. |
| All downloads fail after several successes | Check network connectivity, API quotas, or consider decreasing `--workers`/`--rate-limit`. |
| PDFs appear but JSON metadata remains | `--no-cleanup` may have been set; otherwise inspect logs to confirm directory cleanup permissions. |

## License

This project builds on `pypaperretriever`. Refer to the repository's LICENSE file for the
full terms of use.
