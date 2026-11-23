import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .core import Column, Destination
from .terminal import Terminal


_RESERVED_FIELD_OPTIONS = {"--help", "--data-source-id", "--start-now", "--title"}
FieldOptions = tuple[tuple[str, Column], ...]


@dataclass(frozen=True)
class Edit:
    title: str | None = None
    start_now: bool = False
    selections: dict[Column, tuple[str, ...]] = field(default_factory=dict)


def _selection_columns(*, destination: Destination) -> dict[str, Column]:
    columns = sorted(destination.columns, key=lambda column: (column.name.casefold(), column.id))
    return {column.id: column for column in columns if column.kind == "multi_select"}


def _flag_base(*, column: Column) -> str:
    name = unicodedata.normalize("NFKD", column.name.casefold())
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    base = re.sub(r"[\W_]+", "-", name).strip("-") or "coluna"
    return f"--{base}"


def _allocate_flags(
    *,
    columns: dict[str, Column],
    previous_flags: dict[str, str] | None = None,
    retired_flags: dict[str, str] | None = None,
) -> dict[str, str]:
    flags = dict(previous_flags or {})
    reservations = retired_flags or {}
    bound_columns = set(flags.values())
    for column_id, column in columns.items():
        if column_id in bound_columns:
            continue
        base = _flag_base(column=column)
        option = base
        suffix = 2
        while (
            option in flags
            or option in _RESERVED_FIELD_OPTIONS
            or (option in reservations and reservations[option] != column_id)
        ):
            option = f"{base}-{suffix}"
            suffix += 1
        flags[option] = column_id
    return flags


def field_options(destination: Destination | None) -> FieldOptions:
    if destination is None:
        return ()
    columns = _selection_columns(destination=destination)
    flags = destination.field_aliases
    if flags is None:
        flags = _allocate_flags(columns=columns)
    return tuple(
        (option, columns[column_id]) for option, column_id in flags.items() if column_id in columns
    )


def with_field_aliases(
    destination: Destination, *, previous: Destination | None = None
) -> Destination:
    columns = _selection_columns(destination=destination)
    previous_flags = {}
    retired_flags = {}
    if previous is not None and previous.source_id == destination.source_id:
        retired_flags.update(previous.retired_field_aliases)
        old_columns = _selection_columns(destination=previous)
        flags = previous.field_aliases
        if flags is None:
            flags = _allocate_flags(columns=old_columns)
        for option, column_id in flags.items():
            old_column = old_columns.get(column_id)
            column = columns.get(column_id)
            keeps_flag = (
                old_column is not None
                and column is not None
                and _flag_base(column=old_column) == _flag_base(column=column)
            )
            if keeps_flag:
                previous_flags[option] = column_id
            else:
                # A legacy reservation with an empty ID has no recoverable owner.
                retired_flags[option] = column_id
    active_flags = _allocate_flags(
        columns=columns, previous_flags=previous_flags, retired_flags=retired_flags
    )
    return replace(
        destination,
        field_aliases=active_flags,
        retired_field_aliases={
            option: column_id
            for option, column_id in retired_flags.items()
            if option not in active_flags
        },
    )


def choose_edit(
    terminal: Terminal, destination: Destination, *, selection_options: FieldOptions
) -> Edit | None:
    actions: list[tuple[str, Callable[[], Edit]]] = [
        ("Editar título", lambda: Edit(title=terminal.prompt("Novo título")))
    ]
    if destination.start_id:
        actions.append(("Registrar início", lambda: Edit(start_now=True)))

    def choose_value(column: Column) -> Edit:
        value = terminal.prompt(f"Valor para {column.name}")
        return Edit(selections={column: (value,)})

    for _, column in selection_options:
        actions.append(
            (f"Adicionar valor em {column.name}", lambda column=column: choose_value(column))
        )
    index = terminal.choose("Ação", [label for label, _ in actions])
    return actions[index][1]() if index is not None else None
