"""
AIVO Sales Intents

Define as intencoes reconhecidas pelo Sales Brain.
"""

from enum import Enum


class Intent(str, Enum):
    OBJECTION = "objection"
    UNKNOWN = "unknown"

    GREETING = "greeting"

    BUY = "buy"
    PRICE = "price"
    PLANS = "plans"

    HOW_IT_WORKS = "how_it_works"

    VIDEO = "video"

    MEETING = "meeting"
    CLASS = "class"
    INTERVIEW = "interview"
    PODCAST = "podcast"

    TRANSCRIPTION = "transcription"
    SUMMARY = "summary"
    REPORT = "report"
    MINDMAP = "mindmap"

    COMPARISON = "comparison"

    SUPPORT = "support"

    FAQ = "faq"