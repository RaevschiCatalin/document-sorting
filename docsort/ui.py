from __future__ import annotations

import json
import shutil
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz
import gradio as gr
from loguru import logger
from PIL import Image

from docsort.config import load_config
from docsort.demo_files import create_demo_files
from docsort.extractor import extract_document
from docsort.logging import configure_logging
from docsort.organizer import sanitize_folder_name
from docsort.scanner import scan_once

CONFIG_PATH = Path("config.yaml") if Path("config.yaml").exists() else Path("config.example.yaml")
DOC_TYPES = ["contracts", "nda", "financial", "unknown"]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
SCAN_LOCK = threading.Lock()
SCAN_STATE = {"running": False, "message": "Idle"}

CSS = """
.metric-card {
  border: 1px solid var(--border-color-primary);
  border-radius: 10px;
  padding: 14px 16px;
  background: var(--block-background-fill);
}
.metric-value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.1;
}
.metric-label {
  color: var(--body-text-color-subdued);
  font-size: 13px;
}
"""


def _config():
    config = load_config(CONFIG_PATH)
    configure_logging(config)
    return config


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSONL row in {}", path)
    return rows


def _read_report() -> list[dict[str, Any]]:
    return _read_jsonl(_config().report_path)


def _read_summary() -> dict[str, Any]:
    rows = _read_report()
    return {
        "processed": len(rows),
        "auto_classified": sum(not row.get("needs_review", False) for row in rows),
        "needs_review": sum(row.get("needs_review", False) for row in rows),
        "unknown": sum(row.get("company_name") == "unknown" or row.get("doc_type") == "unknown" for row in rows),
        "extraction_errors": sum(row.get("extraction_error") is not None for row in rows),
        "review_rate": (sum(row.get("needs_review", False) for row in rows) / len(rows)) if rows else 0,
    }


def _pending_rows() -> list[dict[str, Any]]:
    pending_dir = _config().output_dir / "pending_review"
    existing = {path.name for path in pending_dir.glob("*")} if pending_dir.exists() else set()
    return [row for row in _read_report() if row.get("needs_review") and Path(row["target_path"]).name in existing]


def pending_choices() -> list[str]:
    return [Path(row["target_path"]).name for row in _pending_rows()]


def _processed_table() -> list[list[Any]]:
    rows = []
    for row in reversed(_read_report()[-100:]):
        rows.append(
            [
                Path(row["source_path"]).name,
                row["company_name"],
                row["doc_type"],
                round(float(row["confidence"]), 2),
                "Review" if row["needs_review"] else "Auto",
                row["target_path"],
            ]
        )
    return rows


def _pending_table() -> list[list[Any]]:
    return [
        [
            Path(row["target_path"]).name,
            row["company_name"],
            row["doc_type"],
            round(float(row["confidence"]), 2),
            row["reasoning"][:180],
        ]
        for row in _pending_rows()
    ]


def _pending_by_filename(filename: str) -> dict[str, Any] | None:
    for row in _pending_rows():
        if Path(row["target_path"]).name == filename:
            return row
    return None


def _selected_pending_path(filename: str) -> Path | None:
    if not filename:
        return None
    path = _config().output_dir / "pending_review" / filename
    return path if path.exists() else None


def metric_markdown() -> str:
    summary = _read_summary()
    report_rows = _read_report()
    total_reported = len(report_rows)
    pending_now = len(_pending_rows())
    last_processed = report_rows[-1].get("processed_at", "never") if report_rows else "never"
    return f"""
<div style="display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px">
  <div class="metric-card"><div class="metric-value">{summary.get("processed", 0)}</div><div class="metric-label">Processed</div></div>
  <div class="metric-card"><div class="metric-value">{summary.get("auto_classified", 0)}</div><div class="metric-label">Auto-classified</div></div>
  <div class="metric-card"><div class="metric-value">{pending_now}</div><div class="metric-label">Pending now</div></div>
  <div class="metric-card"><div class="metric-value">{summary.get("unknown", 0)}</div><div class="metric-label">Unknown</div></div>
  <div class="metric-card"><div class="metric-value">{summary.get("extraction_errors", 0)}</div><div class="metric-label">Extraction errors</div></div>
</div>
<div style="margin-top:8px;color:var(--body-text-color-subdued);font-size:13px">
  Report rows: {total_reported} · Last processed: {last_processed} · Scan status: {SCAN_STATE["message"]}
</div>
"""


def refresh() -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    return SCAN_STATE["message"], metric_markdown(), _processed_table(), _pending_table(), gr.Dropdown(choices=pending_choices())


