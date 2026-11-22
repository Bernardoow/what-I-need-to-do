from dataclasses import replace

from .core import Column, Destination, DomainError
from .terminal import Terminal


def _column(
    columns: tuple[Column, ...], value: str, *, label: str, match_id: bool = True
) -> Column:
    for column in columns:
        if column.name == value:
            return column
    if match_id:
        for column in columns:
            if column.id == value:
                return column
    raise DomainError(f"Coluna de {label} indisponível: {value}.")


def _choose(*, terminal: Terminal, label: str, options: tuple[str, ...]) -> int:
    index = terminal.choose(label, options)
    if index is None:
        raise DomainError("Cadastro cancelado.")
    return index


def _status(columns: tuple[Column, ...], terminal: Terminal, *, name: str | None) -> Column:
    candidates = tuple(column for column in columns if column.kind == "status")
    if name is not None:
        return _column(candidates, name, label="estado")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise DomainError("A fonte precisa de uma coluna do tipo Status.")
    if not terminal.interactive:
        raise DomainError("Informe --status-column para selecionar a coluna de estado.")
    index = _choose(
        terminal=terminal, label="Coluna de estado", options=tuple(c.name for c in candidates)
    )
    return candidates[index]


def _state(
    column: Column, terminal: Terminal, *, value: str | None, label: str, option: str
) -> str:
    if not column.choices:
        raise DomainError(f"Cadastre valores de estado na coluna {column.name}.")
    if value is not None:
        if value not in column.choices:
            raise DomainError(f"Valor de estado inválido em {column.name}: {value}.")
        return value
    if not terminal.interactive:
        raise DomainError(f"Informe {option} para mapear o estado {label}.")
    index = _choose(terminal=terminal, label=f"Estado {label}", options=column.choices)
    return column.choices[index]


def _date(
    columns: tuple[Column, ...], terminal: Terminal, *, name: str | None, label: str
) -> str | None:
    candidates = tuple(column for column in columns if column.kind == "date")
    if name is not None:
        return _column(candidates, name, label=label).id
    if not candidates or not terminal.interactive:
        return None
    options = ("Sem coluna", *(column.name for column in candidates))
    index = _choose(terminal=terminal, label=f"Coluna de {label}", options=options)
    return candidates[index - 1].id if index else None


def configure(
    *,
    source_id: str,
    columns: tuple[Column, ...],
    terminal: Terminal,
    status_name: str | None,
    pending_state: str | None,
    completed_state: str | None,
    start_name: str | None,
    finish_name: str | None,
) -> Destination:
    status = _status(columns, terminal, name=status_name)
    pending = _state(
        status, terminal, value=pending_state, label="pendente", option="--pending-state"
    )
    completed = _state(
        status, terminal, value=completed_state, label="concluído", option="--completed-state"
    )
    return validate(
        Destination(
            source_id=source_id,
            status_id=status.id,
            pending=pending,
            completed=completed,
            start_id=_date(columns, terminal, name=start_name, label="início"),
            finish_id=_date(columns, terminal, name=finish_name, label="conclusão"),
            columns=columns,
        )
    )


def validate(destination: Destination) -> Destination:
    columns = {column.id: column for column in destination.columns}
    if sum(column.kind == "title" for column in columns.values()) != 1:
        raise DomainError("A fonte precisa de uma coluna do tipo título.")
    status = columns.get(destination.status_id)
    if status is None or status.kind != "status":
        raise DomainError("A coluna de estado cadastrada não está disponível na fonte.")
    if destination.pending == destination.completed:
        raise DomainError("Escolha valores diferentes para os estados pendente e concluído.")
    if not {destination.pending, destination.completed}.issubset(status.choices):
        raise DomainError(f"Os estados cadastrados não estão disponíveis em {status.name}.")
    if destination.start_id and destination.start_id == destination.finish_id:
        raise DomainError("Escolha colunas diferentes para início e conclusão.")
    for label, column_id in (
        ("início", destination.start_id),
        ("conclusão", destination.finish_id),
    ):
        if column_id and (column_id not in columns or columns[column_id].kind != "date"):
            raise DomainError(f"A coluna de {label} cadastrada não está disponível na fonte.")
    return destination


def remap_source(
    destination: Destination, *, source_id: str, columns: tuple[Column, ...]
) -> Destination:
    previous_columns = {column.id: column for column in destination.columns}

    def remap(column_id: str, *, label: str) -> str:
        previous = previous_columns[column_id]
        candidates = tuple(column for column in columns if column.kind == previous.kind)
        return _column(candidates, previous.name, label=label, match_id=False).id

    return validate(
        replace(
            destination,
            source_id=source_id,
            columns=columns,
            field_aliases=None,
            retired_field_aliases={},
            status_id=remap(destination.status_id, label="estado"),
            start_id=remap(destination.start_id, label="início") if destination.start_id else None,
            finish_id=remap(destination.finish_id, label="conclusão")
            if destination.finish_id
            else None,
        )
    )
