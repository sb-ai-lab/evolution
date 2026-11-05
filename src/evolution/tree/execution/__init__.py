"""Interactions, wire values, rollout and scoring, measurement journals and traces.

This layer also owns corpus loading and partitioning, command binding, policy
check values, output admission and authorization decisions. It owns their
values, digests and decision-file storage. Campaign code applies their effects.
It may import storage and other execution modules, never a higher package.
"""
