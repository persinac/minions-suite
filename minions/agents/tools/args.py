"""Coercion for tool arguments that arrive from a model rather than from code.

A JSON Schema `{"type": "integer"}` is a request, not a guarantee. Models emit
`"507"` about as readily as `507`, and nothing between the model and the tool
enforces the declared type -- so every arithmetic use of a tool argument is a
`TypeError` waiting for the right prompt.

Observed on job 68576a15 (playfield-relay), whose Trello card cited
`ActiveState.cpp:507`, in both copies of `read_file`:

    start = max(1, start) - 1
    TypeError: '>' not supported between instances of 'str' and 'int'

The engineer lost the read and retried blind; the reviewers then hit the same
line twice more. It is quiet damage: the executor catches the exception and
hands the model an error string, so the agent keeps going with less of the file
than it asked for and no indication that the range was the problem.
"""


def coerce_line_number(value: object, field: str) -> int | None:
    """Return `value` as a line number, accepting the string form models emit.

    Returns None for a genuinely absent argument. Raises ValueError with a
    message meant for the model -- a caller should return that as the tool
    result so the agent can correct itself, rather than letting it escape as an
    exception the agent only sees as "tool failed".
    """
    if value is None:
        return None

    # bool is a subclass of int, so this must precede the int check or True
    # silently becomes line 1.
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a line number, got a boolean")

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{field} must be a whole line number, got {value!r}")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{field} must be a line number, got {value!r}") from None

    raise ValueError(f"{field} must be a line number, got {type(value).__name__}")
