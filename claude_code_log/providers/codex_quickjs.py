"""Sandboxed execution of Codex ``exec`` tool wrappers (issue: dev/codex-quickjs).

Transcript JavaScript is untrusted input. Rather than statically simulating a
whitelisted subset of the syntax tree (the previous ``codex_javascript``
approach), this module *executes* the snippet in a sandboxed QuickJS engine
(``quickjs-ng``) with instrumented ``tools`` / ``text()`` stand-ins that record
what the snippet actually did. Recorded arguments are the values *after* the
snippet's own JS evaluated them, so the whole fragile-expansion problem
(templates, ``join``, string ``concat``) simply disappears.

Public API is unchanged from ``codex_javascript``:
``analyze_javascript_tools(source) -> Optional[JavaScriptToolBatch]``.

Safety model: NO host callables are registered in the engine (attack surface =
the QuickJS interpreter only). Per-snippet memory/time/stack limits bound
hostile inputs (infinite loops, allocation bombs, deep recursion). Any failure
— syntax error, engine exception, cap hit, a shape the mapper can't
correlate — fails closed to ``None`` so the raw-script ``ToolExecution``
fallback stays visible, exactly like the tree-sitter version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional, cast

import quickjs


# Source cap mirrors the tree-sitter version; loop/node caps are replaced by
# the engine's memory/time/stack limits. ``max_loop_iterations`` is retained in
# the signature for API compatibility but is now enforced by the engine.
MAX_SOURCE_BYTES = 64 * 1024
MAX_LOOP_ITERATIONS = 64
MAX_EXPANDED_CALLS = 128

# Engine sandbox limits (per snippet).
_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
_TIME_LIMIT_SECONDS = 2
_MAX_PENDING_JOBS = 100_000

# Provenance delimiter. A private-use codepoint (U+E000): it cannot plausibly
# appear in legit JSONL-embedded source, AND — unlike a control char such as
# NUL — ``JSON.stringify`` passes it through verbatim, so ``text(JSON.stringify
# (r))`` emissions keep their sentinels intact rather than escaping them to a
# literal ``\uXXXX`` sequence.
_S = ""

# Tools whose single non-object argument names a conventional parameter.
_POSITIONAL_PARAM = {"apply_patch": "patch"}


@dataclass(frozen=True)
class JavaScriptToolCall:
    """One materialized ``tools.<name>`` invocation."""

    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class JavaScriptToolBatch:
    """Ordered calls and the output-row index belonging to each call."""

    calls: list[JavaScriptToolCall]
    result_indexes: list[int]
    output_mode: Literal["markers", "ordered"] = "ordered"
    session_markers: bool = False
    result_prefixes: tuple[Optional[str], ...] = ()
    synthetic_results: tuple[Optional[str], ...] = ()
    output_count: int = 0
    result_object_keys: tuple[Optional[str], ...] = ()


# --------------------------------------------------------------------------
# Instrumentation prelude (validated 2026-07-22) — pure JS, no host callables.
# The literal  delimiter is spliced in from Python so there's one source
# of truth for the sentinel char.
# --------------------------------------------------------------------------
PRELUDE = r"""
globalThis.__records = [];
globalThis.__texts = [];
globalThis.__errors = [];
const __D = "";

function __makeMagic(sentinel) {
  // Arrow fn target: still callable (apply trap fires) but has no
  // `prototype` own key, so the ownKeys trap need not report target keys.
  const fn = () => {};
  const wrap = () => __D + sentinel + __D;
  const magic = new Proxy(fn, {
    get(t, prop) {
      if (prop === "__sentinel") return sentinel;
      if (prop === Symbol.toPrimitive) return wrap;
      if (prop === "toString") return wrap;
      if (prop === "valueOf") return wrap;
      if (prop === "toJSON") return wrap;
      if (prop === Symbol.iterator) return function* () {};
      if (prop === "then") return undefined;
      if (prop === "length") return 0;
      if (typeof prop === "symbol") return undefined;
      return __makeMagic(sentinel + "." + String(prop));
    },
    apply(t, self, args) { return __makeMagic(sentinel + "()"); },
    has() { return true; },
    // Object spread ({name, ...r}) enumerates own keys. A magic result has no
    // statically-knowable shape, so we expose a single canonical "output" key —
    // the field the shared Bash renderer consumes. This degrades a spread to a
    // whole-result projection rather than reproducing unknowable JS key sets.
    ownKeys() { return ["output"]; },
    getOwnPropertyDescriptor(t, prop) {
      if (prop === "output")
        return { enumerable: true, configurable: true,
                 value: __makeMagic(sentinel + ".output") };
      return undefined;
    },
  });
  return magic;
}