def _row_index_from_event(evt: gr.SelectData) -> int:
    index = evt.index
    if isinstance(index, (tuple, list)):
        return int(index[0])
    return int(index)


def select_pending_row(evt: gr.SelectData) -> tuple[dict[str, Any], str, str, str, str | None, Image.Image | None]:
    try:
        row_index = _row_index_from_event(evt)
        filename = _pending_table()[row_index][0]
    except (IndexError, TypeError, ValueError):
        return gr.update(value=None, choices=pending_choices()), "unknown", "unknown", "Could not select pending file.", None, None

    company, doc_type, preview, file_path, image = preview_pending(filename)
    return gr.update(value=filename, choices=pending_choices()), company, doc_type, preview, file_path, image


def _visual_preview(path: Path) -> Image.Image | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return Image.open(path)
    if suffix == ".pdf":
        try:
            with fitz.open(path) as document:
                if len(document) == 0:
                    return None
                pixmap = document[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                return Image.open(BytesIO(pixmap.tobytes("png")))
        except Exception:
            logger.exception("Failed to render PDF preview for {}", path)
    return None


def preview_pending(filename: str) -> tuple[str, str, str, str | None, Image.Image | None]:
    row = _pending_by_filename(filename) if filename else None
    path = _selected_pending_path(filename)
    if row is None or path is None:
        return "unknown", "unknown", "Select a pending file to preview.", None, None

    extracted = extract_document(path, _config())
    preview = extracted.text.strip()
    if not preview:
        if path.suffix.lower() in IMAGE_SUFFIXES:
            preview = "Image preview is shown below. Text extraction is empty because OCR is disabled."
        elif path.suffix.lower() == ".pdf":
            preview = "PDF visual preview is shown below. Extracted text is empty."
        else:
            preview = extracted.extraction_error or "No text preview available for this file."
    if len(preview) > 4000:
        preview = f"{preview[:4000]}\n\n[Preview truncated]"
    header = (
        f"File: {path.name}\n"
        f"Suggested: {row.get('company_name', 'unknown')} / {row.get('doc_type', 'unknown')}\n"
        f"Confidence: {row.get('confidence', 0)}\n"
        f"Reason: {row.get('reasoning', '')}\n\n"
    )
    return row.get("company_name", "unknown"), row.get("doc_type", "unknown"), header + preview, str(path), _visual_preview(path)


def _scan_worker() -> None:
    config = _config()
    try:
        records = scan_once(config)
        SCAN_STATE["message"] = (
            f"Scan complete: {len(records)} processed, "
            f"{sum(record.needs_review for record in records)} need review."
        )
    except Exception as exc:  # noqa: BLE001 - surface failure in the UI.
        logger.exception("UI-triggered scan failed")
        SCAN_STATE["message"] = f"Scan failed: {exc}"
    finally:
        SCAN_STATE["running"] = False
        SCAN_LOCK.release()


def run_scan() -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    if not SCAN_LOCK.acquire(blocking=False):
        _, metrics, processed, pending, choices = refresh()
        return "A scan is already running. The dashboard auto-refreshes every 5 seconds.", metrics, processed, pending, choices

    SCAN_STATE["running"] = True
    SCAN_STATE["message"] = "Scan started in the background. Use Refresh to update results."
    threading.Thread(target=_scan_worker, daemon=True).start()
    _, metrics, processed, pending, choices = refresh()
    return SCAN_STATE["message"], metrics, processed, pending, choices


def auto_scan_if_enabled(enabled: bool) -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    if enabled:
        return run_scan()
    return refresh()


def generate_demo_file_if_enabled(enabled: bool) -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    if not enabled:
        return refresh()

    paths = create_demo_files(_config().input_dir, count_files=5)
    _, metrics, processed, pending, choices = refresh()
    names = ", ".join(path.name for path in paths)
    return f"Generated 5 incoming demo files: {names}", metrics, processed, pending, choices


def _move_pending(filename: str, company: str, doc_type: str) -> str:
    if not filename:
        return "Select a pending file first."

    config = _config()
    pending_path = config.output_dir / "pending_review" / filename
    if not pending_path.exists():
        return f"Pending file not found: {filename}"

    company_slug = sanitize_folder_name(company or "unknown")
    doc_type_slug = sanitize_folder_name(doc_type or "unknown")
    target_dir = config.output_dir / company_slug / doc_type_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / pending_path.name
    counter = 1
    while target_path.exists():
        target_path = target_dir / f"{pending_path.stem}_{counter}{pending_path.suffix}"
        counter += 1
    shutil.move(str(pending_path), str(target_path))
    return f"Moved {filename} to {company_slug}/{doc_type_slug}."


def approve_suggested(filename: str) -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    row = _pending_by_filename(filename) if filename else None
    if row is None:
        _, metrics, processed, pending, choices = refresh()
        return "Select a pending file first.", metrics, processed, pending, choices

    message = _move_pending(filename, row.get("company_name", "unknown"), row.get("doc_type", "unknown"))
    _, metrics, processed, pending, choices = refresh()
    return message, metrics, processed, pending, choices


def reclassify_pending(filename: str, company: str, doc_type: str) -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    message = _move_pending(filename, company, doc_type)
    _, metrics, processed, pending, choices = refresh()
    return message, metrics, processed, pending, choices


def refuse_pending(filename: str) -> tuple[str, str, list[list[Any]], list[list[Any]], gr.Dropdown]:
    message = _move_pending(filename, "unknown", "unknown")
    _, metrics, processed, pending, choices = refresh()
    return f"Refused suggestion. {message}", metrics, processed, pending, choices


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Document Sorting Review") as demo:
        gr.Markdown("# Document Sorting Review")
        gr.Markdown("Operational dashboard for scans, classification results, and pending review.")

        metrics = gr.HTML(value=metric_markdown)
        with gr.Row():
            scan_button = gr.Button("Run Scan", variant="primary")
            refresh_button = gr.Button("Refresh")
            auto_scan = gr.Checkbox(label="Auto scan every 5 minutes", value=False)
            auto_generate = gr.Checkbox(label="Generate 5 demo files every 30 seconds", value=False)
        message = gr.Textbox(label="Status", interactive=False)
        refresh_timer = gr.Timer(value=60, active=True)
        scan_timer = gr.Timer(value=300, active=True)
        generate_timer = gr.Timer(value=30, active=True)

        with gr.Tabs():
            with gr.Tab("Processed Files"):
                processed_table = gr.Dataframe(
                    headers=["File", "Company", "Doc Type", "Confidence", "Status", "Target Path"],
                    value=_processed_table,
                    interactive=False,
                    wrap=True,
                )
            with gr.Tab("Pending Review"):
                with gr.Row():
                    with gr.Column(scale=3):
                        gr.Markdown("Click any row below to select the pending file and open its preview.")
                        pending_table = gr.Dataframe(
                            headers=["File", "Suggested Company", "Suggested Type", "Confidence", "Reason"],
                            value=_pending_table,
                            interactive=False,
                            wrap=True,
                        )
                        pending_file = gr.Dropdown(label="Select pending file", choices=pending_choices())
                    with gr.Column(scale=4):
                        preview = gr.Textbox(label="File Preview", lines=18, interactive=False)
                        image_preview = gr.Image(label="Visual Preview", interactive=False, height=360)
                        file_link = gr.File(label="Open/download selected pending file", interactive=False)
                        with gr.Row():
                            company = gr.Textbox(label="Approved company", value="unknown")
                            doc_type = gr.Dropdown(label="Approved document type", choices=DOC_TYPES, value="unknown")
                with gr.Row():
                    approve_button = gr.Button("Approve Suggested", variant="primary")
                    reclassify_button = gr.Button("Reclassify")
                    refuse_button = gr.Button("Refuse / Send to Unknown", variant="stop")

        scan_button.click(run_scan, outputs=[message, metrics, processed_table, pending_table, pending_file])
        refresh_button.click(refresh, outputs=[message, metrics, processed_table, pending_table, pending_file])
        refresh_timer.tick(refresh, outputs=[message, metrics, processed_table, pending_table, pending_file])
        scan_timer.tick(auto_scan_if_enabled, inputs=[auto_scan], outputs=[message, metrics, processed_table, pending_table, pending_file])
        generate_timer.tick(generate_demo_file_if_enabled, inputs=[auto_generate], outputs=[message, metrics, processed_table, pending_table, pending_file])
        pending_table.select(select_pending_row, outputs=[pending_file, company, doc_type, preview, file_link, image_preview])
        pending_file.change(preview_pending, inputs=[pending_file], outputs=[company, doc_type, preview, file_link, image_preview])
        approve_button.click(
            approve_suggested,
            inputs=[pending_file],
            outputs=[message, metrics, processed_table, pending_table, pending_file],
        )
        reclassify_button.click(
            reclassify_pending,
            inputs=[pending_file, company, doc_type],
            outputs=[message, metrics, processed_table, pending_table, pending_file],
        )
        refuse_button.click(
            refuse_pending,
            inputs=[pending_file],
            outputs=[message, metrics, processed_table, pending_table, pending_file],
        )
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="0.0.0.0", server_port=7860, theme=gr.themes.Soft(), css=CSS)
