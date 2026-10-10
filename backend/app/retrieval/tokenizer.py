"""
Deterministic tokenization and text preprocessing for lexical BM25 retrieval.
"""
import re
from typing import List


# Standard regex pattern for alphanumeric tokens (including dotted clauses / identifiers)
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")


def tokenize_text(text: str) -> List[str]:
    """
    Deterministic whitespace and punctuation tokenizer with lowercase normalization.

    Extracts alphanumeric tokens, acronyms, dates, and clause references (e.g., 'PR.IP.S16', '2024/113').
    """
    if not text or not isinstance(text, str):
        return []

    # Normalize text to lower case
    text_lower = text.lower()
    tokens = _TOKEN_PATTERN.findall(text_lower)
    return tokens
