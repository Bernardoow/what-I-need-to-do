import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fake_notion import SOURCE_ID, STATUS_ID, TAG_ID, FakeNotion, option
from todo_cli.core import Destination, DomainError, Tasks
from todo_cli.notion import NotionClient, NotionRepository


class TaskUpdateTests(unittest.TestCase):
    def test_core_requires_explicit_approval_to_create_a_tag(self) -> None:
        user_directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("pathlib.Path.home", return_value=user_directory))
        server = self.enterContext(FakeNotion())
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        existing = option(name="Trabalho")
        tags = {"options": [existing]}
        server.sources[SOURCE_ID]["properties"]["Tag"] = {
            "id": TAG_ID,
            "type": "multi_select",
            "multi_select": tags,
        }
        client = NotionClient("test-token", base_url=server.url)
        destination = Destination(
            source_id=SOURCE_ID,
            status_id=STATUS_ID,
            pending="To do",
            completed="Done",
            columns=client.schema(SOURCE_ID),
        )
        tasks = Tasks(store=NotionRepository(client, destination), completed_state="Done")
        task = tasks.add("Revisar contrato")
        tasks.update(task.id, selections={TAG_ID: ("Trabalho",)})

        with self.assertRaisesRegex(DomainError, "confirmação"):
            tasks.update(task.id, title="Novo título", selections={TAG_ID: ("Urgente",)})

        unchanged = tasks.show(task.id)
        self.assertEqual(unchanged.title, "Revisar contrato")
        self.assertEqual(unchanged.selections[TAG_ID], ("Trabalho",))
        self.assertEqual(tags["options"], [existing])

        tasks.update(
            task.id,
            selections={TAG_ID: ("Urgente",)},
            approved_tags=frozenset({(TAG_ID, "Urgente")}),
        )
        self.assertEqual(tasks.show(task.id).selections[TAG_ID], ("Trabalho", "Urgente"))
        self.assertEqual(
            server.patches[-1][1]["properties"][TAG_ID]["multi_select"],
            [{"id": existing["id"]}, {"name": "Urgente"}],
        )
