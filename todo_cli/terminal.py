import codecs
import io
import os
import select
import sys
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

if os.name == "posix":
    import termios
    import tty


def clean_text(text: str) -> str:
    return "".join(character if character.isprintable() else " " for character in text)


class Terminal:
    @property
    def interactive(self) -> bool:
        return os.name == "posix" and sys.stdin.isatty() and sys.stderr.isatty()

    def choose(
        self, label: str, options: Sequence[str], *, search_texts: Sequence[str] | None = None
    ) -> int | None:
        if not self.interactive or not options:
            return None

        search = ""
        search_keys = tuple(
            text.casefold() for text in (options if search_texts is None else search_texts)
        )
        matches = list(range(len(options)))
        selected = 0
        first_visible = 0
        input_fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")(errors="replace")
        with _selection_screen(input_fd=input_fd):
            while True:
                columns, rows = _screen_size()
                visible_count = max(1, rows - 4)
                first_visible = min(max(first_visible, selected - visible_count + 1), selected)
                last_visible = min(first_visible + visible_count, len(matches))
                lines = [label, f"Buscar: {search}"]
                for position in range(first_visible, last_visible):
                    marker = "> " if position == selected else "  "
                    lines.append(marker + options[matches[position]])
                if not matches:
                    lines.append("Nenhuma opção encontrada.")
                lines.append("↑/↓ navegam · Enter seleciona · Esc cancela")
                width = max(1, columns - 1)
                screen = "\n".join(_display(line, width=width) for line in lines)
                print(f"\x1b[H\x1b[J{screen}", file=sys.stderr, end="", flush=True)

                key = _read_key(input_fd=input_fd, decoder=decoder)
                if key is None:
                    return None
                if key in ("\r", "\n") and matches:
                    return matches[selected]
                if key == "up":
                    selected = max(0, selected - 1)
                elif key == "down" and matches:
                    selected = min(len(matches) - 1, selected + 1)
                elif key in ("\x7f", "\b") or (key and key.isprintable()):
                    search = search[:-1] if key in ("\x7f", "\b") else search + key
                    query = search.casefold()
                    matches = [index for index, text in enumerate(search_keys) if query in text]
                    selected = 0

    def prompt(self, label: str) -> str:
        if not self.interactive:
            return ""
        _print_prompt(label=label)
        return sys.stdin.readline().rstrip("\r\n")

    def prompt_line(self, *, label: str) -> str | None:
        """Return text submitted with Enter, or None on end of input."""
        if not self.interactive:
            return None
        _print_prompt(label=label)
        # A canonical terminal read returns a record without a newline on Ctrl+D.
        # The default stdin buffer can be smaller than a terminal record.
        line = sys.stdin.buffer.read1(io.DEFAULT_BUFFER_SIZE)
        if not line.endswith(b"\n"):
            return None
        return line[:-1].decode(sys.stdin.encoding or "utf-8")

    def confirm(self, label: str) -> bool:
        return self.prompt(f"{label} [s/N]").strip().casefold() in {"s", "sim", "y", "yes"}


def _print_prompt(*, label: str) -> None:
    print(f"{clean_text(label)}: ", file=sys.stderr, end="", flush=True)


@contextmanager
def _selection_screen(*, input_fd: int) -> Iterator[None]:
    attributes = termios.tcgetattr(input_fd)
    try:
        tty.setcbreak(input_fd, termios.TCSANOW)
        print("\x1b[?1049h", file=sys.stderr, end="", flush=True)
        yield
    finally:
        try:
            termios.tcsetattr(input_fd, termios.TCSANOW, attributes)
        finally:
            print("\x1b[?1049l", file=sys.stderr, end="", flush=True)


def _read_key(*, input_fd: int, decoder: codecs.IncrementalDecoder) -> str | None:
    character = os.read(input_fd, 1)
    if character in (b"", b"\x04"):
        return None
    if character != b"\x1b":
        return decoder.decode(character)

    # Arrow keys begin with the same byte as a standalone Escape.
    escape_timeout = 0.08
    if not select.select([input_fd], [], [], escape_timeout)[0]:
        return None
    if os.read(input_fd, 1) not in (b"[", b"O"):
        return None

    while select.select([input_fd], [], [], escape_timeout)[0]:
        character = os.read(input_fd, 1)
        if not character:
            return None
        if 0x40 <= character[0] <= 0x7E:
            return {b"A": "up", b"B": "down"}.get(character, "")
    return ""


def _screen_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stderr.fileno())
    except OSError:
        return 80, 24
    return size.columns or 80, size.lines or 24


def _display(text: str, *, width: int) -> str:
    printable = clean_text(text)
    used = 0
    for position, character in enumerate(printable):
        if not unicodedata.combining(character):
            used += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        if used > width:
            return printable[:position]
    return printable
