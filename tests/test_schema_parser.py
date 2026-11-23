import pytest

from mcli.schema_parser import (
    CliError,
    convert_value,
    parse_tool_args,
    render_tool_help,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "a": {"type": "integer"},
        "scale": {"type": "number", "default": 1.0},
        "draft": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["png", "svg"]},
        "tags": {"type": "array"},
        "opts": {"type": "object"},
        "file_path": {"type": "string"},
    },
    "required": ["text", "a"],
}


def test_convert_types():
    assert convert_value("42", {"type": "integer"}, "a") == 42
    assert convert_value("2.5", {"type": "number"}, "s") == 2.5
    assert convert_value("true", {"type": "boolean"}, "d") is True
    assert convert_value("off", {"type": "boolean"}, "d") is False
    assert convert_value("hi", {"type": "string"}, "t") == "hi"
    assert convert_value("[1,2]", {"type": "array"}, "t") == [1, 2]
    assert convert_value('{"x":1}', {"type": "object"}, "t") == {"x": 1}


@pytest.mark.parametrize("raw,typ", [("abc", "integer"), ("x", "number"),
                                     ("maybe", "boolean"), ("{bad", "array")])
def test_convert_errors(raw, typ):
    with pytest.raises(CliError):
        convert_value(raw, {"type": typ}, "f")


def test_parse_flat_args():
    args = parse_tool_args(["--text", "hi", "--a", "3", "--scale", "2.5"], SCHEMA)
    assert args == {"text": "hi", "a": 3, "scale": 2.5}


def test_parse_flag_eq_and_hyphen_underscore():
    args = parse_tool_args(["--text=hi", "--a=3", "--file-path", "/x"], SCHEMA)
    assert args == {"text": "hi", "a": 3, "file_path": "/x"}


def test_parse_bare_boolean():
    args = parse_tool_args(["--text", "hi", "--a", "3", "--draft"], SCHEMA)
    assert args["draft"] is True


def test_json_and_stdin_merge():
    args = parse_tool_args(
        ["--json", '{"text": "x"}', "--a", "1"],
        SCHEMA, stdin_reader=lambda: '{"ignored": true}')
    assert args == {"text": "x", "a": 1}
    args = parse_tool_args(
        ["--stdin", "--a", "5"], SCHEMA,
        stdin_reader=lambda: '{"text": "from-stdin", "a": 1}')
    assert args == {"text": "from-stdin", "a": 5}  # flat flag wins


def test_missing_required():
    with pytest.raises(CliError, match="missing required.*'a'"):
        parse_tool_args(["--text", "hi"], SCHEMA)


def test_enum_rejected():
    with pytest.raises(CliError, match="not allowed"):
        parse_tool_args(["--text", "hi", "--a", "1", "--mode", "bmp"], SCHEMA)


def test_unknown_flag():
    with pytest.raises(CliError, match="unknown option"):
        parse_tool_args(["--nope", "1"], SCHEMA)


def test_render_help_marks_required():
    out = render_tool_help("mock", {
        "name": "render", "description": "Render.", "inputSchema": SCHEMA})
    assert "--text" in out and "--a" in out
    assert "one of" in out and "png" in out
    assert "default: 1.0" in out
    assert "(* = required)" in out
