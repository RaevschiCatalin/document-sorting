from __future__ import annotations

import time
from pathlib import Path

import typer

from docsort.config import load_config
from docsort.logging import configure_logging
from docsort.scanner import scan_once

app = typer.Typer(help="AI-assisted document sorting with local Ollama.")


def _print_summary(records_count: int, reviewed_count: int, unknown_count: int, report_path: Path) -> None:
    typer.echo(f"Processed: {records_count}")
    typer.echo(f"Needs review: {reviewed_count}")
    typer.echo(f"Unknown: {unknown_count}")
    typer.echo(f"Report: {report_path}")


@app.command()
def scan(
    config: Path = typer.Option(Path("config.example.yaml"), "--config", "-c", help="Path to YAML config."),
    max_files: int | None = typer.Option(None, "--max-files", help="Optional batch limit for this scan."),
    interactive: bool | None = typer.Option(None, "--interactive/--non-interactive", help="Override config review mode."),
) -> None:
    """Run one recursive scan."""
    app_config = load_config(config)
    configure_logging(app_config)
    if max_files is not None:
        app_config.processing.max_files_per_scan = max_files
    if interactive is not None:
        app_config.interactive = interactive
    records = scan_once(app_config)
    reviewed_count = sum(record.needs_review for record in records)
    unknown_count = sum(
        record.company_name == "unknown" or record.doc_type == "unknown"
        for record in records
    )
    _print_summary(len(records), reviewed_count, unknown_count, app_config.report_path)


@app.command()
def watch(
    config: Path = typer.Option(Path("config.example.yaml"), "--config", "-c", help="Path to YAML config."),
    interval: int = typer.Option(300, "--interval", "-i", help="Scan interval in seconds."),
    max_files: int | None = typer.Option(None, "--max-files", help="Optional batch limit per scan."),
    interactive: bool | None = typer.Option(None, "--interactive/--non-interactive", help="Override config review mode."),
) -> None:
    """Run scans repeatedly until interrupted."""
    app_config = load_config(config)
    configure_logging(app_config)
    if max_files is not None:
        app_config.processing.max_files_per_scan = max_files
    if interactive is not None:
        app_config.interactive = interactive
    typer.echo(f"Watching {app_config.input_dir} every {interval} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            records = scan_once(app_config)
            reviewed_count = sum(record.needs_review for record in records)
            unknown_count = sum(
                record.company_name == "unknown" or record.doc_type == "unknown"
                for record in records
            )
            _print_summary(len(records), reviewed_count, unknown_count, app_config.report_path)
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("Stopped.")


if __name__ == "__main__":
    app()
