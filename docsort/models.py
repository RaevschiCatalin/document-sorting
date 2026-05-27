from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


DEFAULT_DOC_TYPES = ["contracts", "nda", "financial", "unknown"]


class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    timeout_seconds: int = 60


class OcrConfig(BaseModel):
    enabled: bool = False
    min_text_chars_before_ocr: int = 100


class TextConfig(BaseModel):
    max_chars: int = 8000


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file_path: Path | None = Path("reports/docsort.log")
    error_file_path: Path | None = Path("reports/docsort_errors.log")
    rotation: str = "1 MB"
    retention: str = "7 days"


class ProcessingConfig(BaseModel):
    max_files_per_scan: int | None = None
    high_confidence_rule_threshold: float = 0.80

    @field_validator("high_confidence_rule_threshold")
    @classmethod
    def validate_rule_threshold(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("high_confidence_rule_threshold must be between 0 and 1")
        return value


class AppConfig(BaseModel):
    input_dir: Path = Path("sample_input")
    output_dir: Path = Path("sample_output")
    report_path: Path = Path("reports/classification_report.jsonl")
    manifest_path: Path = Path("reports/processed_manifest.jsonl")
    action: Literal["copy", "move"] = "copy"
    low_confidence_threshold: float = 0.75
    interactive: bool = False
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    known_companies: list[str] = Field(default_factory=lambda: ["acme", "globex", "initech"])
    company_aliases: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "acme": ["ACME", "Acme Corp", "ACME Corporation", "ACME Ltd"],
            "globex": ["Globex", "Globex Industries", "Globex Corporation"],
            "initech": ["Initech", "Initech LLC", "Initrode/Initech"],
        }
    )
    allowed_doc_types: list[str] = Field(default_factory=lambda: DEFAULT_DOC_TYPES.copy())
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)

    @field_validator("low_confidence_threshold")
    @classmethod
    def validate_threshold(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("low_confidence_threshold must be between 0 and 1")
        return value

    @field_validator("allowed_doc_types")
    @classmethod
    def validate_doc_types(cls, value: list[str]) -> list[str]:
        if "unknown" not in value:
            raise ValueError("allowed_doc_types must include 'unknown'")
        return value


class RuleHints(BaseModel):
    company_name: str = "unknown"
    doc_type: str = "unknown"
    confidence: float = 0.0
    matched_terms: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class ExtractedDocument(BaseModel):
    source_path: Path
    filename: str
    extension: str
    mime_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    ocr_attempted: bool = False
    extraction_error: str | None = None


class ClassificationResult(BaseModel):
    company_name: str = "unknown"
    doc_type: str = "unknown"
    confidence: float = 0.0
    reasoning: str = ""
    needs_review: bool = True
    rule_hints: RuleHints = Field(default_factory=RuleHints)

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class ManifestRecord(BaseModel):
    source_path: Path
    source_size: int
    source_mtime: float
    sha256: str
    target_path: Path
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportRecord(BaseModel):
    source_path: Path
    target_path: Path
    company_name: str
    doc_type: str
    confidence: float
    needs_review: bool
    reasoning: str
    extraction_error: str | None = None
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SummaryStats(BaseModel):
    processed: int = 0
    needs_review: int = 0
    unknown: int = 0
    extraction_errors: int = 0
    auto_classified: int = 0
    review_rate: float = 0.0
    by_company: dict[str, int] = Field(default_factory=dict)
    by_doc_type: dict[str, int] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
