from enum import Enum


class RAIService:
    """Define constants related to RAI service"""

    TIMEOUT = 1800
    SLEEP_TIME = 2
    HARM_SEVERITY_THRESHOLD = 4


class HarmSeverityLevel(Enum):
    VeryLow = "Very low"
    Low = "Low"
    Medium = "Medium"
    High = "High"


class Tasks:
    """Defines types of annotation tasks supported by RAI Service."""

    CONTENT_HARM = "content harm"
