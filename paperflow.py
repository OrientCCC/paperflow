from __future__ import annotations

import argparse
import json
import logging
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import pandas as pd
from pypaperretriever import PaperRetriever

LOG_FORMAT = "%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s"
SUMMARY_SEPARATOR = "-" * 60
DEFAULT_RANDOM_DELAY = (0.8, 2.0)
NETWORK_FAILURE_THRESHOLD = 10
CHECKPOINT_INTERVAL = 50
FILE_BASENAME_FIELD = "File_Basename"


def sanitize_filename(filename: str) -> str:
    """Return a filesystem-safe filename."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", filename or "")
    sanitized = sanitized.strip(" .")
    return sanitized[:200] if len(sanitized) > 200 else sanitized


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    """Normalize DOI values by removing prefixes, whitespace, and punctuation."""
    if doi is None or pd.isna(doi):
        return None

    doi_str = str(doi).strip().lower()
    if doi_str in {"", "nan", "none"}:
        return None

    doi_str = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi_str)
    doi_str = doi_str.rstrip(".,;")
    return doi_str or None


def normalize_pmid(pmid: Optional[str]) -> Optional[str]:
    """Return a numeric PMID string or None if the input is invalid."""
    if pmid is None or pd.isna(pmid):
        return None

    if isinstance(pmid, float):
        pmid_str = str(int(pmid))
    else:
        pmid_str = str(pmid).strip()

    return pmid_str if pmid_str.isdigit() else None


def validate_year(value: Optional[int], year_range: Tuple[int, int]) -> Optional[int]:
    """Validate publication year using the configured range."""
    if value is None or pd.isna(value):
        return None

    try:
        year = int(value)
    except (TypeError, ValueError):
        return None

    minimum, maximum = year_range
    return year if minimum <= year <= maximum else None


class ErrorType(Enum):
    """Download error classification used for retry logic."""

    PERMANENT = "PERMANENT"
    TEMPORARY = "TEMPORARY"
    RATE_LIMIT = "RATE_LIMIT"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class AppConfig:
    """Runtime configuration with sensible defaults for production use."""

    excel_file: Path = Path("papers.xlsx")
    download_dir: Path = Path("downloads")
    log_dir: Path = Path("logs")
    failed_csv: Path = Path("failed_to_download.csv")
    success_csv: Path = Path("successful_downloads.csv")
    checkpoint_csv: Path = Path("checkpoint.csv")
    identifier_column: str = "UT (Unique WOS ID)"
    email: Optional[str] = None
    allow_scihub: bool = True
    max_workers: int = 3
    request_interval: float = 1.2
    random_delay: Tuple[float, float] = DEFAULT_RANDOM_DELAY
    max_retries: int = 3
    retry_backoff_base: int = 2
    rate_limit_backoff: int = 30
    cleanup_original: bool = True
    skip_existing: bool = True
    validate_year_range: Tuple[int, int] = (1900, 2030)
    use_chunked_processing: bool = False
    chunk_size: int = 1000
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.refresh_runtime_paths()

    def refresh_runtime_paths(self) -> None:
        """Regenerate paths that depend on runtime state (e.g., timestamped logs)."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"download_{timestamp}.log"


