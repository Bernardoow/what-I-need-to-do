"""HTTP adapter for tasks stored in a Notion data source."""

import json
from contextlib import AbstractContextManager
from datetime import datetime
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import Column, Destination, DomainError, TagApprovalRequired, Task, TaskChanges
from .locking import notion_task_lock


NOTION_VERSION = "2026-03-11"
_REQUEST_TIMEOUT = 15
_TEXT_LIMIT = 2000
_HTTP_ERRORS = {
    400: (
        "O Notion recusou a solicitação. Confira os valores e execute win-td destination refresh."
    ),
    401: "O Notion recusou a autenticação. Confira NOTION_TOKEN.",
    403: "A integração do Notion não tem permissão para esta operação.",
    404: "Recurso do Notion não encontrado ou sem acesso para a integração.",
    409: "Houve um conflito ao salvar no Notion. Tente novamente.",
    429: "O limite de requisições do Notion foi atingido. Tente novamente em instantes.",
}


def _object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DomainError(f"Resposta inválida do Notion no campo {field}.")
    return value


def _array(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DomainError(f"Resposta inválida do Notion no campo {field}.")
    return value


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise DomainError(f"Resposta inválida do Notion no campo {field}.")
    return value


def _option_name(option: object) -> str:
    return _string(_object(option, field="opção").get("name"), field="name")


def _schema_options(definition: dict[str, object], *, kind: str) -> list[object]:
    settings = _object(definition.get(kind), field=kind)
    return _array(settings.get("options"), field="options")


def _same_id(actual: object, expected: str) -> bool:
    if actual == expected:
        return True
    if not isinstance(actual, str):
        return False
    try:
        return UUID(actual) == UUID(expected)
    except ValueError:
        return False


def _title_text(value: object) -> str:
    chunks = []
    for fragment in _array(value, field="title"):
        rich_text = _object(fragment, field="texto do título")
        if "plain_text" in rich_text:
            chunks.append(_string(rich_text["plain_text"], field="plain_text"))
        else:
            text = _object(rich_text.get("text"), field="text")
            chunks.append(_string(text.get("content"), field="content"))
    return "".join(chunks)


def _title_value(title: str) -> dict[str, object]:
    return {
        "title": [
            {"type": "text", "text": {"content": title[index : index + _TEXT_LIMIT]}}
            for index in range(0, len(title), _TEXT_LIMIT)
        ]
    }


def _date_value(value: object) -> datetime | None:
    if value is None:
        return None
    date = _object(value, field="date")
    start = _string(date.get("start"), field="start")
    try:
        moment = datetime.fromisoformat(start)
    except ValueError:
        raise DomainError("O Notion retornou uma data inválida.") from None
    zone = date.get("time_zone")
    if zone is not None and moment.tzinfo is None:
        try:
            return moment.replace(tzinfo=ZoneInfo(_string(zone, field="time_zone")))
        except (ValueError, ZoneInfoNotFoundError):
            raise DomainError("Não foi possível interpretar o fuso horário do Notion.") from None
    return moment


def _property_value(
    properties: dict[str, object], *, column_id: str, kind: str, required: bool = True
) -> object:
    entry = properties.get(column_id)
    if not isinstance(entry, dict) or entry.get("type") != kind or kind not in entry:
        if not required:
            return None
        raise DomainError(
            "A tarefa no Notion não contém uma coluna esperada. "
            "Execute win-td destination refresh."
        )
    return entry[kind]


class NotionClient:
    def __init__(self, token: str, *, base_url: str = "https://api.notion.com/v1") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _schema_properties(self, source_id: str) -> dict[str, object]:
        data = self._request(method="GET", path=f"/data_sources/{quote(source_id, safe='')}")
        return _object(data.get("properties"), field="properties")

    def schema(self, source_id: str) -> tuple[Column, ...]:
        columns = []
        for name, entry in self._schema_properties(source_id).items():
            definition = _object(entry, field="coluna")
            kind = _string(definition.get("type"), field="type")
            column_id = _string(definition.get("id"), field="id")
            choices = ()
            if kind in {"status", "multi_select"}:
                choices = tuple(
                    _option_name(option) for option in _schema_options(definition, kind=kind)
                )
            columns.append(Column(id=column_id, name=name, kind=kind, choices=choices))
        return tuple(columns)

    def _request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Notion-Version": NOTION_VERSION, "Accept": "application/json"},
            method=method,
        )
        # urllib excludes unredirected headers when following redirects.
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                payload = json.load(response)
        except HTTPError as error:
            code = error.code
            error.close()
            message = _HTTP_ERRORS.get(code, f"A API do Notion respondeu com HTTP {code}.")
            raise DomainError(message) from None
        except (OSError, HTTPException):
            raise DomainError("Não foi possível completar a conexão com o Notion.") from None
        except (ValueError, RecursionError):
            raise DomainError("A API do Notion retornou uma resposta inválida.") from None
        return _object(payload, field="resposta")


