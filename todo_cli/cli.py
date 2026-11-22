import argparse
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from uuid import UUID

from .config import Config, ConfigStore
from .core import Destination, DomainError, TagApprovalRequired, Task, Tasks
from .destinations import configure, remap_source, validate
from .editor import Edit, choose_edit, field_options, with_field_aliases
from .notion import NotionClient, NotionRepository
from .terminal import Terminal, clean_text


def _source_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-source-id",
        default=argparse.SUPPRESS,
        help="Fonte de dados do Notion para esta execução.",
    )


def _parser(destination: Destination | None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="win-td", description="Capture e organize tarefas pessoais.", allow_abbrev=False
    )
    _source_option(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    use = commands.add_parser("use", help="Escolher o destino ativo.")
    use.add_argument("name", metavar="DESTINO")

    manage = commands.add_parser("destination", help="Gerenciar cadastros de destinos.")
    actions = manage.add_subparsers(dest="destination_action", required=True)
    register = actions.add_parser("add", allow_abbrev=False, help="Cadastrar uma fonte do Notion.")
    register.add_argument("name", metavar="DESTINO")
    register.add_argument(
        "--replace", action="store_true", help="Refazer o mapeamento de um destino cadastrado."
    )
    _source_option(register)
    register.add_argument("--status-column", help="Nome da coluna do tipo Status.")
    register.add_argument("--pending-state", help="Valor de estado para tarefas pendentes.")
    register.add_argument("--completed-state", help="Valor de estado para tarefas concluídas.")
    register.add_argument("--start-column", help="Nome da coluna de horário de início.")
    register.add_argument("--finish-column", help="Nome da coluna de horário de conclusão.")
    refresh = actions.add_parser("refresh", help="Reler as colunas da fonte.")
    refresh.add_argument("name", nargs="?", metavar="DESTINO")
    remove = actions.add_parser("remove", help="Remover o cadastro local de um destino.")
    remove.add_argument("name", nargs="?", metavar="DESTINO", help="Omita para buscar por nome.")

    add = commands.add_parser("add", allow_abbrev=False, help="Criar uma tarefa pendente.")
    _source_option(add)
    add.add_argument("title", metavar="TÍTULO")
    listing = commands.add_parser("list", allow_abbrev=False, help="Listar tarefas e seus IDs.")
    _source_option(listing)
    listing.add_argument("--all", action="store_true", help="Incluir tarefas concluídas.")

    for name, help_text in (
        ("show", "Consultar uma tarefa."),
        ("update", "Atualizar uma tarefa."),
        ("done", "Concluir uma tarefa."),
        ("delete", "Enviar uma tarefa para a lixeira."),
    ):
        command = commands.add_parser(name, allow_abbrev=False, help=help_text)
        _source_option(command)
        command.add_argument(
            "task_id", nargs="?", metavar="ID", help="Omita para buscar por título."
        )
        if name != "update":
            continue
        command.add_argument("--title", help="Novo título da tarefa.")
        command.add_argument(
            "--start-now",
            action="store_true",
            help="Registrar o horário atual na coluna de início cadastrada.",
        )
        for index, (option, column) in enumerate(field_options(destination)):
            column_name = clean_text(column.name).replace("%", "%%")
            command.add_argument(
                option,
                action="append",
                metavar="VALOR",
                dest=f"selection_{index}",
                help=f"Acrescentar um valor na coluna {column_name!r}. Pode ser repetida.",
            )
    return parser


def _early_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="win-td", add_help=False, allow_abbrev=False)
    _source_option(parser)
    parser.add_argument("command", nargs="?")
    return parser.parse_known_args(arguments)[0]


def _source_override(args: argparse.Namespace) -> str | None:
    return getattr(args, "data_source_id", None) or os.environ.get("WIN_TD_DATA_SOURCE_ID")


