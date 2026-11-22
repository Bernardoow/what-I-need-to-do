import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from socketserver import ThreadingMixIn

from tests.fake_notion import SOURCE_ID, TAG_ID, FakeNotion, NotionHandler, option
from tests.test_cli import LAUNCHER


class OverlappingPatchHandler(NotionHandler):
    server: "ConcurrentNotion"

    def do_PATCH(self) -> None:
        if self.server.first_patch.is_set():
            super().do_PATCH()
            self.server.second_patch_finished.set()
            return

        self.server.first_patch.set()
        # Without serialization, the second writer can read and write while this one waits.
        self.server.second_patch_finished.wait(timeout=3)
        super().do_PATCH()


class ConcurrentNotion(ThreadingMixIn, FakeNotion):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__()
        self.RequestHandlerClass = OverlappingPatchHandler
        self.first_patch = threading.Event()
        self.second_patch_finished = threading.Event()
        properties = self.sources[SOURCE_ID]["properties"]
        properties["Tag"] = {
            "id": TAG_ID,
            "type": "multi_select",
            "multi_select": {"options": [option(name="Revisão"), option(name="Financeiro")]},
        }


class ConcurrentUpdatesTests(unittest.TestCase):
    def test_same_task_keeps_both_tags_with_different_configs_and_caches(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server = self.enterContext(ConcurrentNotion())
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        env = {
            **os.environ,
            "NOTION_TOKEN": "test-token",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        env.pop("WIN_TD_DATA_SOURCE_ID", None)
        first_config = {
            **env,
            "WIN_TD_CONFIG": str(root / "first.json"),
            "XDG_CACHE_HOME": str(root / "first-cache"),
        }
        second_config = {
            **env,
            "WIN_TD_CONFIG": str(root / "second.json"),
            "XDG_CACHE_HOME": str(root / "second-cache"),
        }
        command = [sys.executable, "-c", LAUNCHER, server.url, str(root / "user")]

        def run(*args: str, config: dict[str, str]) -> str:
            result = subprocess.run(
                [*command, *args], env=config, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        for config in (first_config, second_config):
            run(
                "destination",
                "add",
                "work",
                "--data-source-id",
                SOURCE_ID,
                "--pending-state",
                "To do",
                "--completed-state",
                "Done",
                config=config,
            )
            run("use", "work", config=config)

        task_id = run("add", "Revisar o orçamento", config=first_config).split("\t", 1)[0]
        with subprocess.Popen(
            [*command, "update", task_id, "--tag", "Revisão"],
            env=first_config,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as first:
            if not server.first_patch.wait(timeout=5):
                stdout, stderr = first.communicate(timeout=5)
                self.fail(f"The first update did not write: {stdout}{stderr}")
            run("update", task_id, "--tag", "Financeiro", config=second_config)
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, stdout + stderr)

        tags = run("show", task_id, config=first_config).splitlines()[1]
        self.assertTrue(tags.startswith("Tag: "), tags)
        self.assertEqual(set(tags.removeprefix("Tag: ").split(", ")), {"Revisão", "Financeiro"})
