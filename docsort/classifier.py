from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import requests
from loguru import logger
from pydantic import ValidationError

from docsort.models import AppConfig, ClassificationResult, ExtractedDocument, RuleHints

SYSTEM_PROMPT = """You are a careful document classification assistant for a local document sorting workflow.

Your task is to classify one document using:
- filename
- file metadata
- extracted text
- deterministic rule hints
- known companies
- allowed document types

You must return only valid JSON. Do not include Markdown, comments, or extra text.

Rules:
1. Choose company_name from known_companies when there is a clear match.
2. If no known company is clearly supported by filename, metadata, or text, use "unknown".
3. Choose doc_type from allowed_doc_types only.
4. If the document appears to be a non-disclosure agreement, use "nda".
5. If the document appears to be a contract, agreement, statement of work, MSA, order form, service agreement, purchase agreement, or Romanian contract ("contract", "acord", "prestari servicii"), use "contracts".
6. If the document appears to be an invoice, financial statement, tax document, balance sheet, payment record, receipt, profit/loss document, Romanian invoice ("factura"), or payment notice ("plata"), use "financial".
7. If evidence is weak or conflicting, use "unknown" or lower the confidence.
8. Use rule_hints as helpful signals, but do not blindly trust them if the text contradicts them.
9. Confidence must be a number from 0.0 to 1.0.
10. Provide concise reasoning that cites the strongest evidence.

Return this exact JSON shape:
{
  "company_name": "string",
  "doc_type": "contracts | nda | financial | unknown",
  "confidence": 0.0,
  "reasoning": "short explanation",
  "needs_review": true
}

Set needs_review to true when:
- confidence is below 0.75
- company_name is "unknown"
- doc_type is "unknown"
- filename/text/metadata conflict
- there is not enough extracted text to classify reliably
"""

DOC_TYPE_KEYWORDS = {
    "nda": [
        "nda",
        "non disclosure",
        "non-disclosure",
        "confidentiality agreement",
        "confidential information",
        "confidentialitate",
        "acord de confidentialitate",
    ],
    "contracts": [
        "contract",
        "agreement",
        "msa",
        "master services",
        "sow",
        "statement of work",
        "order form",
        "purchase agreement",
        "service agreement",
        "acord",
        "prestari servicii",
    ],
    "financial": [
        "invoice",
        "receipt",
        "balance sheet",
        "financial statement",
        "tax",
        "payment",
        "p&l",
        "profit and loss",
        "factura",
        "plata",
        "achitare",
        "situatie financiara",
    ],
}


