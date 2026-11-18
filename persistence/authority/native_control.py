"""House native controls carried by the admission ledger: the typed native Stop row and the process-Stop fence.

A native Stop is admitted through ``admissions.admit(native_stop=True)``: the store builds the owner-produced
``native_stop_v1`` descriptor (target generation, FIFO cutoff, whether the FIFO is paused behind an unknown turn),
persists the session's process-Stop fence and cancels the earlier queued messaging input it supersedes, all in the
admission's write. A caller-supplied payload carrying ``native_stop_v1`` is ``invalid_params``. While a Stop row is
queued, ``claim`` refuses ``control_pending`` and ``cancel`` refuses ``unsupported_control``; Stop rows never count
toward the FIFO cap. ``stop_fence`` refuses ``stale_generation`` when a Stop of the input's native route committed
after the input arrived.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .admissions import AdmissionRecord

__all__ = ["NativeControlAdmissions", "NativeStopFence"]


@dataclass(frozen=True, slots=True)
class NativeStopFence:
    """``admit`` refuses unless the native Stop watermark of ``route`` is still ``seen``."""

    route: str
    seen: int


@runtime_checkable
class NativeControlAdmissions(Protocol):
    """The native-control verbs the SQLite admission repository carries beside ``AdmissionRepository``."""

    def native_stop_watermark(self, *, session_id: str | None = None, route: str | None = None) -> int:
        """The highest ``seq`` of a native Stop row on the session (or native route); 0 when none."""
        ...

    def consume_native_stop(self, *, epoch: int, admission_id: str) -> AdmissionRecord:
        """Settle a queued Stop row ``cancelled`` (applied); a row without the descriptor is ``invalid_params``."""
        ...

    def interrupt_steer(self, *, epoch: int, admission_id: str, generation: int) -> bool:
        """Mark this owner's committed transfer ``interrupted`` so it never runs again; False when nothing changed."""
        ...

    def process_stop_pending(self, session_id: str) -> str | None:
        """The pending process-Stop token of the session."""
        ...

    def record_process_stop(self, *, epoch: int, session_id: str, generation: int) -> str:
        """Persist the process-Stop fence (first writer wins) and return the pending token."""
        ...

    def clear_process_stop(self, *, epoch: int, session_id: str, token: str) -> None:
        """Clear the fence when it still holds ``token``."""
        ...

    def check_epoch(self, *, epoch: int, reopen: bool = True) -> None:
        """``stale_epoch`` unless ``epoch`` still owns the store."""
        ...

    def native_receipts(self, *, epoch: int, principal_id: str, request_id: str) -> list[tuple[AdmissionRecord, str]]:
        """Every live row and retired tombstone of one provider message identity across the source's
        conversation resets, each with its stored ``payload_digest``."""
        ...

    def completion_cancelled(self, *, session_id: str, identity: str | None = None,
                             process_id: str | None = None) -> bool:
        """Whether a native Stop cancelled this process completion (any incarnation of ``process_id`` when
        ``identity`` is None)."""
        ...

    def cancel_process_completions(self, *, epoch: int, session_id: str,
                                   completions: Sequence[tuple[str, str]]) -> None:
        """Commit terminal cancellations for ``(identity, route)`` process completions; ``process_stop_unknown``
        when one cannot be proved."""
        ...
