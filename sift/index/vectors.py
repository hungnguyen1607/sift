import json


def to_pg(vec: list[float]) -> str:
    """pgvector text format; passed as text and cast with ::halfvec in SQL."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def from_pg(value: str | None) -> list[float] | None:
    return json.loads(value) if value else None