def _normalize_text(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = ascii_value.lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", normalized)


def normalize_company_name(value: str, config: AppConfig) -> str:
    normalized = _normalize_text(value).strip()
    if not normalized:
        return "unknown"
    for company in config.known_companies:
        aliases = [company, *config.company_aliases.get(company, [])]
        if normalized in {_normalize_text(alias).strip() for alias in aliases}:
            return company
    return normalized


def _company_aliases(config: AppConfig) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for company in config.known_companies:
        aliases[company] = [company, *config.company_aliases.get(company, [])]
    return aliases


def _metadata_text(metadata: dict[str, Any]) -> str:
    keys = ["title", "author", "subject", "keywords", "creator", "producer"]
    return " ".join(str(metadata.get(key, "")) for key in keys)


def _contains_term(haystack: str, term: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(_normalize_text(term)) + r"(?!\w)"
    return re.search(pattern, haystack) is not None


def build_rule_hints(doc: ExtractedDocument, config: AppConfig) -> RuleHints:
    filename_text = _normalize_text(doc.filename)
    metadata_text = _normalize_text(_metadata_text(doc.metadata))
    body_text = _normalize_text(doc.text[:4000])
    combined = f"{filename_text} {metadata_text} {body_text}"

    matched_terms: list[str] = []
    notes: list[str] = []
    company_name = "unknown"
    company_confidence = 0.0

    for company, aliases in _company_aliases(config).items():
        filename_alias = next((alias for alias in aliases if _contains_term(filename_text, alias)), None)
        context_alias = next(
            (alias for alias in aliases if _contains_term(metadata_text, alias) or _contains_term(body_text, alias)),
            None,
        )
        if filename_alias:
            company_name = company
            company_confidence = 0.9
            matched_terms.append(filename_alias)
            break
        if context_alias:
            company_name = company
            company_confidence = 0.7
            matched_terms.append(context_alias)
            break

    doc_scores: dict[str, float] = {}
    doc_matches: dict[str, set[str]] = {}
    for doc_type, terms in DOC_TYPE_KEYWORDS.items():
        for term in terms:
            normalized_term = _normalize_text(term)
            if _contains_term(filename_text, normalized_term):
                doc_scores[doc_type] = max(doc_scores.get(doc_type, 0.0), 0.9)
                doc_matches.setdefault(doc_type, set()).add(term)
                matched_terms.append(term)
            elif _contains_term(metadata_text, normalized_term):
                doc_scores[doc_type] = max(doc_scores.get(doc_type, 0.0), 0.75)
                doc_matches.setdefault(doc_type, set()).add(term)
                matched_terms.append(term)
            elif _contains_term(body_text, normalized_term):
                doc_scores[doc_type] = max(doc_scores.get(doc_type, 0.0), 0.65)
                doc_matches.setdefault(doc_type, set()).add(term)
                matched_terms.append(term)

    doc_type = "unknown"
    doc_confidence = 0.0
    if doc_scores:
        sorted_scores = sorted(doc_scores.items(), key=lambda item: item[1], reverse=True)
        doc_type, doc_confidence = sorted_scores[0]
        if len(sorted_scores) > 1 and sorted_scores[1][1] >= doc_confidence - 0.1:
            tied_types = {item[0] for item in sorted_scores if item[1] >= doc_confidence - 0.1}
            contract_matches = doc_matches.get("contracts", set())
            if tied_types == {"nda", "contracts"} and contract_matches <= {"agreement"}:
                doc_type = "nda"
                doc_confidence = 0.85
            else:
                doc_type = "unknown"
                doc_confidence = 0.4
                notes.append("Conflicting document type keyword matches")

    confidence = min(
        score for score in [company_confidence, doc_confidence] if score > 0
    ) if company_confidence and doc_confidence else max(company_confidence, doc_confidence) * 0.7

    return RuleHints(
        company_name=company_name,
        doc_type=doc_type,
        confidence=confidence,
        matched_terms=sorted(set(matched_terms)),
        notes=notes,
    )


def _payload(doc: ExtractedDocument, config: AppConfig, rule_hints: RuleHints) -> dict[str, Any]:
    return {
        "filename": doc.filename,
        "extension": doc.extension,
        "mime_type": doc.mime_type,
        "metadata": doc.metadata,
        "text_excerpt": doc.text,
        "rule_hints": rule_hints.model_dump(),
        "known_companies": config.known_companies,
        "allowed_doc_types": config.allowed_doc_types,
    }


def _extract_json(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(content[start : end + 1])


def _call_ollama(doc: ExtractedDocument, config: AppConfig, rule_hints: RuleHints, retry: bool = False) -> dict[str, Any]:
    user_payload = json.dumps(_payload(doc, config, rule_hints), ensure_ascii=False)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"/no_think\n{user_payload}"},
    ]
    if retry:
        messages.append(
            {
                "role": "user",
                "content": "Return valid JSON only. No Markdown. No explanation outside the JSON object.",
            }
        )

    response = requests.post(
        f"{config.ollama.base_url.rstrip('/')}/api/chat",
        json={
            "model": config.ollama.model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": 512,
            },
        },
        timeout=config.ollama.timeout_seconds,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    return _extract_json(content)


def _finalize_result(result: ClassificationResult, config: AppConfig) -> ClassificationResult:
    result.company_name = normalize_company_name(result.company_name, config)
    result.doc_type = result.doc_type.strip().lower() or "unknown"

    if result.doc_type not in config.allowed_doc_types:
        result.reasoning = f"{result.reasoning} Invalid doc_type returned by model; set to unknown.".strip()
        result.doc_type = "unknown"

    if (
        result.confidence < config.low_confidence_threshold
        or result.company_name == "unknown"
        or result.doc_type == "unknown"
    ):
        result.needs_review = True

    return result


def _fallback_result(rule_hints: RuleHints, reason: str) -> ClassificationResult:
    return ClassificationResult(
        company_name=rule_hints.company_name,
        doc_type=rule_hints.doc_type,
        confidence=0.0,
        reasoning=reason,
        needs_review=True,
        rule_hints=rule_hints,
    )


def _rule_only_result(rule_hints: RuleHints) -> ClassificationResult:
    return ClassificationResult(
        company_name=rule_hints.company_name,
        doc_type=rule_hints.doc_type,
        confidence=rule_hints.confidence,
        reasoning=f"High-confidence deterministic rule match: {', '.join(rule_hints.matched_terms)}",
        needs_review=False,
        rule_hints=rule_hints,
    )


def classify_document(doc: ExtractedDocument, config: AppConfig) -> ClassificationResult:
    rule_hints = build_rule_hints(doc, config)
    if not doc.text.strip() and doc.extraction_error:
        return _fallback_result(rule_hints, f"Extraction failed: {doc.extraction_error}")

    if (
        rule_hints.company_name != "unknown"
        and rule_hints.doc_type != "unknown"
        and rule_hints.confidence >= config.processing.high_confidence_rule_threshold
        and not rule_hints.notes
    ):
        logger.debug("Accepted high-confidence rule match for {}", doc.filename)
        return _finalize_result(_rule_only_result(rule_hints), config)

    try:
        try:
            raw = _call_ollama(doc, config, rule_hints)
            result = ClassificationResult.model_validate(raw)
        except (json.JSONDecodeError, KeyError, ValidationError):
            raw = _call_ollama(doc, config, rule_hints, retry=True)
            result = ClassificationResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - local Ollama may be unavailable in demos.
        logger.warning("Ollama classification failed for {}: {}", doc.filename, exc)
        return _fallback_result(
            rule_hints,
            (
                "Ollama unavailable or invalid response; routed to manual review. "
                f"Rule hint suggestion: company={rule_hints.company_name}, "
                f"doc_type={rule_hints.doc_type}."
            ),
        )

    result.rule_hints = rule_hints
    return _finalize_result(result, config)
