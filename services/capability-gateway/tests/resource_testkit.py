from __future__ import annotations

import re
from collections.abc import Callable
from uuid import uuid4


class ResourceLedger:
    def __init__(self, prefix: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", prefix):
            raise ValueError("invalid resource prefix")
        self.namespace = f"{prefix}-{uuid4().hex}"
        self.owned: dict[str, str] = {}

    def created(self, name: str, identity: str) -> None:
        if not name.startswith(self.namespace + "-") or not re.fullmatch(r"[a-z0-9-]+", name) or not identity or name in self.owned:
            raise ValueError("resource ownership not established")
        self.owned[name] = identity

    def cleanup(self, inspect: Callable[[str], str | None], delete: Callable[[str], None]) -> None:
        errors = []
        for name, identity in tuple(self.owned.items()):
            try:
                current = inspect(name)
                if current is None:
                    del self.owned[name]
                    continue
                if current != identity:
                    raise ValueError(f"resource ownership changed: {name}")
                delete(name)
                del self.owned[name]
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("resource cleanup failed; unresolved resources retained", errors)


def preserve_errors(primary: BaseException | None, cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except BaseException as error:
        if primary is not None:
            raise BaseExceptionGroup("test and cleanup failed", [primary, error]) from None
        raise
    if primary is not None:
        raise primary
