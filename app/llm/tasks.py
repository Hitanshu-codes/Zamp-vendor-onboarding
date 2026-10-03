"""The LLM tasks this system uses. Nothing else calls the model.

classify_extract, name_adjudication, holder_type_adjudication, injection_llm_scan,
tax_rule_search, tax_rule_extraction.
"""
from __future__ import annotations

from app.config import config
from app.llm import prompts
from app.llm.client import CallInfo, Part, call_structured, call_with_tools
from app.llm.schemas import DocumentExtraction, HolderAdjudication, InjectionScan, NameAdjudication, TaxRuleExtraction

SLOT_LABELS = {
    "tax_cert": "tax registration certificate",
    "bank_letter": "bank letter or cancelled cheque",
    "incorporation_cert": "incorporation certificate",
}

INJECTION_SCAN_MAX_CHARS = 24000


async def classify_extract(data: bytes, mime_type: str, slot: str) -> tuple[DocumentExtraction, CallInfo]:
    parts = [Part(data=data, mime_type=mime_type), Part(text=prompts.classify_extract_user(SLOT_LABELS[slot]))]
    return await call_structured("classify_extract", config.model_extract, prompts.CLASSIFY_EXTRACT_SYSTEM, parts, DocumentExtraction)


async def adjudicate_names(a: str, b: str, country: str, entity_type: str) -> tuple[NameAdjudication, CallInfo]:
    parts = [Part(text=prompts.name_adjudication_user(a, b, country, entity_type))]
    return await call_structured("name_adjudication", config.model_judge, prompts.NAME_ADJUDICATION_SYSTEM, parts, NameAdjudication)


async def adjudicate_holder(holder: str, entity_type: str, account_type: str) -> tuple[HolderAdjudication, CallInfo]:
    parts = [Part(text=prompts.holder_user(holder, entity_type, account_type))]
    return await call_structured("holder_type_adjudication", config.model_judge, prompts.HOLDER_SYSTEM, parts, HolderAdjudication)


async def injection_scan(source: str, text: str) -> tuple[InjectionScan, CallInfo]:
    parts = [Part(text=prompts.injection_scan_user(source, text[:INJECTION_SCAN_MAX_CHARS]))]
    return await call_structured("injection_llm_scan", config.model_judge, prompts.INJECTION_SCAN_SYSTEM, parts, InjectionScan)


async def tax_rule_search(country_name: str, tax_type: str) -> tuple[dict, CallInfo]:
    parts = [Part(text=prompts.tax_rule_search_user(country_name, tax_type))]
    return await call_with_tools("tax_rule_search", config.model_judge, prompts.TAX_RULE_SEARCH_SYSTEM, parts, [{"type": "google_search"}])


async def tax_rule_extract(country: str, tax_type: str, url: str, page_text: str) -> tuple[TaxRuleExtraction, CallInfo]:
    parts = [Part(text=prompts.tax_rule_extract_user(country, tax_type, url, page_text))]
    return await call_structured("tax_rule_extraction", config.model_judge, prompts.TAX_RULE_EXTRACT_SYSTEM, parts, TaxRuleExtraction)
