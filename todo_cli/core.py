from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


class DomainError(Exception):
    """An operation cannot be performed with the supplied data or configuration."""


class TagApprovalRequired(DomainError):
    def __init__(self, *, column_id: str, column_name: str, value: str) -> None:
        self.column_id = column_id
        self.column_name = column_name
        self.value = value
        super().__init__(f"A opção {value!r} em {column_name} é nova e requer confirmação.")


@dataclass(frozen=True)
class Column:
    id: str
    name: str
    kind: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Destination:
    source_id: str
    status_id: str
    pending: str
    completed: str
    start_id: str | None = None
    finish_id: str | None = None
    columns: tuple[Column, ...] = ()
    field_aliases: dict[str, str] | None = None
    retired_field_aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    selections: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskChanges:
    title: str | None = None
    state: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    selections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    approved_tags: frozenset[tuple[str, str]] = frozenset()


class TaskStore(Protocol):
    def task_lock(self, task_id: str) -> AbstractContextManager[None]: ...

    def list(self, *, include_completed: bool = False) -> list[Task]: ...

    def get(self, task_id: str) -> Task: ...

    def create(self, title: str) -> Task: ...

    def update(self, task_id: str, changes: TaskChanges) -> Task: ...

    def delete(self, task_id: str) -> None: ...


class Tasks:
    def __init__(
        self,
        *,
        store: TaskStore,
        completed_state: str,
        records_start: bool = False,
        records_completion: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._completed_state = completed_state
        self._records_start = records_start
        self._records_completion = records_completion
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self, *, include_completed: bool = False) -> list[Task]:
        return self._store.list(include_completed=include_completed)

    def show(self, task_id: str) -> Task:
        return self._store.get(task_id)

    def add(self, title: str) -> Task:
        return self._store.create(self._title(title))

    def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        start_now: bool = False,
        selections: dict[str, tuple[str, ...]] | None = None,
        approved_tags: frozenset[tuple[str, str]] = frozenset(),
    ) -> Task:
        new_title = self._title(title) if title is not None else None
        if start_now and not self._records_start:
            raise DomainError(
                "Configure uma coluna de início para usar --start-now neste destino."
            )

        with self._store.task_lock(task_id):
            task = self.show(task_id)
            if title == task.title or new_title == task.title:
                new_title = None
            merged = {}
            for column_id, values in (selections or {}).items():
                requested = tuple(value.strip() for value in values)
                existing = task.selections.get(column_id, ())
                combined = tuple(dict.fromkeys((*existing, *requested)))
                if combined != existing:
                    merged[column_id] = combined

            started_at = self._clock() if start_now and task.started_at is None else None
            if new_title is None and started_at is None and not merged:
                return task

            return self._store.update(
                task.id,
                TaskChanges(
                    title=new_title,
                    started_at=started_at,
                    selections=merged,
                    approved_tags=approved_tags,
                ),
            )

    def done(self, task_id: str) -> Task:
        with self._store.task_lock(task_id):
            task = self.show(task_id)
            state = self._completed_state if task.state != self._completed_state else None
            completed_at = None
            if self._records_completion and task.completed_at is None:
                completed_at = self._clock()
            if state is None and completed_at is None:
                return task
            return self._store.update(task.id, TaskChanges(state=state, completed_at=completed_at))

    def delete(self, task_id: str) -> None:
        with self._store.task_lock(task_id):
            self._store.delete(self.show(task_id).id)

    @staticmethod
    def _title(title: str) -> str:
        if not title.strip():
            raise DomainError("Informe um título para a tarefa.")
        return title.strip()
