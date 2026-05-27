from __future__ import annotations

import re
import shutil
import unicodedata
from functools import lru_cache
from pathlib import Path

import typer

from docsort.models import AppConfig, ClassificationResult


@lru_cache(maxsize=512)
def sanitize_folder_name(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = ascii_value.strip().lower().replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_.-]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "unknown"


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _prompt_for_review(classification: ClassificationResult, config: AppConfig) -> tuple[str, str, bool]:
    typer.echo("\nLow-confidence classification needs review:")
    typer.echo(f"  Suggested company: {classification.company_name}")
    typer.echo(f"  Suggested type:    {classification.doc_type}")
    typer.echo(f"  Confidence:        {classification.confidence:.2f}")
    typer.echo(f"  Reasoning:         {classification.reasoning}")

    choice = typer.prompt(
        "Accept [a], override [o], refuse/send to unknown [r], or pending review [p]?",
        default="p",
    ).strip().lower()
    if choice == "a":
        return classification.company_name, classification.doc_type, False
    if choice == "o":
        company = typer.prompt("Company name", default=classification.company_name or "unknown")
        doc_type = typer.prompt("Document type", default=classification.doc_type or "unknown")
        if doc_type not in config.allowed_doc_types:
            typer.echo(f"Unsupported document type '{doc_type}', using 'unknown'.")
            doc_type = "unknown"
        return company, doc_type, False
    if choice == "r":
        return "unknown", "unknown", False
    return "pending_review", "unknown", True


def organize_file(source: Path, classification: ClassificationResult, config: AppConfig) -> Path:
    company = classification.company_name
    doc_type = classification.doc_type
    pending_review = False

    if classification.needs_review:
        if config.interactive:
            company, doc_type, pending_review = _prompt_for_review(classification, config)
        else:
            pending_review = True

    if pending_review:
        target_dir = config.output_dir / "pending_review"
    else:
        target_dir = config.output_dir / sanitize_folder_name(company) / sanitize_folder_name(doc_type)

    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(target_dir / source.name)

    if config.action == "move":
        shutil.move(str(source), str(target))
    else:
        shutil.copy2(source, target)

    return target