class NotionRepository:
    def __init__(self, client: NotionClient, destination: Destination) -> None:
        self._client = client
        self._destination = destination
        self._title_id = next(
            (column.id for column in destination.columns if column.kind == "title"), "title"
        )
        self._selection_ids = tuple(
            column.id for column in destination.columns if column.kind == "multi_select"
        )

    def task_lock(self, task_id: str) -> AbstractContextManager[None]:
        return notion_task_lock(task_id=task_id)

    def list(self, *, include_completed: bool = False) -> list[Task]:
        source_id = quote(self._destination.source_id, safe="")
        body: dict[str, object] = {"result_type": "page"}
        if not include_completed:
            status_id = self._destination.status_id
            body["filter"] = {
                "or": [
                    {
                        "property": status_id,
                        "status": {"does_not_equal": self._destination.completed},
                    },
                    {"property": status_id, "status": {"is_empty": True}},
                ]
            }

        tasks = []
        seen_cursors: set[str] = set()
        while True:
            response = self._client._request(
                method="POST", path=f"/data_sources/{source_id}/query", body=body
            )
            request_status = response.get("request_status")
            if isinstance(request_status, dict) and request_status.get("type") == "incomplete":
                raise DomainError("O Notion interrompeu a listagem sem retornar todas as tarefas.")
            for result in _array(response.get("results"), field="results"):
                page = _object(result, field="página")
                if not page.get("in_trash"):
                    task = self._task(page)
                    if include_completed or task.state != self._destination.completed:
                        tasks.append(task)

            has_more = response.get("has_more")
            if has_more is False:
                return tasks
            cursor = response.get("next_cursor")
            if has_more is not True or not isinstance(cursor, str) or not cursor:
                raise DomainError("O Notion não retornou uma paginação válida.")
            if cursor in seen_cursors:
                raise DomainError("O Notion repetiu um cursor de paginação.")
            seen_cursors.add(cursor)
            body["start_cursor"] = cursor

    def get(self, task_id: str) -> Task:
        page = self._client._request(method="GET", path=f"/pages/{quote(task_id, safe='')}")
        parent = _object(page.get("parent"), field="parent")
        if (
            page.get("in_trash")
            or parent.get("type") != "data_source_id"
            or not _same_id(parent.get("data_source_id"), self._destination.source_id)
            or not _same_id(page.get("id"), task_id)
        ):
            raise DomainError("Tarefa não encontrada no destino selecionado.")
        return self._task(page)

    def create(self, title: str) -> Task:
        body: dict[str, object] = {
            "parent": {"type": "data_source_id", "data_source_id": self._destination.source_id},
            "properties": {
                self._title_id: _title_value(title),
                self._destination.status_id: {"status": {"name": self._destination.pending}},
            },
        }
        return self._task(self._client._request(method="POST", path="/pages", body=body))

    def update(self, task_id: str, changes: TaskChanges) -> Task:
        properties: dict[str, object] = {}
        if changes.title is not None:
            properties[self._title_id] = _title_value(changes.title)
        if changes.state is not None:
            properties[self._destination.status_id] = {"status": {"name": changes.state}}

        for column_id, moment in (
            (self._destination.start_id, changes.started_at),
            (self._destination.finish_id, changes.completed_at),
        ):
            if moment is not None:
                if column_id is None:
                    raise DomainError("O destino não tem uma coluna para esse horário.")
                properties[column_id] = {"date": {"start": moment.isoformat()}}

        if changes.selections:
            properties.update(self._selection_properties(changes=changes))

        if not properties:
            return self.get(task_id)
        response = self._client._request(
            method="PATCH",
            path=f"/pages/{quote(task_id, safe='')}",
            body={"properties": properties},
        )
        return self._task(response)

    def _selection_properties(self, *, changes: TaskChanges) -> dict[str, object]:
        if not changes.selections.keys() <= set(self._selection_ids):
            raise DomainError("O destino não tem essa coluna de seleção múltipla.")

        source = self._client._schema_properties(self._destination.source_id)
        current: dict[str, tuple[str, dict[str, object]]] = {}
        for name, entry in source.items():
            definition = _object(entry, field="coluna")
            if definition.get("type") == "multi_select":
                column_id = _string(definition.get("id"), field="id")
                current[column_id] = (name, definition)

        properties: dict[str, object] = {}
        for column_id, names in changes.selections.items():
            if column_id not in current:
                raise DomainError(
                    "Uma coluna de seleção múltipla mudou. Execute win-td destination refresh."
                )
            column_name, definition = current[column_id]
            options: dict[str, str] = {}
            for entry in _schema_options(definition, kind="multi_select"):
                option = _object(entry, field="opção")
                options[_string(option.get("name"), field="name")] = _string(
                    option.get("id"), field="id"
                )

            writes = []
            for name in names:
                if not name.strip() or "," in name:
                    raise DomainError(
                        f"Informe um valor não vazio e sem vírgulas para {column_name}."
                    )
                if option_id := options.get(name):
                    writes.append({"id": option_id})
                elif (column_id, name) in changes.approved_tags:
                    writes.append({"name": name})
                else:
                    raise TagApprovalRequired(
                        column_id=column_id, column_name=column_name, value=name
                    )
            properties[column_id] = {"multi_select": writes}
        return properties

    def delete(self, task_id: str) -> None:
        self._client._request(
            method="PATCH", path=f"/pages/{quote(task_id, safe='')}", body={"in_trash": True}
        )

    def _task(self, page: dict[str, object]) -> Task:
        properties: dict[str, object] = {}
        for entry in _object(page.get("properties"), field="properties").values():
            item = _object(entry, field="coluna")
            properties[_string(item.get("id"), field="id")] = item

        title = _property_value(properties, column_id=self._title_id, kind="title")
        status = _property_value(properties, column_id=self._destination.status_id, kind="status")
        start_id = self._destination.start_id
        finish_id = self._destination.finish_id
        started_at = (
            _property_value(properties, column_id=start_id, kind="date", required=False)
            if start_id
            else None
        )
        completed_at = (
            _property_value(properties, column_id=finish_id, kind="date", required=False)
            if finish_id
            else None
        )
        selections = {}
        for column_id in self._selection_ids:
            selected = _property_value(
                properties, column_id=column_id, kind="multi_select", required=False
            )
            if selected is not None:
                selections[column_id] = tuple(
                    _option_name(option) for option in _array(selected, field="multi_select")
                )
        return Task(
            id=_string(page.get("id"), field="id"),
            title=_title_text(title),
            state=_option_name(status) if status is not None else self._destination.pending,
            started_at=_date_value(started_at),
            completed_at=_date_value(completed_at),
            selections=selections,
        )
