import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import unquote
from uuid import uuid4


Json = dict[str, Any]
SOURCE_ID = "15acc0fe-c201-4aca-b349-cbd88de5e423"
SECOND_SOURCE_ID = "89a03d91-d89c-4b67-a2ca-8a7394a927df"
TITLE_ID = "title"
STATUS_ID = "status"
TAG_ID = "tags"
START_ID = "start"
FINISH_ID = "finish"


def option(*, name: str) -> Json:
    return {"id": str(uuid4()), "name": name, "color": "default"}


def data_source(
    *,
    item_id: str,
    title: str,
    status: str,
    pending: str,
    completed: str,
) -> Json:
    return {
        "object": "data_source",
        "id": item_id,
        "properties": {
            title: {"id": TITLE_ID, "type": "title", "title": {}},
            status: {
                "id": STATUS_ID,
                "type": "status",
                "status": {"options": [option(name=pending), option(name=completed)]},
            },
        },
    }


class FakeNotion(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), NotionHandler)
        self.pages: dict[str, Json] = {}
        self.sources = {
            SOURCE_ID: data_source(
                item_id=SOURCE_ID,
                title="Descrição",
                status="Status",
                pending="To do",
                completed="Done",
            ),
            SECOND_SOURCE_ID: data_source(
                item_id=SECOND_SOURCE_ID,
                title="Projeto",
                status="Andamento",
                pending="A fazer",
                completed="Entregue",
            ),
        }
        self.patches: list[tuple[str, Json]] = []
        self.page_size = 100

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class NotionHandler(BaseHTTPRequestHandler):
    server: FakeNotion

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        route, _, item_id = self.path.removeprefix("/v1/").partition("/")
        records = {"data_sources": self.server.sources, "pages": self.server.pages}
        if item_id in records.get(route, {}):
            self.respond(payload=records[route][item_id])
        else:
            self.missing()

    def do_POST(self) -> None:
        route = self.path.removeprefix("/v1/").split("/")
        body = self.read_body()
        if route == ["pages"]:
            self.create_page(body=body)
        elif len(route) == 3 and route[0] == "data_sources" and route[2] == "query":
            self.query(source_id=route[1], body=body)
        else:
            self.missing()

    def do_PATCH(self) -> None:
        route, _, item_id = self.path.removeprefix("/v1/").partition("/")
        if route != "pages" or item_id not in self.server.pages:
            self.missing()
            return
        body = self.read_body()
        page = self.server.pages[item_id]
        self.server.patches.append((item_id, body))
        if "in_trash" in body:
            page["in_trash"] = body["in_trash"]
        self.write_properties(page=page, values=body.get("properties", {}))
        self.respond(payload=page)

    def create_page(self, *, body: Json) -> None:
        page_id = str(uuid4())
        page = {
            "object": "page",
            "id": page_id,
            "parent": body["parent"],
            "properties": {},
            "in_trash": False,
        }
        self.write_properties(page=page, values=body["properties"])
        self.server.pages[page_id] = page
        self.respond(payload=page)

    def write_properties(self, *, page: Json, values: Json) -> None:
        source = self.server.sources[page["parent"]["data_source_id"]]
        schemas = source["properties"]
        if not page["properties"]:
            for name, schema in schemas.items():
                kind = schema["type"]
                empty = [] if kind in {"title", "multi_select"} else None
                page["properties"][name] = {"id": schema["id"], "type": kind, kind: empty}

        schema_names = {unquote(schema["id"]): name for name, schema in schemas.items()}
        for key, value in values.items():
            name = schema_names.get(unquote(key), key)
            schema = schemas[name]
            kind = schema["type"]
            content = value[kind]
            if kind == "title":
                content = [
                    {**fragment, "plain_text": fragment["text"]["content"]} for fragment in content
                ]
            elif kind == "status":
                content = self.find_option(options=schema[kind]["options"], value=content)
            elif kind == "multi_select":
                options = schema[kind]["options"]
                content = [self.tag_option(options=options, value=tag) for tag in content]
            page["properties"][name] = {"id": schema["id"], "type": kind, kind: content}

    def find_option(self, *, options: list[Json], value: Json) -> Json | None:
        return next(
            (
                choice
                for choice in options
                if choice.get("id") == value.get("id") or choice.get("name") == value.get("name")
            ),
            None,
        )

    def tag_option(self, *, options: list[Json], value: Json) -> Json:
        existing = self.find_option(options=options, value=value)
        if existing is not None:
            return existing
        new_option = option(name=value["name"])
        options.append(new_option)
        return new_option

    def query(self, *, source_id: str, body: Json) -> None:
        if source_id not in self.server.sources:
            self.missing()
            return
        filters = body.get("filter")
        pages = [
            page
            for page in self.server.pages.values()
            if page["parent"]["data_source_id"] == source_id
            and not page["in_trash"]
            and (not filters or self.matches_filter(page=page, filters=filters))
        ]
        start = int(body.get("start_cursor", "0"))
        end = start + self.server.page_size
        self.respond(
            payload={
                "object": "list",
                "results": pages[start:end],
                "has_more": end < len(pages),
                "next_cursor": str(end) if end < len(pages) else None,
            }
        )

    def matches_filter(self, *, page: Json, filters: Json) -> bool:
        if "or" in filters:
            return any(self.matches_filter(page=page, filters=part) for part in filters["or"])
        property_name = unquote(filters["property"])
        status = next(
            (
                value["status"]
                for name, value in page["properties"].items()
                if property_name in {name, unquote(value["id"])}
            ),
            None,
        )
        comparison = filters["status"]
        if "is_empty" in comparison:
            return status is None
        if status is None:
            return False
        if "does_not_equal" in comparison:
            return status["name"] != comparison["does_not_equal"]
        return status["name"] == comparison["equals"]

    def read_body(self) -> Json:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else {}

    def respond(self, *, payload: Json, status: int = 200) -> None:
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def missing(self) -> None:
        self.respond(
            payload={"object": "error", "code": "object_not_found", "message": "Not found"},
            status=404,
        )