function __safe(value, depth, seen) {
  depth = depth || 0;
  if (depth > 6) return "<deep>";
  const t = typeof value;
  if (value === null || t === "number" || t === "boolean" || t === "string")
    return value;
  if (t === "undefined") return "<undefined>";
  if (t === "function")
    return value.__sentinel ? __D + value.__sentinel + __D : "<function>";
  seen = seen || [];
  if (seen.indexOf(value) !== -1) return "<cycle>";
  seen = seen.concat([value]);
  if (Array.isArray(value)) return value.slice(0, 64).map((v) => __safe(v, depth + 1, seen));
  if (t === "object") {
    const out = {};
    let n = 0;
    for (const k of Object.keys(value)) {
      if (++n > 64) { out["..."] = "<truncated>"; break; }
      out[k] = __safe(value[k], depth + 1, seen);
    }
    return out;
  }
  return String(value);
}

globalThis.tools = new Proxy({}, {
  get(t, name) {
    if (typeof name !== "string") return undefined;
    return (...args) => {
      const idx = __records.length;
      __records.push({ name: name, args: __safe(args) });
      return Promise.resolve(__makeMagic("R" + idx));
    };
  },
});

globalThis.text = (v) => { __texts.push(__safe(v)); };
globalThis.ALL_TOOLS = __makeMagic("ALL_TOOLS");
globalThis.setTimeout = (fn, ms) => {
  __records.push({ name: "__wait", args: [{ delay_ms: ms }] });
  if (typeof fn === "function") fn();
  return 0;
};
globalThis.clearTimeout = () => {};
globalThis.exit = () => { throw { __exit: true }; };
globalThis.console = {
  log: (...a) => __texts.push(__safe(a.length === 1 ? a[0] : a)),
  error: (...a) => __texts.push(__safe(a.length === 1 ? a[0] : a)),
};
"""


def _wrap(code: str) -> str:
    """Wrap the snippet in an async IIFE that records unhandled errors."""
    return (
        "globalThis.__done = false;\n"
        "(async () => {\n" + code + "\n})().then(\n"
        "  () => { __done = true; },\n"
        "  (e) => { if (!(e && e.__exit)) "
        "__errors.push(String((e && e.message) || e)); __done = true; });\n"
    )


def _run_snippet(code: str) -> Optional[dict[str, Any]]:
    """Execute *code* under the instrumented prelude; return the recorded report.

    Returns ``{records, texts, errors, done}`` or ``None`` on any engine-level
    failure (syntax error, limit hit, unexpected engine exception).
    """
    ctx = quickjs.Context()
    try:
        ctx.set_memory_limit(_MEMORY_LIMIT_BYTES)
        ctx.set_time_limit(_TIME_LIMIT_SECONDS)
    except Exception:
        # Binding without a limit setter: proceed, the pending-job bound below
        # still prevents an unbounded microtask storm.
        pass
    try:
        ctx.eval(PRELUDE)
        ctx.eval(_wrap(code))
        pumped = 0
        while ctx.execute_pending_job():
            pumped += 1
            if pumped > _MAX_PENDING_JOBS:
                return None
        report = ctx.eval(
            "JSON.stringify({records:__records,texts:__texts,"
            "errors:__errors,done:__done})"
        )
        return json.loads(report)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Sentinel parsing: a recorded text() value embeds "<D>R<i>[.path]<D>"
# provenance markers revealing which call result (and property path) it emits,
# plus the literal prefixes around them.
# --------------------------------------------------------------------------
_SENTINEL_RE = re.compile(_S + "([^" + _S + "]*)" + _S)
_RESULT_MARKER_RE = re.compile(r"^RESULT_(\d+)$")
_SESSION_PREFIX = "SESSION_ID="


@dataclass(frozen=True)
class _Ref:
    """A parsed ``R<i>[.path]`` sentinel: which call, which property path."""

    call_index: int
    path: tuple[str, ...]
    object_key: Optional[str] = None


def _parse_ref(token: str) -> Optional[_Ref]:
    """Parse a sentinel token ``R<i>.a.b`` into a call ref, or None (non-call)."""
    m = re.match(r"^R(\d+)(.*)$", token)
    if m is None:
        return None
    rest = m.group(2)
    path = tuple(seg for seg in rest.split(".") if seg and seg != "()")
    return _Ref(call_index=int(m.group(1)), path=path)


@dataclass
class _Emission:
    """One text() emission decomposed into literal prefix + call references."""

    raw: str
    prefix: str  # literal text before the first sentinel
    refs: list[_Ref]  # call refs embedded, in order (object_key set for JSON obj)
    literal_only: bool  # no sentinel at all


def _refs_from_string(
    value: str, object_key: Optional[str] = None
) -> Optional[list[_Ref]]:
    out: list[_Ref] = []
    for m in _SENTINEL_RE.finditer(value):
        ref = _parse_ref(m.group(1))
        if ref is None:
            return None  # ALL_TOOLS / non-call sentinel — not correlatable.
        out.append(
            _Ref(ref.call_index, ref.path, object_key if object_key else ref.object_key)
        )
    return out


def _project_result_object(obj: dict[str, Any]) -> Optional[_Emission]:
    """Project a metadata object bundling tool results into an emission.

    Two recognized shapes, distinguished by whether a result-referencing field
    holds a *whole* result (path ``()``) or a *field* of one (path ``(key,)``):

    - **Bundle** ``{presence: p, mail: m}`` — each field is a whole result under
      its own key; several calls collapse into one output row, keyed per field.
    - **Projection** ``{name, ...r}`` / ``{name, exit_code: r.exit_code,
      output: r.output}`` — one call's fields projected (the spread degrades to
      a single ``output`` key via the prelude). Mirrors the tree-sitter
      ``_mapped_result_object``: one ``output``-keyed ref, ``output`` required,
      leading static fields become a JSON prefix.

    Mixed or malformed shapes fail closed to ``None``.
    """
    whole: list[tuple[str, _Ref]] = []  # field -> whole-result ref (path ())
    projected: list[tuple[str, _Ref]] = []  # field -> field-of-result ref
    prefix: Optional[str] = None
    seen_keys = 0
    for raw_key, val in obj.items():
        key = str(raw_key)
        if isinstance(val, str) and _S in val:
            got = _refs_from_string(val)
            if got is None or len(got) != 1:
                return None  # composed / multi-ref field — not a clean shape.
            ref = got[0]
            if ref.path == ():
                whole.append((key, ref))
            elif ref.path == (key,):
                projected.append((key, ref))
            else:
                return None  # e.g. ``output: r.exit_code`` — mismatched field.
        else:
            if _contains_sentinel(val):
                return None  # nested/composed sentinel — not a flat projection.
            if seen_keys == 0:
                try:
                    prefix = json.dumps(
                        {key: val}, ensure_ascii=False, separators=(",", ":")
                    )[:-1]
                except (TypeError, ValueError):
                    return None
        seen_keys += 1

    if whole and not projected:
        # Bundle: one whole-result ref per field, all sharing one output row.
        return _Emission(
            raw="",
            prefix="",
            refs=[_Ref(ref.call_index, (), object_key=key) for key, ref in whole],
            literal_only=False,
        )
    if projected and not whole:
        # Projection: collapse to one call's ``output`` field + static prefix.
        call_index = projected[0][1].call_index
        if any(ref.call_index != call_index for _, ref in projected):
            return None
        if not any(key == "output" for key, _ in projected):
            return None
        return _Emission(
            raw="",
            prefix=prefix or "",
            refs=[_Ref(call_index, ("output",), object_key="output")],
            literal_only=False,
        )
    return None


def _decompose(text_value: Any) -> Optional[_Emission]:
    """Decompose one recorded text() value into its emission shape."""
    if isinstance(text_value, dict):
        # text(obj) pushed a whole object (e.g. out.forEach(text)) — project it.
        return _project_result_object(cast("dict[str, Any]", text_value))
    if not isinstance(text_value, str):
        # console.log(array) etc. — opaque, non-correlatable literal.
        return _Emission(raw="", prefix="", refs=[], literal_only=True)
    if _S not in text_value:
        return _Emission(raw=text_value, prefix=text_value, refs=[], literal_only=True)

    # JSON.stringify(...) emissions decode as valid JSON whose leaf strings hold
    # the sentinels: a bare string (clean full-result emission) or an object
    # (result projection). Non-JSON forms are literal prefix + refs.
    try:
        decoded = json.loads(text_value)
    except Exception:
        decoded = None

    if isinstance(decoded, str):
        refs = _refs_from_string(decoded)
        if refs is None:
            return None
        return _Emission(raw=text_value, prefix="", refs=refs, literal_only=False)

    if isinstance(decoded, dict):
        return _project_result_object(cast("dict[str, Any]", decoded))

    # Literal form: prefix<sentinel>[...]. Reject if any sentinel is non-call.
    refs = _refs_from_string(text_value)
    if refs is None:
        return None
    first = _SENTINEL_RE.search(text_value)
    prefix = text_value[: first.start()] if first else ""
    return _Emission(raw=text_value, prefix=prefix, refs=refs, literal_only=False)


# --------------------------------------------------------------------------
# Report → JavaScriptToolBatch
# --------------------------------------------------------------------------
@dataclass
class _CallInfo:
    name: str
    input: dict[str, Any]
    is_wait: bool = False
    delay_ms: Optional[int] = None


def _contains_sentinel(value: Any) -> bool:
    if isinstance(value, str):
        return _S in value
    if isinstance(value, list):
        return any(_contains_sentinel(v) for v in cast("list[Any]", value))
    if isinstance(value, dict):
        return any(
            _contains_sentinel(v) for v in cast("dict[str, Any]", value).values()
        )
    return False


def _coerce_call(record: dict[str, Any]) -> Optional[_CallInfo]:
    name = record.get("name")
    args = record.get("args")
    if not isinstance(name, str) or not isinstance(args, list):
        return None
    args = cast("list[Any]", args)
    if name == "__wait":
        first = args[0] if args else None
        delay = (
            cast("dict[str, Any]", first).get("delay_ms")
            if isinstance(first, dict)
            else None
        )
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            return None
        return _CallInfo(
            "wait", {"delay_ms": int(delay)}, is_wait=True, delay_ms=int(delay)
        )
    arg0: Any = args[0] if args else {}
    if isinstance(arg0, dict):
        arg0 = cast("dict[str, Any]", arg0)
        if _contains_sentinel(arg0):
            # Argument still embeds an unresolved call result (inter-call data
            # dependency) — fail closed rather than emit a half-value.
            return None
        return _CallInfo(name, arg0)
    if len(args) == 1 and isinstance(arg0, str) and not _contains_sentinel(arg0):
        param = _POSITIONAL_PARAM.get(name)
        if param is not None:
            return _CallInfo(name, {param: arg0})
        return None  # unknown single-positional convention — fail closed.
    return None


def _build_batch(report: dict[str, Any]) -> Optional[JavaScriptToolBatch]:
    """Map a recorded execution report to a JavaScriptToolBatch, or None."""
    if not report.get("done") or report.get("errors"):
        return None
    raw_records = report.get("records")
    raw_texts = report.get("texts")
    if not isinstance(raw_records, list) or not isinstance(raw_texts, list):
        return None
    raw_records = cast("list[Any]", raw_records)
    raw_texts = cast("list[Any]", raw_texts)
    if not raw_records or len(raw_records) > MAX_EXPANDED_CALLS:
        return None

    calls: list[_CallInfo] = []
    for rec in raw_records:
        if not isinstance(rec, dict):
            return None
        info = _coerce_call(cast("dict[str, Any]", rec))
        if info is None:
            return None
        calls.append(info)

    emissions: list[_Emission] = []
    for tv in raw_texts:
        emi = _decompose(tv)
        if emi is None:
            return None
        emissions.append(emi)

    return _correlate(calls, emissions)


def _correlate(
    calls: list[_CallInfo], emissions: list[_Emission]
) -> Optional[JavaScriptToolBatch]:
    """Assign each non-wait call the output row of the emission that reads it."""
    n = len(calls)
    result_indexes = [-1] * n
    result_prefixes: list[Optional[str]] = [None] * n
    synthetic_results: list[Optional[str]] = [None] * n
    result_object_keys: list[Optional[str]] = [None] * n

    for i, c in enumerate(calls):
        if c.is_wait:
            synthetic_results[i] = f"Waited {c.delay_ms} ms"

    output_mode: Literal["markers", "ordered"] = "ordered"
    session_markers = False
    output_row = 0
    seen_rows: set[int] = set()

    for emi in emissions:
        if emi.literal_only:
            if emi.raw and _RESULT_MARKER_RE.match(emi.raw.strip()):
                output_mode = "markers"
                continue
            if emi.raw.strip():
                # A non-marker literal with no call ref: a synthetic string the
                # tree-sitter version wouldn't correlate → fail closed.
                return None
            continue

        # Session marker: "SESSION_ID=<sentinel .session_id>".
        if (
            emi.prefix == _SESSION_PREFIX
            and len(emi.refs) == 1
            and emi.refs[0].path == ("session_id",)
        ):
            session_markers = True
            continue

        row = output_row
        introduced_new = False
        for ref in emi.refs:
            i = ref.call_index
            if i < 0 or i >= n or calls[i].is_wait:
                return None
            if result_indexes[i] != -1:
                # Re-emission of an already-assigned call (e.g. a second text()
                # reading a different field of the same result) — keep its first
                # output row; this emission adds no new output group.
                continue
            result_indexes[i] = row
            introduced_new = True
            if emi.prefix and result_prefixes[i] is None:
                result_prefixes[i] = emi.prefix
            if ref.object_key is not None:
                result_object_keys[i] = ref.object_key
            elif ref.path:
                result_object_keys[i] = ref.path[-1]
        if introduced_new:
            seen_rows.add(row)
            output_row += 1

    output_count = len(seen_rows)

    for i, c in enumerate(calls):
        if not c.is_wait and result_indexes[i] == -1:
            return None  # un-emitted call → we'd drop its output; fail closed.

    js_calls = [JavaScriptToolCall(c.name, c.input) for c in calls]
    return JavaScriptToolBatch(
        calls=js_calls,
        result_indexes=result_indexes,
        output_mode=output_mode,
        session_markers=session_markers,
        result_prefixes=tuple(result_prefixes),
        synthetic_results=tuple(synthetic_results),
        output_count=output_count,
        result_object_keys=tuple(result_object_keys),
    )


def analyze_javascript_tools(
    source: str,
    *,
    max_loop_iterations: int = MAX_LOOP_ITERATIONS,
    max_expanded_calls: int = MAX_EXPANDED_CALLS,
) -> Optional[JavaScriptToolBatch]:
    """Execute a bounded snippet and materialize its tool batch, failing closed.

    ``max_loop_iterations`` is accepted for API compatibility; loop bounds are
    now enforced by the engine's time limit. ``max_expanded_calls`` caps the
    number of recorded calls.
    """
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            return None
        report = _run_snippet(source)
        if report is None:
            return None
        batch = _build_batch(report)
        if batch is not None and len(batch.calls) > max_expanded_calls:
            return None
        return batch
    except Exception:
        return None
