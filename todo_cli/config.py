import json
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from .core import Column, Destination, DomainError
from .locking import file_lock


@dataclass
class Config:
    active: str | None = None
    destinations: dict[str, Destination] = field(default_factory=dict)


class ConfigStore:
    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path.expanduser() if path is not None else _default_path()

    def load(self) -> Config:
        try:
            with self.path.open(encoding="utf-8") as file:
                return _parse_config(json.load(file))
        except FileNotFoundError:
            return Config()
        except json.JSONDecodeError as error:
            raise DomainError(
                f"Configuração inválida em {self.path}: JSON malformado na linha {error.lineno}."
            ) from error
        except UnicodeDecodeError as error:
            raise DomainError(f"A configuração em {self.path} deve estar em UTF-8.") from error
        except ValueError as error:
            raise DomainError(f"Configuração inválida em {self.path}: {error}.") from error
        except OSError as error:
            raise DomainError(
                f"Falha ao ler a configuração em {self.path}: {error.strerror}."
            ) from error

    @contextmanager
    def edit(self) -> Iterator[Config]:
        # Atomic replacement changes the config file's inode, so lock a stable sibling.
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with file_lock(path=lock_path):
            config = self.load()
            yield config
            self._save(config)

    def _save(self, config: Config) -> None:
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as file:
                temporary_path = Path(file.name)
                json.dump(asdict(config), file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(self.path)
            temporary_path = None
        except OSError as error:
            raise DomainError(
                f"Falha ao salvar a configuração em {self.path}: {error.strerror}."
            ) from error
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)


def _default_path() -> Path:
    if override := os.environ.get("WIN_TD_CONFIG"):
        return Path(override).expanduser()
    if config_home := os.environ.get("XDG_CONFIG_HOME"):
        return Path(config_home).expanduser() / "win-td" / "config.json"
    return Path.home() / ".config" / "win-td" / "config.json"


def _parse_config(data: object) -> Config:
    if not isinstance(data, dict):
        raise ValueError("esperado um objeto com os destinos cadastrados")
    active = data.get("active")
    if active is not None and not isinstance(active, str):
        raise ValueError("o destino ativo deve ser um nome ou nulo")
    destinations = data.get("destinations", {})
    if not isinstance(destinations, dict):
        raise ValueError("esperado um objeto de destinos por nome")
    return Config(
        active=active,
        destinations={name: _parse_destination(value) for name, value in destinations.items()},
    )


def _parse_destination(data: object) -> Destination:
    match data:
        case {
            "source_id": str() as source_id,
            "status_id": str() as status_id,
            "pending": str() as pending,
            "completed": str() as completed,
        }:
            start_id = data.get("start_id")
            finish_id = data.get("finish_id")
            if any(
                value is not None and not isinstance(value, str) for value in (start_id, finish_id)
            ):
                raise ValueError("as colunas de horários devem usar IDs de texto ou nulo")
            columns = data.get("columns", [])
            if not isinstance(columns, list):
                raise ValueError("esperada uma lista de colunas no destino")
            field_aliases = data.get("field_aliases")
            return Destination(
                source_id=source_id,
                status_id=status_id,
                pending=pending,
                completed=completed,
                start_id=start_id,
                finish_id=finish_id,
                columns=tuple(map(_parse_column, columns)),
                field_aliases=_parse_aliases(field_aliases) if field_aliases is not None else None,
                retired_field_aliases=_parse_aliases(data.get("retired_field_aliases", {})),
            )
        case _:
            raise ValueError("cada destino deve informar a fonte, a coluna de estado e os estados")


def _parse_aliases(data: object) -> dict[str, str]:
    if not isinstance(data, dict) or not all(
        isinstance(alias, str) and alias.startswith("--") and isinstance(column_id, str)
        for alias, column_id in data.items()
    ):
        raise ValueError("esperado um mapeamento de opções para as colunas")
    return data


def _parse_column(data: object) -> Column:
    match data:
        case {"id": str() as column_id, "name": str() as name, "kind": str() as kind}:
            choices = data.get("choices", [])
            if not isinstance(choices, list) or not all(
                isinstance(choice, str) for choice in choices
            ):
                raise ValueError("as opções da coluna devem ser uma lista de textos")
            return Column(id=column_id, name=name, kind=kind, choices=tuple(choices))
        case _:
            raise ValueError("cada coluna deve informar o ID, o nome e o tipo")
