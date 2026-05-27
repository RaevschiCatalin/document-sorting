from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from loguru import logger
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from docsort.classifier import classify_document
from docsort.extractor import extract_document
from docsort.models import AppConfig, ManifestRecord, ReportRecord, SummaryStats
from docsort.organizer import organize_file


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_ready(model: ManifestRecord | ReportRecord) -> dict:
    return json.loads(model.model_dump_json())


def _append_jsonl(path: Path, model: ManifestRecord | ReportRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_json_ready(model), ensure_ascii=False) + "\n")


def _load_processed_keys(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()

    keys: set[tuple[str, int]] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                keys.add((record["sha256"], int(record["source_size"])))
            except (json.JSONDecodeError, KeyError):
                continue
    return keys


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _should_skip(path: Path, config: AppConfig) -> bool:
    if not path.is_file():
        return True

    skip_roots = {
        config.output_dir.resolve(),
        config.report_path.parent.resolve(),
        config.manifest_path.parent.resolve(),
    }
    return any(_is_relative_to(path, root) for root in skip_roots)


def scan_once(config: AppConfig) -> list[ReportRecord]:
    config.input_dir.mkdir(parents=True, exist_ok=True)
    processed_keys = _load_processed_keys(config.manifest_path)
    report_records: list[ReportRecord] = []
    logger.info("Scanning {}", config.input_dir)
    candidates = [source for source in sorted(config.input_dir.rglob("*")) if not _should_skip(source, config)]
    if config.processing.max_files_per_scan is not None:
        candidates = candidates[: config.processing.max_files_per_scan]

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        transient=True,
    )
    with progress:
        task = progress.add_task("Scanning documents", total=len(candidates))
        for source in candidates:
            progress.update(task, description=f"Processing {source.name[:40]}")
            _process_source(source, config, processed_keys, report_records)
            progress.advance(task)

    write_summary(config, report_records)
    logger.info("Scan complete: {} files processed", len(report_records))
    return report_records


def _process_source(
    source: Path,
    config: AppConfig,
    processed_keys: set[tuple[str, int]],
    report_records: list[ReportRecord],
) -> None:
    file_hash = _sha256(source)
    stat = source.stat()
    processed_key = (file_hash, stat.st_size)
    if processed_key in processed_keys:
        logger.debug("Skipping already processed file: {}", source)
        return

    logger.debug("Processing {}", source.name)
    extracted = extract_document(source, config)
    try:
        classification = classify_document(extracted, config)
        target = organize_file(source, classification, config)
    except Exception as exc:  # noqa: BLE001 - keep batch scans resilient.
        logger.exception("Unexpected processing failure for {}", source)
        target_dir = config.output_dir / "pending_review"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        counter = 1
        while target.exists():
            target = target_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)
        from docsort.models import ClassificationResult

        classification = ClassificationResult(
            company_name="unknown",
            doc_type="unknown",
            confidence=0,
            needs_review=True,
            reasoning=f"Unexpected processing failure; copied to pending review: {exc}",
        )

    report = ReportRecord(
        source_path=source,
        target_path=target,
        company_name=classification.company_name,
        doc_type=classification.doc_type,
        confidence=classification.confidence,
        needs_review=classification.needs_review,
        reasoning=classification.reasoning,
        extraction_error=extracted.extraction_error,
    )
    manifest = ManifestRecord(
        source_path=source,
        source_size=stat.st_size,
        source_mtime=stat.st_mtime,
        sha256=file_hash,
        target_path=target,
    )

    _append_jsonl(config.report_path, report)
    _append_jsonl(config.manifest_path, manifest)
    processed_keys.add(processed_key)
    report_records.append(report)


def build_summary(records: list[ReportRecord]) -> SummaryStats:
    summary = SummaryStats(
        processed=len(records),
        needs_review=sum(record.needs_review for record in records),
        unknown=sum(record.company_name == "unknown" or record.doc_type == "unknown" for record in records),
        extraction_errors=sum(record.extraction_error is not None for record in records),
        auto_classified=sum(not record.needs_review for record in records),
        review_rate=(sum(record.needs_review for record in records) / len(records)) if records else 0,
    )
    for record in records:
        summary.by_company[record.company_name] = summary.by_company.get(record.company_name, 0) + 1
        summary.by_doc_type[record.doc_type] = summary.by_doc_type.get(record.doc_type, 0) + 1
    return summary


def write_summary(config: AppConfig, records: list[ReportRecord]) -> Path:
    summary_path = config.report_path.with_name("classification_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(build_summary(records).model_dump_json(indent=2), encoding="utf-8")
    return summary_path
