"""Minimal ULID generator (Crockford base32, 48-bit time + 80-bit randomness).

ULIDs are lexicographically sortable by creation time, which matches the
Appendix format (cand_01J9X…) and is index-friendly at scale (append-mostly
inserts, no central sequence contention).

Non-determinism is safe for idempotency: an id is minted once at creation and
never re-minted on re-ingest (existing rows are matched, not recreated).
"""
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid() -> str:
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    return _encode(ms, 10) + _encode(rand, 16)


def candidate_id() -> str:
    return "cand_" + ulid()


def change_id() -> str:
    return "chg_" + ulid()


def run_id() -> str:
    return "run_" + ulid()