def configure_logging(log_file: Path) -> logging.Logger:
    """Configure a logger that writes to both file and console."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("paper_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


class RateLimiter:
    """Simple token bucket rate limiter with optional random jitter."""

    def __init__(self, min_interval: float, random_delay: Tuple[float, float]) -> None:
        self.min_interval = max(min_interval, 0.0)
        self.random_delay = random_delay
        self.last_request = 0.0
        self.lock = Lock()

    def acquire(self) -> None:
        """Block until the next request slot is available."""
        sleep_time = 0.0
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
            self.last_request = now + sleep_time

        if sleep_time > 0:
            time.sleep(sleep_time)

        lower, upper = self.random_delay
        if lower < 0 or upper < 0 or lower > upper:
            return
        time.sleep(random.uniform(lower, upper))


class DownloadStats:
    """Thread-safe tracker for download metrics and records."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.total_valid = 0
        self.total_invalid = 0
        self.total_duplicates = 0
        self.success = 0
        self.failed = 0
        self.skipped_existing = 0
        self.skipped_checkpoint = 0
        self.success_records: List[Dict[str, object]] = []
        self.failed_records: List[Dict[str, object]] = []
        self.year_distribution: Dict[str, int] = defaultdict(int)
        self.source_distribution: Dict[str, int] = defaultdict(int)
        self.error_distribution: Dict[str, int] = defaultdict(int)
        self.consecutive_failures = 0
        self.max_consecutive_failures = 0
        self.dirs_to_cleanup: Set[Path] = set()
        self.checkpoint_save_count = 0
        self.track_year_distribution = True

    def register_invalid(self) -> None:
        with self.lock:
            self.total_invalid += 1

    def register_duplicate(self) -> None:
        with self.lock:
            self.total_duplicates += 1

    def set_total_valid(self, count: int) -> None:
        with self.lock:
            self.total_valid = count

    def add_success(self, record: Dict[str, object]) -> None:
        with self.lock:
            self.success += 1
            self.success_records.append(record)
            if self.track_year_distribution and "Publication Year" in record:
                year = str(record.get("Publication Year", "Unknown"))
                self.year_distribution[year] += 1
            source = record.get("Source", "Unknown")
            self.source_distribution[str(source)] += 1
            self.consecutive_failures = 0

    def add_failed(self, record: Dict[str, object]) -> None:
        with self.lock:
            self.failed += 1
            self.failed_records.append(record)
            error_type = str(record.get("Error Type", "Unknown"))
            self.error_distribution[error_type] += 1
            self.consecutive_failures += 1
            self.max_consecutive_failures = max(
                self.max_consecutive_failures, self.consecutive_failures
            )

    def add_skipped_existing(self) -> None:
        with self.lock:
            self.skipped_existing += 1

    def add_skipped_checkpoint(self) -> None:
        with self.lock:
            self.skipped_checkpoint += 1

    def get_download_progress(self) -> Tuple[int, int]:
        with self.lock:
            downloaded = self.success + self.failed
            return downloaded, self.total_valid

    def add_dir_to_cleanup(self, directory: Path) -> None:
        with self.lock:
            self.dirs_to_cleanup.add(directory)

    def collect_dirs_to_cleanup(self) -> List[Path]:
        with self.lock:
            dirs = list(self.dirs_to_cleanup)
            self.dirs_to_cleanup.clear()
            return dirs

    def snapshot_success_records(self) -> List[Dict[str, object]]:
        with self.lock:
            return list(self.success_records)

    def snapshot_failed_records(self) -> List[Dict[str, object]]:
        with self.lock:
            return list(self.failed_records)

    def increment_checkpoint_counter(self) -> int:
        with self.lock:
            self.checkpoint_save_count += 1
            return self.checkpoint_save_count

    def check_network_issue(self) -> bool:
        with self.lock:
            return self.consecutive_failures >= NETWORK_FAILURE_THRESHOLD

    def print_summary(self, logger: logging.Logger) -> None:
        with self.lock:
            total_records = self.total_valid + self.total_invalid + self.total_duplicates
            success_pct = (self.success / self.total_valid * 100) if self.total_valid else 0.0
            failure_pct = (self.failed / self.total_valid * 100) if self.total_valid else 0.0

            logger.info(SUMMARY_SEPARATOR)
            logger.info("Download Statistics Summary")
            logger.info(SUMMARY_SEPARATOR)
            logger.info(
                "Total records: %d (valid=%d, invalid=%d, duplicates=%d)",
                total_records,
                self.total_valid,
                self.total_invalid,
                self.total_duplicates,
            )
            logger.info(
                "Processed: success=%d (%.2f%%) | failed=%d (%.2f%%)",
                self.success,
                success_pct,
                self.failed,
                failure_pct,
            )
            logger.info(
                "Skipped: existing=%d | checkpoint=%d | max consecutive failures=%d",
                self.skipped_existing,
                self.skipped_checkpoint,
                self.max_consecutive_failures,
            )

            if self.track_year_distribution and self.year_distribution:
                logger.info("Year distribution (successful downloads):")
                for year in sorted(self.year_distribution):
                    logger.info("  %s: %d", year, self.year_distribution[year])

            if self.source_distribution:
                logger.info("Source distribution (successful downloads):")
                for source, count in sorted(
                    self.source_distribution.items(), key=lambda item: item[1], reverse=True
                ):
                    logger.info("  %s: %d", source, count)

            if self.error_distribution:
                logger.info("Error distribution (failed downloads):")
                for error_type, count in sorted(
                    self.error_distribution.items(), key=lambda item: item[1], reverse=True
                ):
                    logger.info("  %s: %d", error_type, count)

            logger.info(SUMMARY_SEPARATOR)


