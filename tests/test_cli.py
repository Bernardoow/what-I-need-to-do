import errno
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.fake_notion import (
    FINISH_ID,
    SECOND_SOURCE_ID,
    SOURCE_ID,
    START_ID,
    STATUS_ID,
    TAG_ID,
    TITLE_ID,
    FakeNotion,
    data_source,
    option,
)


LAUNCHER = """\
import sys
from pathlib import Path
from unittest.mock import patch

from todo_cli.cli import main
from todo_cli.notion import NotionClient

if sys.stdin.isatty():
    import fcntl
    import termios

    # Ctrl+C only signals processes in the foreground of the controlling terminal.
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)

url, user_directory, *arguments = sys.argv[1:]
with patch('pathlib.Path.home', return_value=Path(user_directory)):
    raise SystemExit(main(arguments, client_factory=lambda token: NotionClient(token, base_url=url)))
"""


class TodoCliTests(unittest.TestCase):
    def setUp(self) -> None:
        config_directory = self.enterContext(tempfile.TemporaryDirectory())
        self.user_directory = Path(config_directory) / "user"
        self.env = {
            **os.environ,
            "WIN_TD_CONFIG": str(Path(config_directory) / "config.json"),
            "NOTION_TOKEN": "test-token",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        self.env.pop("WIN_TD_DATA_SOURCE_ID", None)
        self.server = self.enterContext(FakeNotion())
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            "-c",
            LAUNCHER,
            self.server.url,
            str(self.user_directory),
            *arguments,
        ]

    def cli(self, *arguments: str, expect_success: bool = True) -> str:
        process = subprocess.run(
            self.command(*arguments),
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        transcript = process.stdout + process.stderr
        self.assertEqual(process.returncode == 0, expect_success, transcript)
        self.assertNotIn("Traceback", transcript)
        return process.stdout if expect_success else process.stderr

    def interactive_cli(
        self,
        *arguments: str,
        steps: list[tuple[str, bytes | Callable[[], bytes]]],
        expected_returncode: int = 0,
    ) -> str:
        with ExitStack() as stack:
            master, slave = os.openpty()
            stack.callback(os.close, master)
            try:
                process = stack.enter_context(
                    subprocess.Popen(
                        self.command(*arguments),
                        env=self.env,
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        start_new_session=True,
                    )
                )
            finally:
                os.close(slave)

            try:
                transcript = bytearray()
                for marker, response in steps:
                    start = len(transcript)
                    expected = marker.encode()
                    deadline = time.monotonic() + 5
                    while expected not in transcript[start:]:
                        chunk = self.read_terminal(master=master, deadline=deadline)
                        self.assertTrue(
                            chunk, f"Missing {marker!r} in {transcript.decode(errors='replace')!r}"
                        )
                        transcript.extend(chunk)
                    keys = response() if callable(response) else response
                    os.write(master, keys)

                deadline = time.monotonic() + 5
                while chunk := self.read_terminal(master=master, deadline=deadline):
                    transcript.extend(chunk)
                output = transcript.decode(errors="replace")
                self.assertEqual(process.wait(timeout=5), expected_returncode, output)
                self.assertNotIn("Traceback", output)
                return output
            finally:
                if process.poll() is None:
                    process.kill()

    def read_terminal(self, *, master: int, deadline: float) -> bytes:
        ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
        self.assertTrue(ready, "Timed out waiting for terminal output")
        try:
            return os.read(master, 65536)
        except OSError as error:
            if error.errno == errno.EIO:
                return b""
            raise

    def register_work(self, *extra_options: str) -> None:
        self.cli(
            "destination",
            "add",
            "work",
            "--data-source-id",
            SOURCE_ID,
            "--status-column",
            "Status",
            "--pending-state",
            "To do",
            "--completed-state",
            "Done",
            *extra_options,
        )
        self.cli("use", "work")

    def register_second(self) -> None:
        self.cli(
            "destination",
            "add",
            "second",
            "--data-source-id",
            SECOND_SOURCE_ID,
            "--status-column",
            "Andamento",
            "--pending-state",
            "A fazer",
            "--completed-state",
            "Entregue",
        )

    def add_task(self, *extra_options: str, title: str) -> str:
        before = set(self.server.pages)
        self.cli("add", title, *extra_options)
        new_ids = self.server.pages.keys() - before
        self.assertEqual(len(new_ids), 1)
        return new_ids.pop()

    def include_tag_column(
        self,
        *,
        name: str = "Tag",
        source_id: str = SOURCE_ID,
        column_id: str = TAG_ID,
        choices: tuple[str, ...] = ("Revisão", "Financeiro"),
    ) -> None:
        schemas = self.server.sources[source_id]["properties"]
        schemas[name] = {
            "id": column_id,
            "type": "multi_select",
            "multi_select": {"options": [option(name=choice) for choice in choices]},
        }

    def assert_property_changes(self, *, task_id: str, expected: set[str]) -> None:
        changed_id, payload = self.server.patches[-1]
        properties = self.server.pages[task_id]["properties"]
        keys = {properties.get(key, {}).get("id", key) for key in payload["properties"]}
        self.assertEqual(changed_id, task_id)
        self.assertEqual(keys, expected)

    def assert_edit_option(self, *, help_text: str, flag: str, column_name: str) -> None:
        self.assertIn(
            f"{flag} VALOR Acrescentar um valor na coluna {column_name!r}.",
            " ".join(help_text.split()),
        )

    def assert_selection_changes(self, *, task_id: str, expected: dict[str, list[str]]) -> None:
        self.assert_property_changes(task_id=task_id, expected=set(expected))
        properties = self.server.pages[task_id]["properties"].values()
        selections = {
            prop["id"]: [tag["name"] for tag in prop["multi_select"]]
            for prop in properties
            if prop["id"] in expected
        }
        self.assertEqual(selections, expected)

    def test_help_and_parser_errors_use_the_installed_command_name(self) -> None:
        general_help = self.cli("--help")
        self.assertIn("usage: win-td ", general_help)
        self.assertIn("+ Pedir o título da tarefa no terminal.", " ".join(general_help.split()))
        self.assertIn("usage: win-td update ", self.cli("update", "--help"))
        self.assertIn("win-td: error:", self.cli("--data-source-id", expect_success=False))

    def test_configuration_defaults_to_win_td_directory(self) -> None:
        del self.env["WIN_TD_CONFIG"]
        self.env.pop("XDG_CONFIG_HOME", None)
        self.register_work()

        self.assertTrue((self.user_directory / ".config" / "win-td" / "config.json").is_file())

        config_home = self.user_directory / "xdg"
        self.env["XDG_CONFIG_HOME"] = str(config_home)
        self.register_work()

        self.assertTrue((config_home / "win-td" / "config.json").is_file())

    def test_add_creates_a_pending_task_in_the_active_destination(self) -> None:
        self.register_work()

        for title in ("Revisar o orçamento", "+"):
            with self.subTest(title=title):
                task_id = self.add_task(title=title)

                page = self.server.pages[task_id]
                self.assertEqual(page["parent"]["data_source_id"], SOURCE_ID)
                self.assertEqual(page["properties"]["Status"]["status"]["name"], "To do")
                self.assertEqual(page["properties"]["Descrição"]["title"][0]["plain_text"], title)
                self.assertIn(f"{task_id}\tTo do\t{title}\n", self.cli("list"))

    def test_plus_creates_one_pending_task_with_a_free_text_title(self) -> None:
        self.register_work()
        title = 'Revisar orçamento "final" & conferir R$ 50'

        output = self.interactive_cli("+", steps=[("Título: ", f"  {title}  \r".encode())])

        self.assertEqual(len(self.server.pages), 1)
        task_id, page = next(iter(self.server.pages.items()))
        self.assertEqual(page["parent"]["data_source_id"], SOURCE_ID)
        self.assertEqual(page["properties"]["Status"]["status"]["name"], "To do")
        self.assertEqual(page["properties"]["Descrição"]["title"][0]["plain_text"], title)
        self.assertIn(f"{task_id}\tTo do\t{title}", output)

    def test_plus_repeats_the_prompt_after_blank_titles(self) -> None:
        self.register_work()
        title = "Enviar a proposta"

        output = self.interactive_cli(
            "+",
            steps=[
                ("Título: ", b"\r"),
                ("Título: ", b"   \r"),
                ("Título: ", f"{title}\r".encode()),
            ],
        )

        self.assertEqual(output.count("Digite um título para continuar."), 2)
        self.assertEqual(output.count("Título: "), 3)
        self.assertEqual(len(self.server.pages), 1)
        self.assertTrue(self.cli("list").endswith(f"\tTo do\t{title}\n"))

    def test_plus_cancels_on_end_of_input(self) -> None:
        self.register_work()

        for keys in (b"\x04", b"Rascunho\x04"):
            with self.subTest(keys=keys):
                output = self.interactive_cli("+", steps=[("Título: ", keys)])

                self.assertEqual(output.count("Título: "), 1)
                self.assertEqual(self.server.pages, {})

    def test_plus_cancels_on_ctrl_c(self) -> None:
        self.register_work()

        output = self.interactive_cli("+", steps=[("Título: ", b"\x03")], expected_returncode=130)

        self.assertIn("Operação cancelada.", output)
        self.assertEqual(self.server.pages, {})

    def test_plus_rejects_a_title_with_incomplete_multibyte_text(self) -> None:
        self.register_work()
        self.env["PYTHONIOENCODING"] = "utf-8"

        output = self.interactive_cli(
            "+", steps=[("Título: ", b"Rascunho \xc3\r")], expected_returncode=1
        )

        self.assertIn(
            "Erro: O título contém texto inválido para a codificação do terminal.", output
        )
        self.assertEqual(self.server.pages, {})

    def test_plus_requires_an_interactive_terminal(self) -> None:
        error = self.cli("+", expect_success=False)

        self.assertIn("terminal interativo", error)
        self.assertIn("win-td add 'título da tarefa'", error)
        self.assertNotIn("Título: ", error)
        self.assertEqual(self.server.pages, {})

    def test_plus_applies_source_overrides_for_one_execution(self) -> None:
        self.register_work()
        self.register_second()
        active_title = "Revisar o orçamento"
        active_task = self.add_task(title=active_title)

        for options, env_source_id, title in (
            ((), SECOND_SOURCE_ID, "Revisar pagamentos"),
            (("--data-source-id", SECOND_SOURCE_ID), SOURCE_ID, "Agendar a reunião"),
        ):
            with self.subTest(options=options):
                before = set(self.server.pages)
                self.env["WIN_TD_DATA_SOURCE_ID"] = env_source_id
                self.interactive_cli("+", *options, steps=[("Título: ", f"{title}\r".encode())])
                del self.env["WIN_TD_DATA_SOURCE_ID"]

                self.assertEqual(self.cli("list"), f"{active_task}\tTo do\t{active_title}\n")
                new_ids = self.server.pages.keys() - before
                self.assertEqual(len(new_ids), 1)
                page = self.server.pages[new_ids.pop()]
                self.assertEqual(page["parent"]["data_source_id"], SECOND_SOURCE_ID)

    def test_switching_destinations_persists_across_temporary_overrides(self) -> None:
        self.register_work()
        self.register_second()
        first_task = self.add_task(title="Revisar o orçamento")
        self.cli("use", "second")
        second_task = self.add_task(title="Responder ao cliente")

        self.assertNotIn(first_task, self.cli("list"))
        self.assertIn(second_task, self.cli("list"))
        self.assertIn(first_task, self.cli("--data-source-id", SOURCE_ID, "list"))
        self.env["WIN_TD_DATA_SOURCE_ID"] = SOURCE_ID
        self.assertIn(first_task, self.cli("list"))
        self.assertIn(second_task, self.cli("--data-source-id", SECOND_SOURCE_ID, "list"))
        del self.env["WIN_TD_DATA_SOURCE_ID"]
        self.assertIn(second_task, self.cli("list"))

        other_page = self.server.pages[second_task]
        self.assertEqual(other_page["parent"]["data_source_id"], SECOND_SOURCE_ID)
        self.assertEqual(other_page["properties"]["Andamento"]["status"]["name"], "A fazer")
        self.assertEqual(
            other_page["properties"]["Projeto"]["title"][0]["plain_text"], "Responder ao cliente"
        )
        self.assertIn("destino", self.cli("delete", first_task, expect_success=False))
        self.assertFalse(self.server.pages[first_task]["in_trash"])
        self.cli("use", "work")
        self.assertIn(first_task, self.cli("list"))

    def test_update_options_follow_registered_and_unregistered_source_overrides(self) -> None:
        self.include_tag_column(column_id="old-active-tags")
        self.register_work()
        schemas = self.server.sources[SOURCE_ID]["properties"]
        schemas["Área"] = schemas.pop("Tag")
        self.include_tag_column()
        self.cli("destination", "refresh")
        active_task = self.add_task(title="Tarefa do destino ativo")

        self.include_tag_column(source_id=SECOND_SOURCE_ID, column_id="retired-tags")
        self.register_second()
        schemas = self.server.sources[SECOND_SOURCE_ID]["properties"]
        schemas["Categoria"] = schemas.pop("Tag")
        self.include_tag_column(source_id=SECOND_SOURCE_ID, column_id="registered-tags")
        self.cli("destination", "refresh", "second")

        unregistered_source = "966a7c58-97b8-47bc-959a-50228c9236c8"
        self.server.sources[unregistered_source] = data_source(
            item_id=unregistered_source,
            title="Descrição",
            status="Status",
            pending="To do",
            completed="Done",
        )
        self.include_tag_column(source_id=unregistered_source, column_id="temporary-tags")

        for source_id, column_id, flag in (
            (SECOND_SOURCE_ID, "registered-tags", "--tag-2"),
            (unregistered_source, "temporary-tags", "--tag"),
        ):
            with self.subTest(source_id=source_id):
                title = f"Tarefa com {column_id}"
                task_id = self.add_task("--data-source-id", source_id, title=title)
                self.env["WIN_TD_DATA_SOURCE_ID"] = source_id
                help_text = self.cli("update", "--help")
                self.assert_edit_option(help_text=help_text, flag=flag, column_name="Tag")
                if source_id == SECOND_SOURCE_ID:
                    self.assert_edit_option(
                        help_text=help_text, flag="--categoria", column_name="Categoria"
                    )

                self.interactive_cli(
                    "update",
                    steps=[
                        (title, b"\r"),
                        ("Adicionar valor em Tag", b"Tag\r"),
                        ("Valor para Tag: ", "Revisão\r".encode()),
                    ],
                )
                self.assert_selection_changes(task_id=task_id, expected={column_id: ["Revisão"]})

                self.env["WIN_TD_DATA_SOURCE_ID"] = SOURCE_ID
                self.cli("update", task_id, "--data-source-id", source_id, flag, "Financeiro")
                self.assert_selection_changes(
                    task_id=task_id, expected={column_id: ["Revisão", "Financeiro"]}
                )

        del self.env["WIN_TD_DATA_SOURCE_ID"]
        self.cli("update", active_task, "--tag-2", "Revisão")
        self.assert_selection_changes(task_id=active_task, expected={TAG_ID: ["Revisão"]})

    def test_interactive_destination_removal_clears_active_selection(self) -> None:
        self.register_work()
        self.cli(
            "destination",
            "add",
            "archive",
            "--data-source-id",
            SOURCE_ID,
            "--pending-state",
            "To do",
            "--completed-state",
            "Done",
        )
        task_id = self.add_task(title="Revisar o orçamento")
        del self.env["NOTION_TOKEN"]

        output = self.interactive_cli(
            "destination",
            "remove",
            steps=[("work", b"WORK\r"), ("Remover o cadastro de 'work'? [s/N]", b"s\r")],
        )

        self.assertIn("Cadastro removido: work.", output)
        self.assertIn("win-td use DESTINO", output)
        self.assertIn("Selecione um destino ativo", self.cli("list", expect_success=False))
        self.assertIn("Destino não cadastrado", self.cli("use", "work", expect_success=False))
        self.assertIn("Destino ativo: archive", self.cli("use", "archive"))
        self.assertFalse(self.server.pages[task_id]["in_trash"])

    def test_interactive_destination_removal_can_be_canceled(self) -> None:
        self.register_work()
        task_id = self.add_task(title="Revisar o orçamento")

        self.interactive_cli("destination", "remove", steps=[("work", b"\x1b")])
        self.assertIn(task_id, self.cli("list"))

        output = self.interactive_cli(
            "destination",
            "remove",
            steps=[("work", b"\r"), ("Remover o cadastro de 'work'? [s/N]", b"\r")],
        )
        self.assertIn("Remoção cancelada.", output)
        self.assertIn(task_id, self.cli("list"))

    def test_destination_removal_by_name_allows_removing_the_last_registration(self) -> None:
        self.register_work()
        self.assertIn(
            "Informe o nome do destino",
            self.cli("destination", "remove", expect_success=False),
        )

        output = self.cli("destination", "remove", "work")

        self.assertIn("win-td destination add DESTINO", output)
        self.assertIn("Selecione um destino ativo", self.cli("list", expect_success=False))
        self.assertIn(
            "Destino não cadastrado",
            self.cli("destination", "remove", "work", expect_success=False),
        )
        empty = self.interactive_cli("destination", "remove", steps=[])
        self.assertIn("Nenhum destino cadastrado.", empty)

    def test_removing_an_inactive_destination_keeps_the_active_destination(self) -> None:
        self.register_work()
        task_id = self.add_task(title="Revisar o orçamento")
        self.cli(
            "destination",
            "add",
            "archive",
            "--data-source-id",
            SOURCE_ID,
            "--pending-state",
            "To do",
            "--completed-state",
            "Done",
        )

        self.cli("destination", "remove", "archive")

        self.assertIn("Destino não cadastrado", self.cli("use", "archive", expect_success=False))
        self.assertIn(task_id, self.cli("list"))

    def test_interactive_destination_removal_rejects_a_concurrent_refresh(self) -> None:
        self.register_work()
        self.include_tag_column()

        def refresh_then_confirm() -> bytes:
            self.cli("destination", "refresh", "work")
            return b"s\r"

        output = self.interactive_cli(
            "destination",
            "remove",
            steps=[("work", b"\r"), ("Remover o cadastro de 'work'? [s/N]", refresh_then_confirm)],
            expected_returncode=1,
        )

        self.assertIn("O destino 'work' mudou durante a operação", output)
        self.assertIn("--tag", self.cli("update", "--help"))

    def test_show_update_done_and_delete_by_id(self) -> None:
        self.include_tag_column()
        self.register_work()
        self.server.page_size = 1
        task_id = self.add_task(title="Organizar comprovantes")
        self.add_task(title="Organizar agenda")
        self.cli("update", task_id, "--tag", "Revisão")

        self.assertIn("Organizar comprovantes", self.cli("show", task_id))
        self.cli("update", task_id, "--title", "Conferir comprovantes")
        self.assertIn("Conferir comprovantes", self.cli("show", task_id))
        self.assert_property_changes(task_id=task_id, expected={TITLE_ID})

        patch_count = len(self.server.patches)
        self.assertIn("início", self.cli("update", task_id, "--start-now", expect_success=False))
        self.assertEqual(len(self.server.patches), patch_count)

        self.cli("done", task_id)
        self.assert_property_changes(task_id=task_id, expected={STATUS_ID})
        properties = self.server.pages[task_id]["properties"]
        self.assertEqual(properties["Status"]["status"]["name"], "Done")
        self.assertEqual([tag["name"] for tag in properties["Tag"]["multi_select"]], ["Revisão"])
        self.assertNotIn(task_id, self.cli("list"))
        all_tasks = self.cli("list", "--all")
        self.assertIn(task_id, all_tasks)
        self.assertIn("Organizar agenda", all_tasks)

        self.cli("delete", task_id)
        self.assertTrue(self.server.pages[task_id]["in_trash"])
        self.assertNotIn(task_id, self.cli("list", "--all"))

    def test_search_uses_typed_text_and_arrows_before_applying_options(self) -> None:
        self.register_work()
        self.add_task(title="Arquivar notas")
        first_match = self.add_task(title="Revisar agenda")
        second_match = self.add_task(title="Revisar orçamento")

        self.interactive_cli(
            "update",
            "--title",
            "Conferir orçamento",
            steps=[("Revisar orçamento", b"REVISAR\x1b[B\r")],
        )

        self.assertIn("Revisar agenda", self.cli("show", first_match))
        self.assertIn("Conferir orçamento", self.cli("show", second_match))
        self.assertEqual(self.server.patches[-1][0], second_match)

        shown = self.interactive_cli(
            "show",
            steps=[
                ("Conferir orçamento", b"TO DO"),
                ("Nenhuma opção encontrada.", b"\x7f" * 5 + b"CONFERIR\r"),
            ],
        )
        self.assertIn(f"{second_match}\tTo do\tConferir orçamento", shown)

    def test_done_search_includes_completed_tasks_only_with_all(self) -> None:
        self.register_work()
        pending_title = "Revisar agenda atual"
        completed_title = "Revisar agenda antiga"
        pending_id = self.add_task(title=pending_title)
        completed_id = self.add_task(title=completed_title)
        self.cli("done", completed_id)

        shown = self.interactive_cli(
            "done",
            "--all",
            steps=[("Esc cancela", b"ANTIGA"), ("Buscar: ANTIGA", b"\r")],
        )
        initial_screen = shown.partition("Buscar: A")[0]
        self.assertIn(f"{pending_title} · To do", initial_screen)
        self.assertIn(f"{completed_title} · Done", initial_screen)
        self.assertIn(f"{completed_id}\tDone\t{completed_title}", shown)

        shown = self.interactive_cli("done", steps=[(pending_title, b"\r")])
        self.assertNotIn(completed_title, shown)
        self.assertIn(f"{pending_id}\tDone\t{pending_title}", shown)

        empty = self.interactive_cli("done", steps=[])
        self.assertIn("Nenhuma tarefa disponível.", empty)

    def test_interactive_update_offers_actions_and_delete_can_be_canceled(self) -> None:
        self.include_tag_column()
        self.register_work()
        task_id = self.add_task(title="Revisar o orçamento")

        self.interactive_cli(
            "update",
            steps=[
                ("Revisar o orçamento", b"\r"),
                ("Adicionar valor em Tag", b"\r"),
                ("Novo título: ", "Conferir o orçamento\r".encode()),
            ],
        )
        self.assertIn("Conferir o orçamento", self.cli("show", task_id))

        self.interactive_cli("delete", steps=[("Conferir o orçamento", b"\x1b")])
        self.assertIn(task_id, self.cli("list"))
        self.cli("done", expect_success=False)
        self.assertIn(task_id, self.cli("list"))

    def test_help_uses_the_stored_schema_until_destination_refresh(self) -> None:
        self.include_tag_column()
        self.register_work()
        self.assertIn("--tag", self.cli("update", "--help"))
        schemas = self.server.sources[SOURCE_ID]["properties"]
        schemas["Área de Atuação (%)"] = schemas.pop("Tag")

        self.assertIn("--tag", self.cli("update", "--help"))
        self.cli("destination", "refresh")
        help_text = self.cli("update", "--help")
        self.assertIn("--area-de-atuacao", help_text)
        self.assertIn("Área de Atuação (%)", help_text)
        self.assertNotIn("--tag", help_text)

        task_id = self.add_task(title="Revisar a proposta")
        self.cli("update", task_id, "--area-de-atuacao", "Revisão")
        self.assert_property_changes(task_id=task_id, expected={TAG_ID})

    def test_legacy_destination_without_options_uses_its_saved_columns(self) -> None:
        self.include_tag_column(name="Área")
        self.register_work()
        path = Path(self.env["WIN_TD_CONFIG"])
        config = json.loads(path.read_text(encoding="utf-8"))
        del config["destinations"]["work"]["field_aliases"]
        del config["destinations"]["work"]["retired_field_aliases"]
        path.write_text(json.dumps(config), encoding="utf-8")
        task_id = self.add_task(title="Revisar o cadastro antigo")

        self.assert_edit_option(
            help_text=self.cli("update", "--help"), flag="--area", column_name="Área"
        )
        self.cli("update", task_id, "--area", "Revisão")
        self.assert_selection_changes(task_id=task_id, expected={TAG_ID: ["Revisão"]})

    def test_add_and_list_work_after_an_optional_column_is_removed(self) -> None:
        self.include_tag_column()
        self.register_work()
        del self.server.sources[SOURCE_ID]["properties"]["Tag"]

        task_id = self.add_task(title="Acompanhar a proposta")

        listing = self.cli("list")
        self.assertIn(task_id, listing)
        self.assertIn("Acompanhar a proposta", listing)

    def test_refreshed_help_menu_and_arguments_agree_for_colliding_options(self) -> None:
        self.include_tag_column(name="Tag de equipe", choices=("Revisão", "Operações", "Jurídico"))
        self.register_work()
        schemas = self.server.sources[SOURCE_ID]["properties"]
        schemas["Tag-de-equipe"] = schemas.pop("Tag de equipe")
        self.include_tag_column(
            name="Tag de equipe", column_id="new-tags", choices=("Financeiro",)
        )
        self.cli("destination", "refresh")
        task_id = self.add_task(title="Preparar apresentação")

        help_text = self.cli("update", "--help")
        self.assert_edit_option(
            help_text=help_text, flag="--tag-de-equipe", column_name="Tag-de-equipe"
        )
        self.assert_edit_option(
            help_text=help_text, flag="--tag-de-equipe-2", column_name="Tag de equipe"
        )
        menu = self.interactive_cli(
            "update",
            task_id,
            steps=[
                ("Adicionar valor em Tag de equipe", b"Tag-de-equipe\r"),
                ("Valor para Tag-de-equipe: ", "Revisão\r".encode()),
            ],
        )
        self.assertIn("Adicionar valor em Tag-de-equipe", menu)
        self.assert_selection_changes(task_id=task_id, expected={TAG_ID: ["Revisão"]})

        self.cli(
            "update",
            task_id,
            "--tag-de-equipe-2",
            "Financeiro",
            "--tag-de-equipe",
            "Operações",
            "--tag-de-equipe",
            "Jurídico",
        )
        self.assert_selection_changes(
            task_id=task_id,
            expected={TAG_ID: ["Revisão", "Operações", "Jurídico"], "new-tags": ["Financeiro"]},
        )
        details = self.cli("show", task_id)
        self.assertIn("Tag-de-equipe: Revisão, Operações, Jurídico", details)
        self.assertIn("Tag de equipe: Financeiro", details)

    def test_refresh_only_restores_a_retired_option_to_the_original_column(self) -> None:
        self.include_tag_column()
        self.register_work()
        schemas = self.server.sources[SOURCE_ID]["properties"]
        schemas["Área"] = schemas.pop("Tag")
        self.cli("destination", "refresh")
        help_text = self.cli("update", "--help")
        self.assertIn("--area", help_text)
        self.assertNotIn("--tag", help_text)

        schemas["Tag"] = schemas.pop("Área")
        self.cli("destination", "refresh")
        task_id = self.add_task(title="Preparar a reunião")
        self.cli("update", task_id, "--tag", "Revisão")
        self.assert_property_changes(task_id=task_id, expected={TAG_ID})

        schemas["Tag"] = {**schemas["Tag"], "id": "replacement-tags"}
        self.cli("destination", "refresh")
        task_id = self.add_task(title="Enviar a ata")
        self.cli("update", task_id, "--tag", "Financeiro", expect_success=False)
        self.cli("update", task_id, "--tag-2", "Financeiro")
        self.assert_property_changes(task_id=task_id, expected={"replacement-tags"})

    def test_new_tags_require_confirmation_and_existing_tags_are_preserved(self) -> None:
        self.include_tag_column()
        self.register_work()
        task_id = self.add_task(title="Escolher fornecedor")
        self.cli("update", task_id, "--title", "Escolher fornecedor", "--tag", "Revisão")
        self.assert_property_changes(task_id=task_id, expected={TAG_ID})
        self.cli("update", task_id, "--tag", "Financeiro")

        self.cli("update", task_id, "--tag", "Próximo", expect_success=False)
        self.interactive_cli(
            "update",
            task_id,
            "--title",
            "Nova tarefa",
            "--tag",
            "Próximo",
            steps=[("Criar a opção 'Próximo' na coluna Tag?", b"n\r")],
        )
        self.assertIn("Escolher fornecedor", self.cli("show", task_id))
        tags = self.server.pages[task_id]["properties"]["Tag"]
        self.assertEqual([tag["name"] for tag in tags["multi_select"]], ["Revisão", "Financeiro"])

        self.interactive_cli(
            "update",
            task_id,
            "--tag",
            "Próximo",
            steps=[("Criar a opção 'Próximo' na coluna Tag?", b"s\r")],
        )
        self.assert_property_changes(task_id=task_id, expected={TAG_ID})
        tags = self.server.pages[task_id]["properties"]["Tag"]
        self.assertEqual(
            [tag["name"] for tag in tags["multi_select"]],
            ["Revisão", "Financeiro", "Próximo"],
        )

    def test_can_remap_a_destination_after_a_status_change(self) -> None:
        self.register_work()
        states = self.server.sources[SOURCE_ID]["properties"]["Status"]["status"]["options"]
        states[0]["name"] = "A fazer"

        self.assertIn("--replace", self.cli("destination", "refresh", expect_success=False))
        self.cli(
            "destination",
            "add",
            "work",
            "--replace",
            "--pending-state",
            "A fazer",
            "--completed-state",
            "Done",
        )
        task_id = self.add_task(title="Planejar a reunião")

        self.assertIn(f"{task_id}\tA fazer\tPlanejar a reunião", self.cli("list"))

    def test_mapped_start_and_completion_keep_the_first_recorded_times(self) -> None:
        schemas = self.server.sources[SOURCE_ID]["properties"]
        schemas["Início"] = {"id": START_ID, "type": "date", "date": {}}
        schemas["Fim"] = {"id": FINISH_ID, "type": "date", "date": {}}
        self.register_work("--start-column", "Início", "--finish-column", "Fim")
        task_id = self.add_task(title="Inspecionar o orçamento")
        properties = self.server.pages[task_id]["properties"]

        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.cli("update", task_id, "--start-now")
        start = properties["Início"]["date"]["start"]
        self.assert_property_changes(task_id=task_id, expected={START_ID})
        self.assertIsNone(properties["Fim"]["date"])
        self.cli("done", task_id)
        finish = properties["Fim"]["date"]["start"]
        after = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assert_property_changes(task_id=task_id, expected={STATUS_ID, FINISH_ID})
        self.assertLessEqual(before, datetime.fromisoformat(start))
        self.assertLessEqual(datetime.fromisoformat(start), datetime.fromisoformat(finish))
        self.assertLessEqual(datetime.fromisoformat(finish), after)

        patch_count = len(self.server.patches)
        self.cli("update", task_id, "--start-now")
        self.cli("done", task_id)
        self.assertEqual(len(self.server.patches), patch_count)
        self.assertEqual(properties["Início"]["date"]["start"], start)
        self.assertEqual(properties["Fim"]["date"]["start"], finish)
