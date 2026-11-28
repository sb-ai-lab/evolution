"""A caller-provided trusted mapper; never loaded from trace strings."""
from typing import Protocol
from .models import MappingResult, TraceEnvelope


class PayloadMapper(Protocol):
    mapper_id: str
    mapper_version: str

    def map_trace(self, trace: TraceEnvelope) -> MappingResult: ...