class DownloadManager:
    """High-level orchestrator for batch paper downloads."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = configure_logging(config.log_file)
        self.stats = DownloadStats()
        self.rate_limiter = RateLimiter(config.request_interval, config.random_delay)
        self.year_column = "Publication Year"
        self.year_column_available = True

    def run(self) -> None:
        start_time = time.time()
        self.logger.info("Paper batch downloader started")
        self.logger.info("Input file: %s", self.config.excel_file)
        self.logger.info("Download directory: %s", self.config.download_dir)
        self.logger.info("Workers: %d", self.config.max_workers)
        self.logger.info("Allow Sci-Hub: %s", self.config.allow_scihub)
        self.logger.info("Skip existing files: %s", self.config.skip_existing)
        self.logger.info("Filename column: %s", self.config.identifier_column)

        dataframe = self._read_input()
        if self.config.identifier_column not in dataframe.columns:
            self.logger.error(
                "Identifier column '%s' not found in the input file.",
                self.config.identifier_column,
            )
            raise SystemExit(1)

        self.year_column_available = self.year_column in dataframe.columns
        self.stats.track_year_distribution = self.year_column_available
        if not self.year_column_available:
            self.logger.info(
                "Column '%s' not found. Year-based statistics will be skipped.",
                self.year_column,
            )

        valid_rows = self._prepare_rows(dataframe)
        if not valid_rows:
            self.logger.error("No valid records found. Nothing to download.")
            raise SystemExit(1)

        total_records = (
            self.stats.total_valid + self.stats.total_invalid + self.stats.total_duplicates
        )
        self.logger.info(
            "Records: total=%d | valid=%d | invalid=%d | duplicates=%d",
            total_records,
            self.stats.total_valid,
            self.stats.total_invalid,
            self.stats.total_duplicates,
        )

        completed_basenames = self._load_completed_basenames()
        self.logger.info(
            "Loaded %d completed records from checkpoints", len(completed_basenames)
        )

        self.config.download_dir.mkdir(parents=True, exist_ok=True)

        checkpoint_baseline = 0
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = {
                executor.submit(self._download_paper, row_data, completed_basenames): row_data[0]
                for row_data in valid_rows
            }
            for future in as_completed(futures):
                try:
                    future.result()
                    current_success = self.stats.success
                    if current_success - checkpoint_baseline >= CHECKPOINT_INTERVAL:
                        if self.save_checkpoint():
                            checkpoint_baseline = current_success
                except Exception as exc:
                    self.logger.error("Task execution error: %s", exc, exc_info=True)

        self.cleanup_directories()
        self.save_checkpoint()
        self._write_results()

        elapsed = time.time() - start_time
        minutes = elapsed / 60 if elapsed else 0
        self.logger.info("Processing finished in %.2f minutes", minutes)

        downloaded, total_valid = self.stats.get_download_progress()
        if minutes and downloaded:
            self.logger.info("Download throughput: %.2f papers/minute", downloaded / minutes)
        if minutes:
            processed = downloaded + self.stats.skipped_existing + self.stats.skipped_checkpoint
            if processed:
                self.logger.info("Overall throughput: %.2f records/minute", processed / minutes)

        self.logger.info("Detailed log: %s", self.config.log_file)
        self.stats.print_summary(self.logger)

    def _read_input(self) -> pd.DataFrame:
        input_path = self.config.excel_file
        try:
            suffix = input_path.suffix.lower()
            if suffix == ".csv":
                dataframe = pd.read_csv(input_path)
            else:
                dataframe = pd.read_excel(input_path)
        except Exception as exc:
            self.logger.error("Unable to read input file %s: %s", input_path, exc)
            raise SystemExit(1) from exc

        self.logger.info(
            "Loaded %d rows. Columns: %s", len(dataframe), list(dataframe.columns)
        )
        return dataframe

    def _prepare_rows(self, dataframe: pd.DataFrame) -> List[Tuple[int, pd.Series]]:
        valid_rows: List[Tuple[int, pd.Series]] = []
        seen_basenames: Set[str] = set()
        identifier_field = self.config.identifier_column

        for index, row in dataframe.iterrows():
            doi = normalize_doi(row.get("DOI"))
            identifier_value = row.get(identifier_field)

            if not doi or identifier_value is None or pd.isna(identifier_value):
                self.stats.register_invalid()
                self.logger.debug(
                    "Row %s skipped (missing DOI or %s)", index + 1, identifier_field
                )
                continue

            identifier_str = str(identifier_value).strip()
            if not identifier_str:
                self.stats.register_invalid()
                self.logger.debug(
                    "Row %s skipped (empty %s value)", index + 1, identifier_field
                )
                continue

            file_basename = sanitize_filename(identifier_str)
            if not file_basename:
                self.stats.register_invalid()
                self.logger.debug(
                    "Row %s skipped (unable to sanitize %s value '%s')",
                    index + 1,
                    identifier_field,
                    identifier_str,
                )
                continue

            if file_basename in seen_basenames:
                self.stats.register_duplicate()
                self.logger.debug(
                    "Duplicate %s skipped: %s", identifier_field, file_basename
                )
                continue

            seen_basenames.add(file_basename)
            valid_rows.append((index, row))

        self.stats.set_total_valid(len(valid_rows))
        return valid_rows

    def _load_completed_basenames(self) -> Set[str]:
        completed: Set[str] = set()
        candidate_columns = [FILE_BASENAME_FIELD, "UT_Sanitized"]

        checkpoint_path = self.config.checkpoint_csv
        if checkpoint_path.exists():
            try:
                df = pd.read_csv(checkpoint_path)
                for column in candidate_columns:
                    if column in df.columns:
                        stripped_values = [
                            item.strip()
                            for item in df[column].dropna().astype(str)
                        ]
                        completed.update(value for value in stripped_values if value)
            except Exception as exc:
                self.logger.warning("Failed to load checkpoint %s: %s", checkpoint_path, exc)

        success_path = self.config.success_csv
        if success_path.exists():
            try:
                df = pd.read_csv(success_path)
                for column in candidate_columns:
                    if column in df.columns:
                        stripped_values = [
                            item.strip()
                            for item in df[column].dropna().astype(str)
                        ]
                        completed.update(value for value in stripped_values if value)
            except Exception as exc:
                self.logger.warning("Failed to load success records %s: %s", success_path, exc)

        return completed

    def save_checkpoint(self) -> bool:
        records = self.stats.snapshot_success_records()
        if not records:
            return False

        df = pd.DataFrame(records)
        df.to_csv(self.config.checkpoint_csv, index=False, encoding="utf-8-sig")
        checkpoint_number = self.stats.increment_checkpoint_counter()
        self.logger.debug(
            "Checkpoint #%d saved (%d records) -> %s",
            checkpoint_number,
            len(records),
            self.config.checkpoint_csv,
        )
        return True

    def cleanup_directories(self) -> None:
        if not self.config.cleanup_original:
            return

        dirs_to_remove = self.stats.collect_dirs_to_cleanup()
        if not dirs_to_remove:
            return

        removed = 0
        failed = 0
        for directory in dirs_to_remove:
            try:
                if directory.exists() and directory.is_dir():
                    shutil.rmtree(directory)
                    removed += 1
            except Exception as exc:
                failed += 1
                self.logger.debug("Failed to remove %s: %s", directory, exc)

        self.logger.info(
            "Temporary directory cleanup complete: %d removed, %d failed", removed, failed
        )

    def _download_paper(
        self,
        row_data: Tuple[int, pd.Series],
        completed_basenames: Set[str],
    ) -> Optional[Dict[str, str]]:
        index, row = row_data
        doi = normalize_doi(row.get("DOI"))
        pmid = normalize_pmid(row.get("Pubmed Id"))
        identifier_field = self.config.identifier_column
        identifier_value = row.get(identifier_field)
        year = (
            validate_year(row.get(self.year_column), self.config.validate_year_range)
            if self.year_column_available
            else None
        )
        title = row.get("Article Title", "No Title")

        if not doi or identifier_value is None or pd.isna(identifier_value):
            self.logger.debug(
                "Row %s skipped (missing DOI or %s)", index + 1, identifier_field
            )
            return None

        identifier_str = str(identifier_value).strip()
        if not identifier_str:
            self.logger.debug(
                "Row %s skipped (empty %s value)", index + 1, identifier_field
            )
            return None

        file_basename = sanitize_filename(identifier_str)
        if not file_basename:
            self.logger.debug(
                "Row %s skipped (unable to sanitize %s value '%s')",
                index + 1,
                identifier_field,
                identifier_str,
            )
            return None

        if file_basename in completed_basenames:
            self.logger.debug("Skipped %s (already completed)", file_basename)
            self.stats.add_skipped_checkpoint()
            return None

        target_pdf = self.config.download_dir / f"{file_basename}.pdf"
        if self.config.skip_existing and target_pdf.exists():
            self.logger.debug("Skipped %s (existing PDF)", file_basename)
            self.stats.add_skipped_existing()
            return None

        downloaded, total = self.stats.get_download_progress()
        self.logger.info("[Downloading %d/%d] %s", downloaded + 1, total, file_basename)
        self.logger.debug("DOI=%s PMID=%s Year=%s", doi, pmid, year)

        retriever: Optional[PaperRetriever] = None
        error_type: ErrorType = ErrorType.TEMPORARY
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                self.rate_limiter.acquire()
                if retriever is None:
                    retriever = PaperRetriever(
                        email=self.config.email,
                        doi=doi,
                        pmid=pmid,
                        download_directory=str(self.config.download_dir),
                        allow_scihub=self.config.allow_scihub,
                    )

                retriever.download()
                success, metadata = self._rename_and_extract_metadata(doi, file_basename)

                if success:
                    record: Dict[str, object] = {
                        identifier_field: identifier_str,
                        FILE_BASENAME_FIELD: file_basename,
                        "DOI": doi,
                        "PMID": pmid,
                        "Article Title": title,
                        "Source": metadata.get("source", "Unknown"),
                        "Source_Domain": metadata.get("domain", ""),
                        "Is_OA": metadata.get("is_oa", False),
                        "Via_SciHub": metadata.get("via_scihub", False),
                        "Download_Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "Final_Filename": f"{file_basename}.pdf",
                    }
                    if self.year_column_available:
                        record["Publication Year"] = year if year else "Unknown"

                    self.logger.info(
                        "SUCCESS: %s (%s @ %s)",
                        record["Final_Filename"],
                        record["Source"],
                        record.get("Source_Domain", ""),
                    )
                    self.stats.add_success(record)
                    return {"status": "success", "basename": file_basename}

                raise RuntimeError("PDF file not found or rename failed")

            except Exception as exc:
                last_error = exc
                error_type = self._classify_error(str(exc))
                self.logger.debug(
                    "Attempt %d/%d for %s failed: %s (%s)",
                    attempt + 1,
                    self.config.max_retries,
                    file_basename,
                    exc,
                    error_type.value,
                )

                if error_type in {ErrorType.PERMANENT, ErrorType.ACCESS_DENIED}:
                    self.logger.error(
                        "FAILED: %s - %s: %s", file_basename, error_type.value, exc
                    )
                    break

                if error_type == ErrorType.RATE_LIMIT:
                    self.logger.warning(
                        "Rate limit triggered, waiting %s seconds", self.config.rate_limit_backoff
                    )
                    time.sleep(self.config.rate_limit_backoff)
                    continue

                if error_type == ErrorType.TEMPORARY and attempt < self.config.max_retries - 1:
                    backoff_time = self.config.retry_backoff_base ** attempt
                    self.logger.debug("Temporary error, retrying in %s seconds", backoff_time)
                    time.sleep(backoff_time)
                else:
                    self.logger.error("FAILED: %s - maximum retries reached", file_basename)

        if self.stats.check_network_issue():
            self.logger.warning(
                "Consecutive failures detected. Please verify network / VPN / proxy settings."
            )

        failure_record: Dict[str, object] = {
            identifier_field: identifier_str,
            FILE_BASENAME_FIELD: file_basename,
            "DOI": doi,
            "PMID": pmid,
            "Article Title": title,
            "Error": str(last_error) if last_error else "Unknown error",
            "Error Type": error_type.value,
            "Retry_Count": self.config.max_retries,
            "Retry_Time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.year_column_available:
            failure_record["Publication Year"] = year if year else "Unknown"

        self.stats.add_failed(failure_record)
        return {"status": "failed", "basename": file_basename}

    def _rename_and_extract_metadata(self, doi: str, file_basename: str) -> Tuple[bool, Dict[str, object]]:
        download_dir = self.config.download_dir
        metadata: Dict[str, object] = {"source": "Unknown", "domain": "", "is_oa": False, "via_scihub": False}

        if not download_dir.exists():
            return False, metadata

        potential_dirs: List[Path] = []
        doi_safe = doi.replace("/", "_").replace(".", "_")
        potential_dirs.append(download_dir / f"doi-{doi_safe}")

        try:
            for item in download_dir.iterdir():
                if item.is_dir() and item.name.startswith("pmid-"):
                    potential_dirs.append(item)

            recent_dirs = sorted(
                (d for d in download_dir.iterdir() if d.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:5]
            potential_dirs.extend(recent_dirs)
        except FileNotFoundError:
            return False, metadata
        except Exception as exc:
            self.logger.debug("Failed to enumerate download directory: %s", exc)

        seen_dirs: Set[Path] = set()
        for dir_path in potential_dirs:
            if dir_path in seen_dirs:
                continue
            seen_dirs.add(dir_path)
            if not dir_path.exists() or not dir_path.is_dir():
                continue

            for json_file in dir_path.glob("*.json"):
                try:
                    with json_file.open("r", encoding="utf-8") as handle:
                        json_data = json.load(handle)
                except Exception as exc:
                    self.logger.debug("Failed to parse %s: %s", json_file, exc)
                    continue

                if not json_data.get("download_success"):
                    continue

                if json_data.get("doi", "").lower() != doi.lower():
                    continue

                pdf_path = Path(json_data.get("pdf_filepath", ""))
                if not pdf_path.exists():
                    continue

                source_url = json_data.get("source_url", "") or ""
                metadata["is_oa"] = bool(json_data.get("open_access", False))
                metadata["domain"] = urlparse(source_url).netloc if source_url else ""
                metadata["source"] = self._determine_source(source_url)
                metadata["via_scihub"] = "scihub" in source_url.lower() or "sci-hub" in source_url.lower()

                target_path = self.config.download_dir / f"{file_basename}.pdf"
                if target_path.exists():
                    return True, metadata

                try:
                    if pdf_path.parent != self.config.download_dir:
                        temp_path = target_path.with_suffix(".tmp")
                        shutil.copy2(pdf_path, temp_path)
                        temp_path.replace(target_path)
                    else:
                        pdf_path.replace(target_path)
                except FileExistsError:
                    return True, metadata
                except Exception as exc:
                    self.logger.debug(
                        "Move failed for %s: %s. Falling back to copy.", file_basename, exc
                    )
                    shutil.copy2(pdf_path, target_path)

                if self.config.cleanup_original and pdf_path.parent != self.config.download_dir:
                    self.stats.add_dir_to_cleanup(pdf_path.parent)

                return True, metadata

        return False, metadata

    @staticmethod
    def _determine_source(source_url: str) -> str:
        url_lower = source_url.lower()
        if "scihub" in url_lower or "sci-hub" in url_lower:
            return "Sci-Hub"
        if "europepmc" in url_lower:
            return "Europe PMC"
        if "ncbi.nlm.nih.gov/pmc" in url_lower or "pmc.ncbi" in url_lower:
            return "PMC"
        if "arxiv" in url_lower:
            return "arXiv"
        if "unpaywall" in url_lower:
            return "Unpaywall"
        if "crossref" in url_lower:
            return "Crossref"
        return "Other" if source_url else "Unknown"

    @staticmethod
    def _classify_error(message: str) -> ErrorType:
        error_lower = message.lower()
        if any(keyword in error_lower for keyword in ["access denied", "paywall", "forbidden", "401", "403"]):
            return ErrorType.ACCESS_DENIED
        if any(keyword in error_lower for keyword in ["429", "rate limit", "too many requests", "throttle"]):
            return ErrorType.RATE_LIMIT
        if any(keyword in error_lower for keyword in ["404", "not found", "invalid doi", "unavailable"]):
            return ErrorType.PERMANENT
        if any(keyword in error_lower for keyword in ["timeout", "connection", "timed out", "503", "502", "500", "refused", "reset"]):
            return ErrorType.TEMPORARY
        return ErrorType.TEMPORARY

    def _write_results(self) -> None:
        success_records = self.stats.snapshot_success_records()
        if success_records:
            pd.DataFrame(success_records).to_csv(
                self.config.success_csv, index=False, encoding="utf-8-sig"
            )
            self.logger.info(
                "Success records saved to %s (%d entries)",
                self.config.success_csv,
                len(success_records),
            )
        else:
            self.logger.info("No successful downloads recorded.")

        failed_records = self.stats.snapshot_failed_records()
        if failed_records:
            pd.DataFrame(failed_records).to_csv(
                self.config.failed_csv, index=False, encoding="utf-8-sig"
            )
            self.logger.info(
                "Failed records saved to %s (%d entries)",
                self.config.failed_csv,
                len(failed_records),
            )
        else:
            self.logger.info("No failed downloads recorded.")

        if self.config.checkpoint_csv.exists():
            self.logger.info("Checkpoint available at %s", self.config.checkpoint_csv)


def parse_random_delay(value: str) -> Tuple[float, float]:
    try:
        lower_str, upper_str = value.split(",", 1)
        lower = float(lower_str.strip())
        upper = float(upper_str.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Random delay must follow 'min,max' format.") from exc

    if lower < 0 or upper < 0 or lower > upper:
        raise argparse.ArgumentTypeError("Random delay requires non-negative values with min <= max.")
    return lower, upper


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch download papers using pypaperretriever with production safeguards.",
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        required=True,
        help="Input data file (Excel or CSV)",
    )
    parser.add_argument("--output", "-o", type=Path, required=True, help="Directory for downloaded PDFs")
    parser.add_argument("--email", "-e", required=True, help="Email address used for API requests")
    parser.add_argument(
        "--id-column",
        "-c",
        required=True,
        help="Column name used to derive PDF filenames",
    )
    parser.add_argument("--workers", "-w", type=int, help="Number of parallel workers")
    parser.add_argument("--rate-limit", "-r", type=float, help="Minimum seconds between requests")
    parser.add_argument("--random-delay", type=parse_random_delay, help="Random delay range, e.g. '0.5,1.5'")
    parser.add_argument("--max-retries", type=int, help="Maximum retry attempts per paper")
    parser.add_argument("--no-scihub", action="store_true", help="Disable Sci-Hub usage")
    parser.add_argument("--no-skip-existing", action="store_true", help="Re-download PDFs even if they exist")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep intermediate download directories")
    parser.add_argument("--log-dir", type=Path, help="Directory for log files (default: logs)")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> AppConfig:
    kwargs: Dict[str, object] = {}
    if args.input:
        kwargs["excel_file"] = args.input
    if args.output:
        kwargs["download_dir"] = args.output
    if args.log_dir:
        kwargs["log_dir"] = args.log_dir
    if args.email is not None:
        kwargs["email"] = args.email
    if args.id_column is not None:
        kwargs["identifier_column"] = args.id_column
    if args.workers is not None:
        kwargs["max_workers"] = args.workers
    if args.rate_limit is not None:
        kwargs["request_interval"] = args.rate_limit
    if args.max_retries is not None:
        kwargs["max_retries"] = args.max_retries
    if args.random_delay is not None:
        kwargs["random_delay"] = args.random_delay

    config = AppConfig(**kwargs)
    config.allow_scihub = not args.no_scihub
    config.skip_existing = not args.no_skip_existing
    config.cleanup_original = not args.no_cleanup

    config.excel_file = config.excel_file.expanduser()
    config.download_dir = config.download_dir.expanduser()
    config.log_dir = config.log_dir.expanduser()
    config.failed_csv = config.failed_csv.expanduser()
    config.success_csv = config.success_csv.expanduser()
    config.checkpoint_csv = config.checkpoint_csv.expanduser()
    config.identifier_column = config.identifier_column.strip()
    if not config.identifier_column:
        raise SystemExit("Identifier column name cannot be empty.")
    config.refresh_runtime_paths()
    return config


def main() -> None:
    args = parse_cli_args()
    config = build_config(args)
    manager = DownloadManager(config)
    try:
        manager.run()
    except KeyboardInterrupt:
        manager.logger.info("Interrupted by user. Saving checkpoint before exit...")
        if manager.save_checkpoint():
            manager.logger.info("Checkpoint saved to %s", manager.config.checkpoint_csv)
        raise SystemExit(0)
    except Exception as exc:
        manager.logger.error("Program terminated with error: %s", exc, exc_info=True)
        manager.save_checkpoint()
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
