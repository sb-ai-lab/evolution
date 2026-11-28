"""Parse the explicit Core finding references shared by the two CLI forms."""
from evolution_lab.owner import Accepted, OwnerError
from .errors import UsageError


def acceptances(values, effect=None):
    accepted = []
    for value in values:
        code, separator, digest = value.partition(':')
        if not separator:
            raise UsageError('--accept requires CODE:DIGEST')
        try:
            accepted.append(Accepted(code, digest, effect if code == 'run_identity_changed' else None))
        except OwnerError:
            raise UsageError('--accept requires CODE and a SHA-256 digest') from None
    if effect is not None and not any(a.code == 'run_identity_changed' for a in accepted):
        raise UsageError('--effect requires an accepted run_identity_changed finding')
    return tuple(accepted)
