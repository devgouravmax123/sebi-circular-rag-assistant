"""
Deterministic query intent classification for SEBI CSCRF Regulatory Q&A Assistant.
"""
from enum import Enum
import re
from typing import Dict, Any, List, Optional


class QueryIntent(str, Enum):
    CURRENT_STATUS_OR_DEADLINE = "CURRENT_STATUS_OR_DEADLINE"
    HISTORICAL_DEADLINE = "HISTORICAL_DEADLINE"
    CIRCULAR_SPECIFIC_CHANGE = "CIRCULAR_SPECIFIC_CHANGE"
    PROVISION_SPECIFIC_REQUIREMENT = "PROVISION_SPECIFIC_REQUIREMENT"
    GENERAL_REQUIREMENT = "GENERAL_REQUIREMENT"
    OUT_OF_SCOPE_OR_UNESTABLISHED = "OUT_OF_SCOPE_OR_UNESTABLISHED"


# Patterns for clearly out of scope topics relative to the 5-document CSCRF corpus
OUT_OF_SCOPE_PATTERNS = [
    r"\bnav\s+cutoff\b",
    r"\bmutual\s+fund\s+nav\b",
    r"\bcutoff\s+time\b",
    r"\bunregulated\s+fintechs?\b",
    r"\bunregulated\s+entit\w*\b",
    r"\binsider\s+trading\b",
    r"\btakeover\s+code\b",
    r"\blodr\b",
    r"\bipo\s+allotment\b",
    r"\bdemat\s+account\s+opening\s+charge\b",
    r"\bdividend\s+distribution\b",
    r"\bfund\s+manager\s+salary\b",
]

# Specific circular mention patterns (generalized regex + baseline aliases)
CIRCULAR_SPECIFIC_PATTERNS = [
    r"\b(?:circular\s+)?(?:20\d{2}[/_-]0?\d{1,4}|\d{1,4}/20\d{2})\b",
    r"\b(?:circular\s+)?(?:2025/0?96|cir/2025/0?96|2025_0?96|96/2025)\b",
    r"\b(?:circular\s+)?(?:2025/0?45|cir/2025/0?45|2025_0?45|45/2025)\b",
    r"\b(?:circular\s+)?(?:2025/0?60|cir/2025/0?60|2025_0?60|60/2025)\b",
    r"\b(?:circular\s+)?(?:2025/119|cir/2025/119|2025_119|119/2025)\b",
    r"\b(?:circular\s+)?(?:2024/113|cir/2024/113|2024_113|113/2024)\b",
]


# Historical deadline patterns
HISTORICAL_DEADLINE_PATTERNS = [
    r"\boriginal\s+(?:cscrf\s+)?(?:implementation\s+|compliance\s+)?deadline\b",
    r"\boriginal\s+timeline\b",
    r"\bearlier\s+(?:cybersecurity\s+)?(?:compliance\s+)?timeline\b",
    r"\bwhat\s+was\s+the\s+(?:earlier|initial|original|previous)\b",
    r"\bbefore\s+(?:recent\s+)?extensions?\b",
]

# Current status or active deadline patterns
CURRENT_DEADLINE_PATTERNS = [
    r"\bdeadline\s+today\b",
    r"\bmandatory\s+compliance\s+deadline\s+today\b",
    r"\bwhat\s+is\s+the\s+(?:current|active|effective|latest)\s+deadline\b",
    r"\bcompliance\s+deadline\b",
    r"\bdeadline\s+for\s+(?:cscrf|qualified\s+res?)\b",
    r"\bwhen\s+is\s+the\s+cscrf\s+deadline\b",
    r"\bwhat\s+is\s+the\s+mandatory\s+compliance\s+deadline\b",
    r"\bis\s+it\s+currently\s+in\s+force\b",
]

# Specific technical provision patterns
PROVISION_SPECIFIC_PATTERNS = [
    r"\biso\s*27001\b",
    r"\bciso\b",
    r"\bchief\s+information\s+security\s+officer\b",
    r"\bincident\s+reporting\b",
    r"\bcyber\s+incident\b",
    r"\brto\b",
    r"\brpo\b",
    r"\brecovery\s+time\s+objective\b",
    r"\bhardware\s+security\s+module\b|\bhsm\b",
    r"\bzero[\s-]trust\b",
    r"\bvirtual\s+patching\b",
    r"\bmarket[\s-]soc\b",
    r"\bvapt\b",
    r"\bstock\s+brokers?\s+categor\w*\b",
    r"\bkyc\s+registration\s+agenc\w*\b|\bkra\b",
    r"\binvestment\s+advisers?\b|\bia\b",
    r"\bresearch\s+analysts?\b|\bra\b",
]


def classify_query_intent(query: str) -> QueryIntent:
    """
    Deterministic query intent classifier with clear precedence rules.
    """
    q_norm = " ".join(query.strip().lower().split())

    # 1. Check out of scope patterns
    for pat in OUT_OF_SCOPE_PATTERNS:
        if re.search(pat, q_norm):
            return QueryIntent.OUT_OF_SCOPE_OR_UNESTABLISHED

    # 2. Check circular specific changes
    for pat in CIRCULAR_SPECIFIC_PATTERNS:
        if re.search(pat, q_norm):
            return QueryIntent.CIRCULAR_SPECIFIC_CHANGE

    # 3. Check historical deadline queries
    for pat in HISTORICAL_DEADLINE_PATTERNS:
        if re.search(pat, q_norm):
            return QueryIntent.HISTORICAL_DEADLINE

    # 4. Check current status / deadline queries
    for pat in CURRENT_DEADLINE_PATTERNS:
        if re.search(pat, q_norm):
            return QueryIntent.CURRENT_STATUS_OR_DEADLINE

    # 5. Check provision specific requirements
    for pat in PROVISION_SPECIFIC_PATTERNS:
        if re.search(pat, q_norm):
            return QueryIntent.PROVISION_SPECIFIC_REQUIREMENT

    # 6. Default to general requirement
    return QueryIntent.GENERAL_REQUIREMENT
