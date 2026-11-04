"""Processing counts with an explicit scope and unit; transport failures are separate."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProcessingCounts:
    unit: str
    scope: str
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    # Conflicts are a subset of rejected, not an additional count of received items.
    conflicts: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        counts = (self.received, self.accepted, self.rejected, self.duplicates, self.conflicts)
        if not self.unit.strip() or not self.scope.strip():
            raise ValueError("missing_processing_unit_or_scope")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts):
            raise ValueError("invalid_processing_counts")
        if self.received != self.accepted + self.rejected + self.duplicates:
            raise ValueError("inconsistent_processing_counts")
        if self.conflicts > self.rejected:
            raise ValueError("conflicts_exceed_rejected")
        if any(
            not isinstance(k, str)
            or not k.strip()
            or isinstance(v, bool)
            or not isinstance(v, int)
            or v <= 0
            for k, v in self.rejection_reasons.items()
        ):
            raise ValueError("invalid_processing_reasons")

    def database_counts(self):
        """Existing run columns; unit/scope/conflicts are explicit in the job result."""
        return dict(
            received=self.received,
            accepted=self.accepted,
            rejected=self.rejected,
            duplicates=self.duplicates,
            rejection_reasons=dict(self.rejection_reasons),
        )

    def as_dict(self):
        return dict(
            unit=self.unit, scope=self.scope, conflicts=self.conflicts, **self.database_counts()
        )


@dataclass(frozen=True)
class NormalizationOutcome:
    """Committed write decision, distinct from successful field parsing or HTTP receipt."""

    status: str
    error_code: str | None
    counts: ProcessingCounts
    details: dict = field(default_factory=dict)
