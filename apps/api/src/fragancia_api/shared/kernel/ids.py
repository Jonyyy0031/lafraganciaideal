import uuid


def new_id() -> uuid.UUID:
    """Time-ordered UUID (v7): unique without coordination and index-friendly."""
    return uuid.uuid7()
