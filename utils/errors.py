"""Error taxonomy used at real call sites. Values are stable log/Airtable strings."""


class ErrorType:
    INGESTION_ERROR = "INGESTION_ERROR"
    RATE_LIMIT_ERROR = "RATE_LIMIT_ERROR"
    DOCUMENT_ERROR = "DOCUMENT_ERROR"
    ANALYSIS_ERROR = "ANALYSIS_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SSRF_ERROR = "SSRF_ERROR"
    DRAFTING_ERROR = "DRAFTING_ERROR"
    SPEND_CAP_ERROR = "SPEND_CAP_ERROR"
    CANCELLED = "CANCELLED"


class SpendCapError(Exception):
    """Raised when complete() would exceed MAX_RUN_COST_USD. Not a draft-quality failure."""

    error_type = ErrorType.SPEND_CAP_ERROR

    def __init__(self, message: str = "per-run LLM spend cap reached"):
        super().__init__(message)


class RunHaltRequested(SpendCapError):
    """A human asked the current run to stop (dashboard Cancel).

    Subclasses SpendCapError on purpose: the pipeline already has exactly one
    cooperative halt point — ``assert_under_spend_cap()`` before every
    ``complete()`` — and exactly one handler for it, which leaves the ledger
    ``failed`` at the last persisted stage with the checkpoint intact and does
    not count a draft failure (ADR 012 §4). A cancel wants precisely that
    outcome, so it reuses that path rather than adding a second one. The
    distinct type exists only so the recorded reason says "cancelled", not
    "spend cap".
    """

    error_type = ErrorType.CANCELLED

    def __init__(self, message: str = "run halted at operator request"):
        super().__init__(message)


def classify_exception(exc: Exception) -> str:
    """Best-effort mapping. Unknown exceptions stay ANALYSIS_ERROR only if
    they came from an analysis call site — callers should pass a default."""
    name = type(exc).__name__
    msg = str(exc).lower()

    if isinstance(exc, RunHaltRequested) or name == "RunHaltRequested":
        return ErrorType.CANCELLED
    if isinstance(exc, SpendCapError) or name == "SpendCapError":
        return ErrorType.SPEND_CAP_ERROR
    if "429" in msg or "rate limit" in msg or name in {"RateLimitError"}:
        return ErrorType.RATE_LIMIT_ERROR
    if "401" in msg or "403" in msg or "authentication" in msg or name in {
        "AuthenticationError",
        "PermissionError",
    }:
        return ErrorType.AUTH_ERROR
    if "timeout" in msg or name in {"TimeoutException", "APITimeoutError", "ReadTimeout", "ConnectTimeout"}:
        return ErrorType.TIMEOUT_ERROR
    if "certificate" in msg or "ssl" in msg or "connection" in msg or name in {
        "ConnectError",
        "NetworkError",
        "HTTPStatusError",
    }:
        return ErrorType.NETWORK_ERROR
    return ErrorType.INGESTION_ERROR
