"""The shared test port allocator (#475): ports outside the ephemeral range, disjoint per xdist worker."""

import socket

import pytest

from ports import BASE, BLOCK, EPHEMERAL_FLOOR, PortAllocator


def test_each_worker_draws_from_its_own_block_below_the_ephemeral_range():
    first, second = PortAllocator("gw0", offset=0), PortAllocator("gw1", offset=0)
    drawn = [first.next() for _ in range(5)], [second.next() for _ in range(5)]
    assert all(BASE <= port < BASE + BLOCK for port in drawn[0])
    assert all(BASE + BLOCK <= port < BASE + 2 * BLOCK for port in drawn[1])
    assert max(drawn[1]) < EPHEMERAL_FLOOR


def test_a_run_without_xdist_uses_the_first_block():
    assert BASE <= PortAllocator("master", offset=0).next() < BASE + BLOCK


def test_consecutive_ports_differ_until_the_block_wraps():
    allocator = PortAllocator("gw2", offset=BLOCK - 2)
    assert [allocator.next() for _ in range(3)] == [BASE + 3 * BLOCK - 2, BASE + 3 * BLOCK - 1, BASE + 2 * BLOCK]


def test_a_port_someone_listens_on_is_skipped():
    allocator = PortAllocator("gw3", offset=0)
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", BASE + 3 * BLOCK))
        foreign.listen()
        assert allocator.next() == BASE + 3 * BLOCK + 1


def test_a_worker_whose_block_would_reach_the_ephemeral_range_is_refused():
    last = (EPHEMERAL_FLOOR - BASE) // BLOCK - 1
    PortAllocator(f"gw{last}", offset=0)
    with pytest.raises(ValueError, match="gw"):
        PortAllocator(f"gw{last + 1}", offset=0)
