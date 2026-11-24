"""JSON Schema -> CLI argument parsing and --help rendering."""

from __future__ import annotations

import json
from typing import Any


class CliError(Exception):
    """One-line, user-facing error (no traceback)."""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def convert_value(raw: str, schema: dict, flag: str) -> Any:
    """Convert a string CLI value according to the property's JSON Schema type."""
    t = schema.get("type")
    if t == "boolean":
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise CliError(f"--{flag}: expected a boolean (true/false), got {raw!r}")
    if t == "integer":
        try:
            return int(raw, 10)
        except ValueError:
            raise CliError(f"--{flag}: expected an integer, got {raw!r}")
    if t == "number":
        try:
            return float(raw)
        except ValueError:
            raise CliError(f"--{flag}: expected a number, got {raw!r}")
    if t in ("array", "object"):
        try:
            val = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CliError(f"--{flag}: invalid JSON for type '{t}': {e}")
        if t == "array" and not isinstance(val, list):
            raise CliError(f"--{flag}: expected a JSON array")
        if t == "object" and not isinstance(val, dict):
            raise CliError(f"--{flag}: expected a JSON object")
        return val
    if t == "null":
        return None
    return raw  # "string" or untyped


def _check_enum(value: Any, schema: dict, flag: str) -> Any:
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        raise CliError(f"--{flag}: {value!r} not allowed; expected one of: {allowed}")
    return value


def parse_tool_args(
    argv: list[str],
    schema: dict,
    tool_name: str = "tool",
    stdin_reader=None,
) -> dict:
    """Parse trailing CLI args into a tool-arguments dict.

    Supports: --key value (typed by schema), --json '<obj>', --stdin (JSON on stdin),
    --key=value syntax. Flat flags override --json/--stdin values.
    """
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    args: dict = {}
    flat: dict = {}

    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("--"):
            raise CliError(
                f"unexpected argument {tok!r}; use --key value, --json '<obj>' or --stdin"
            )
        key = tok[2:]
        if "=" in key:
            key, inline = key.split("=", 1)
        else:
            inline = None

        if key == "json":
            if inline is None:
                i += 1
                if i >= len(argv):
                    raise CliError("--json requires a JSON object argument")
                inline = argv[i]
            try:
                obj = json.loads(inline)
            except json.JSONDecodeError as e:
                raise CliError(f"--json: invalid JSON: {e}")
            if not isinstance(obj, dict):
                raise CliError("--json expects a JSON object, e.g. --json '{\"a\": 1}'")
            args.update(obj)
        elif key == "stdin":
            if stdin_reader is None:
                raise CliError("--stdin is not supported in this context")
            try:
                obj = json.loads(stdin_reader())
            except json.JSONDecodeError as e:
                raise CliError(f"--stdin: invalid JSON on stdin: {e}")
            if not isinstance(obj, dict):
                raise CliError("--stdin expects a JSON object")
            args.update(obj)
        else:
            prop = _lookup_prop(props, key)
            if prop is None:
                known = ", ".join("--" + k for k in props) or "(none)"
                raise CliError(f"unknown option --{key}; available: {known}")
            pname, pschema = prop
            if pschema.get("type") == "boolean" and inline is None and (
                i + 1 >= len(argv) or argv[i + 1].startswith("--")
            ):
                flat[pname] = True  # bare --flag means true
            else:
                if inline is None:
                    i += 1
                    if i >= len(argv):
                        raise CliError(f"--{key} requires a value")
                    inline = argv[i]
                flat[pname] = _check_enum(
                    convert_value(inline, pschema, key), pschema, key)
        i += 1

    args.update(flat)
    for req in required:
        if req not in args:
            hint = f"--{req.replace('_', '-')}" if req in props else req
            raise CliError(
                f"missing required argument '{req}'; pass {hint} <value> "
                f"(see 'mcli <server> {tool_name} --help')"
            )
    from jsonschema import exceptions, validators

    validator_class = validators.validator_for(schema)
    try:
        validator_class.check_schema(schema)
        validator_class(schema).validate(args)
    except exceptions.SchemaError as exc:
        raise CliError(f"invalid inputSchema for {tool_name}: {exc.message}") from exc
    except exceptions.ValidationError as exc:
        location = ".".join(map(str, exc.absolute_path)) or "arguments"
        raise CliError(f"{location}: {exc.message}; see {tool_name} --help") from exc
    return args


def _lookup_prop(props: dict, flag: str):
    """Match --foo-bar to property 'foo-bar' or 'foo_bar'."""
    if flag in props:
        return flag, props[flag]
    alt = flag.replace("-", "_")
    if alt in props:
        return alt, props[alt]
    return None


def _type_str(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        t = "|".join(t)
    return t or "any"


def render_tool_help(server: str, tool: dict) -> str:
    name = tool.get("name", "?")
    desc = tool.get("description") or ""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    lines = [f"usage: mcli {server} {name} [OPTIONS]", ""]
    if desc:
        lines += [desc, ""]
    lines.append("options:")
    lines.append("  --json '<obj>'   pass all arguments as one JSON object")
    lines.append("  --stdin          read arguments JSON from stdin")
    for pname, p in props.items():
        flag = "--" + pname.replace("_", "-")
        mark = "*" if pname in required else " "
        bits = [_type_str(p)]
        if "enum" in p:
            bits.append("one of " + "|".join(json.dumps(v) for v in p["enum"]))
        if "default" in p:
            bits.append(f"default: {json.dumps(p['default'])}")
        meta = ", ".join(bits)
        pdesc = p.get("description") or ""
        lines.append(f"  {flag:<20} {mark}({meta})" + (f"  {pdesc}" if pdesc else ""))
    if required:
        lines.append("")
        lines.append("(* = required)")
    return "\n".join(lines)


def render_server_help(server: str, tools: list[dict]) -> str:
    lines = [f"usage: mcli {server} <tool> [OPTIONS]", ""]
    lines.append("tools:")
    width = max((len(t.get("name", "?")) for t in tools), default=0)
    for t in tools:
        name = t.get("name", "?")
        desc = (t.get("description") or "").split("\n")[0]
        lines.append(f"  {name:<{width}}  {desc}".rstrip())
    lines.append("")
    lines.append(f"run 'mcli {server} <tool> --help' for tool-specific options")
    return "\n".join(lines)