def _source_id(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except ValueError as error:
        raise DomainError("Informe o UUID da fonte de dados do Notion.") from error


def _registered(*, config: Config, name: str | None) -> Destination:
    if name is None:
        raise DomainError("Selecione um destino ativo com win-td use DESTINO.")
    try:
        return config.destinations[name]
    except KeyError as error:
        raise DomainError(f"Destino não cadastrado: {name!r}.") from error


def _resolve_destination(
    config: Config, *, override: str | None, connect: Callable[[], NotionClient]
) -> Destination:
    if not override:
        return validate(_registered(config=config, name=config.active))
    source_id = _source_id(override)
    active = config.destinations.get(config.active) if config.active is not None else None
    if active and active.source_id == source_id:
        return validate(active)
    matching = [item for item in config.destinations.values() if item.source_id == source_id]
    if len(matching) == 1:
        return validate(matching[0])
    if matching:
        raise DomainError("Há vários destinos para esta fonte. Escolha um com win-td use DESTINO.")
    active = validate(_registered(config=config, name=config.active))
    return remap_source(active, source_id=source_id, columns=connect().schema(source_id))


def _manage_destination(
    args: argparse.Namespace,
    *,
    config: Config,
    store: ConfigStore,
    terminal: Terminal,
    connect: Callable[[], NotionClient],
) -> None:
    if args.command == "use":
        with store.edit() as latest:
            _registered(config=latest, name=args.name)
            latest.active = args.name
        print(f"Destino ativo: {args.name}")
        return

    if args.destination_action == "remove":
        _remove_destination(name=args.name, config=config, store=store, terminal=terminal)
        return

    if args.destination_action == "refresh":
        name = args.name if args.name is not None else config.active
        current = _registered(config=config, name=name)
        columns = connect().schema(current.source_id)
        try:
            updated = validate(replace(current, columns=columns))
        except DomainError as error:
            raise DomainError(
                f"{error} Reveja o mapeamento com "
                f"win-td destination add {shlex.quote(name)} --replace."
            ) from error
        _save_destination(
            store,
            name=name,
            previous=current,
            updated=with_field_aliases(updated, previous=current),
        )
        print(f"Destino atualizado: {name}")
        return

    name = args.name.strip()
    if not name or name != args.name or not name.isprintable():
        raise DomainError("Informe um nome válido para o destino.")
    current = config.destinations.get(name)
    if args.replace:
        registered = _registered(config=config, name=name)
        source = getattr(args, "data_source_id", None)
        source_id = _source_id(source) if source is not None else registered.source_id
    else:
        if current is not None:
            raise DomainError(f"O destino {name!r} já está cadastrado.")
        source = _source_override(args)
        if not source and terminal.interactive:
            source = terminal.prompt("ID da fonte de dados do Notion")
        if not source:
            raise DomainError("Informe a fonte do Notion com --data-source-id.")
        source_id = _source_id(source)
    updated = configure(
        source_id=source_id,
        columns=connect().schema(source_id),
        terminal=terminal,
        status_name=args.status_column,
        pending_state=args.pending_state,
        completed_state=args.completed_state,
        start_name=args.start_column,
        finish_name=args.finish_column,
    )
    _save_destination(
        store, name=name, previous=current, updated=with_field_aliases(updated, previous=current)
    )
    if args.replace:
        print(f"Destino atualizado: {name}")
    else:
        print(f"Destino cadastrado: {name}. Ative com win-td use {shlex.quote(name)}.")


def _remove_destination(
    *, name: str | None, config: Config, store: ConfigStore, terminal: Terminal
) -> None:
    if name is None:
        if not terminal.interactive:
            raise DomainError("Informe o nome do destino ou use um terminal interativo.")
        if not config.destinations:
            print("Nenhum destino cadastrado.")
            return
        names = sorted(config.destinations, key=str.casefold)
        index = terminal.choose(
            "Buscar destino para remover", [clean_text(item) for item in names]
        )
        if index is None:
            return
        name = names[index]
        if not terminal.confirm(f"Remover o cadastro de {name!r}?"):
            print("Remoção cancelada.")
            return

    previous = _registered(config=config, name=name)
    with store.edit() as latest:
        current = _registered(config=latest, name=name)
        _check_destination_unchanged(name=name, previous=previous, current=current)
        del latest.destinations[name]
        was_active = latest.active == name
        if was_active:
            latest.active = None
        has_destinations = bool(latest.destinations)

    print(f"Cadastro removido: {clean_text(name)}.")
    if not has_destinations:
        print("Cadastre um destino com win-td destination add DESTINO.")
    elif was_active:
        print("Escolha um destino ativo com win-td use DESTINO.")


def _save_destination(
    store: ConfigStore, *, name: str, previous: Destination | None, updated: Destination
) -> None:
    with store.edit() as config:
        current = config.destinations.get(name)
        _check_destination_unchanged(name=name, previous=previous, current=current)
        config.destinations[name] = updated


def _check_destination_unchanged(
    *, name: str, previous: Destination | None, current: Destination | None
) -> None:
    if current == previous:
        return
    if previous is None:
        raise DomainError(f"O destino {name!r} já está cadastrado.")
    raise DomainError(f"O destino {name!r} mudou durante a operação. Tente novamente.")


def _task_id(given: str | None, *, tasks: Tasks, terminal: Terminal) -> str | None:
    if given is not None:
        return given
    if not terminal.interactive:
        raise DomainError("Informe o ID da tarefa ou use um terminal interativo.")
    options = tasks.list(include_completed=True)
    if not options:
        print("Nenhuma tarefa disponível.")
        return None
    titles = [clean_text(task.title) for task in options]
    index = terminal.choose(
        "Buscar tarefa",
        [
            f"{title} · {clean_text(task.state)} · {task.id}"
            for title, task in zip(titles, options)
        ],
        search_texts=titles,
    )
    return options[index].id if index is not None else None


def _print_row(task: Task) -> None:
    print(f"{task.id}\t{clean_text(task.state)}\t{clean_text(task.title)}")


def _print_details(task: Task, destination: Destination) -> None:
    _print_row(task)
    if task.started_at is not None:
        print(f"Início: {task.started_at.isoformat()}")
    if task.completed_at is not None:
        print(f"Conclusão: {task.completed_at.isoformat()}")
    for column in destination.columns:
        if values := task.selections.get(column.id):
            print(f"{clean_text(column.name)}: {', '.join(map(clean_text, values))}")


def _read_edit(args: argparse.Namespace, destination: Destination) -> Edit:
    selections = {}
    for index, (_, column) in enumerate(field_options(destination)):
        if values := getattr(args, f"selection_{index}"):
            selections[column] = tuple(values)
    return Edit(title=args.title, start_now=args.start_now, selections=selections)


def _apply_edit(*, tasks: Tasks, task_id: str, edit: Edit, terminal: Terminal) -> Task | None:
    selections = {column.id: values for column, values in edit.selections.items()}
    approved_tags: set[tuple[str, str]] = set()
    while True:
        try:
            return tasks.update(
                task_id,
                title=edit.title,
                start_now=edit.start_now,
                selections=selections,
                approved_tags=frozenset(approved_tags),
            )
        except TagApprovalRequired as error:
            if not terminal.interactive:
                raise
            if not terminal.confirm(
                f"Criar a opção {error.value!r} na coluna {error.column_name}?"
            ):
                return None
            approved_tags.add((error.column_id, error.value))


def _run_task_command(
    args: argparse.Namespace,
    *,
    destination: Destination,
    client: NotionClient,
    terminal: Terminal,
) -> None:
    tasks = Tasks(
        store=NotionRepository(client, destination),
        completed_state=destination.completed,
        records_start=destination.start_id is not None,
        records_completion=destination.finish_id is not None,
    )
    if args.command == "list":
        for task in tasks.list(include_completed=args.all):
            _print_row(task)
        return
    if args.command == "add":
        _print_row(tasks.add(args.title))
        return

    task_id = _task_id(args.task_id, tasks=tasks, terminal=terminal)
    if task_id is None:
        return
    if args.command == "show":
        _print_details(tasks.show(task_id), destination)
    elif args.command == "done":
        _print_row(tasks.done(task_id))
    elif args.command == "delete":
        tasks.delete(task_id)
        print("Tarefa enviada para a lixeira.")
    else:
        edit = _read_edit(args, destination)
        if edit == Edit():
            if not terminal.interactive:
                raise DomainError(
                    "Informe pelo menos uma alteração. Consulte win-td update --help."
                )
            edit = choose_edit(terminal, destination)
        if edit is None:
            return
        task = _apply_edit(tasks=tasks, task_id=task_id, edit=edit, terminal=terminal)
        if task is not None:
            _print_row(task)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str], NotionClient] = NotionClient,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    terminal = Terminal()
    store = ConfigStore()

    def connect() -> NotionClient:
        token = os.environ.get("NOTION_TOKEN", "").strip()
        if not token:
            raise DomainError("Defina NOTION_TOKEN com o token da sua conexão do Notion.")
        return client_factory(token)

    try:
        config = None
        destination = None
        early = _early_args(arguments)
        if early.command == "update" and arguments[0] not in ("-h", "--help"):
            config = store.load()
            if config.active is not None or _source_override(early) is not None:
                destination = _resolve_destination(
                    config, override=_source_override(early), connect=connect
                )
        args = _parser(destination).parse_args(arguments)
        config = config if config is not None else store.load()
        if args.command in ("destination", "use"):
            _manage_destination(
                args, config=config, store=store, terminal=terminal, connect=connect
            )
        else:
            if destination is None:
                destination = _resolve_destination(
                    config, override=_source_override(args), connect=connect
                )
            _run_task_command(args, destination=destination, client=connect(), terminal=terminal)
        return 0
    except DomainError as error:
        print(f"Erro: {clean_text(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Operação cancelada.", file=sys.stderr)
        return 130
