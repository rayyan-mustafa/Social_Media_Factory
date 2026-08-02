"""Custom exception definitions for domain-driven error handling."""

class PipelineError(Exception):
    """Base exception for pipeline-related errors."""
    pass

class BenchmarkExceededError(PipelineError):
    """Raised when a specific pipeline step exceeds its SLA benchmark."""
    def __init__(self, step_name: str, duration: float, limit: float):
        self.step_name = step_name
        self.duration = duration
        self.limit = limit
        super().__init__(f"Benchmark failed in '{step_name}': took {duration:.2f}s (Limit: {limit:.2f}s)")

class ProviderAPIError(PipelineError):
    """Raised when a third-party provider (e.g. RunPod, MinIO) fails or times out."""
    def __init__(self, provider: str, status_code: int, message: str):
        self.provider = provider
        self.status_code = status_code
        self.message = message
        super().__init__(f"{provider} API Error [{status_code}]: {message}")

class WatchdogCriticalError(PipelineError):
    """Raised when the Watchdog detects a critical system failure."""
    def __init__(self, total_score: int):
        self.total_score = total_score
        super().__init__(f"System health critical. Watchdog score: {total_score}/100")
