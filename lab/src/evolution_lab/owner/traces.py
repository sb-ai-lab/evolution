"""Private campaign export over Core's public formatter and Owner-held reads."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from evolution.tree import TraceSummary, export_run

from .binding import open_prepared
from .models import OwnerError


def trace_summary_payload(value: TraceSummary) -> dict:
    """The new closed service projection of Core's public counters."""
    return {'schema': 'lab-trace-summary/1', 'tries': value.tries, 'scored': value.scored,
            'failed': value.failed, 'unavailable': value.unavailable,
            'unverified': value.unverified, 'in_flight': value.in_flight,
            'unset_secrets': value.unset_secrets}


class TraceSupport:
    async def trace(self, ref, *, out: Path, secrets: Mapping[str, str],
                    disclose_private: bool, measurements=(), max_field_bytes=65536) -> TraceSummary:
        self._export_ref(ref, 'export')
        if disclose_private is not True:
            raise OwnerError('private_disclosure_required', 'export')
        if (not isinstance(secrets, Mapping)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in secrets.items())
                or type(max_field_bytes) is not int or max_field_bytes < 1
                or not isinstance(measurements, (tuple, list))
                or any(not isinstance(item, str) for item in measurements)):
            raise OwnerError('invalid_argument', 'export')
        try:
            destination = Path(out)
        except (TypeError, ValueError):
            raise OwnerError('invalid_argument', 'export') from None
        if str(destination) == '-':
            # A service worker's stdout is not a private export destination.
            raise OwnerError('invalid_argument', 'export')
        environment, selected = dict(secrets), tuple(measurements)
        async def work():
            status = await self._export_status(ref, 'export')
            if status.kind != 'campaign':
                raise OwnerError('trace_not_a_campaign', 'export')
            binding = await self._result_thread(open_prepared, self._storage, status.prepared_digest)
            try:
                return await self._result_thread(export_run,
                    run_dir=self.state_directory / 'artifacts' / ref.id / ref.id,
                    out=destination, manifest=binding.bundle.manifest_path,
                    measurements=selected, max_field_bytes=max_field_bytes, environ=environment)
            except Exception:
                # Core removes partial output on journal faults. Keep the public
                # Owner failure path-free without importing private Core errors.
                raise OwnerError('export_unavailable', 'export') from None
        return await self._result_work(work, self._result_io.acquire_read(ref.id, 'export'))
