import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from uuid import UUID

from .core import DomainError

if os.name == "nt":
    from msvcrt import LK_LOCK, locking
else:
    from fcntl import LOCK_EX, flock


@contextmanager
def file_lock(*, path: Path) -> Iterator[None]:
    with ExitStack() as stack:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            file = stack.enter_context(path.open("ab"))
            if os.name == "nt":
                file.seek(0)
                locking(file.fileno(), LK_LOCK, 1)
            else:
                flock(file.fileno(), LOCK_EX)
        except OSError as error:
            raise DomainError(
                f"Falha ao bloquear {path}: {error.strerror}. Tente novamente."
            ) from error
        yield


def notion_task_lock(*, task_id: str) -> AbstractContextManager[None]:
    try:
        key = UUID(task_id).hex
    except ValueError as error:
        raise DomainError("Informe um ID de tarefa válido do Notion.") from error

    # Every process using the same user directory must lock the same page file.
    lock_dir = Path.home() / ".local" / "state" / "todo" / "task-locks"
    return file_lock(path=lock_dir / f"{key}.lock")
