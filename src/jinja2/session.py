"""Synchronous, dependency aware render sessions.

A render session is created by :meth:`Template.render_session` and keeps
track of the values flowing through filter, test and function calls while a
template is rendered.  When the context is replaced with a new snapshot by
:meth:`RenderSession.update`, calls whose dependencies are unchanged are not
executed again; their previous results are reused.  The surrounding template
machinery still runs normally, so branch switching, loop resizing, template
inheritance and everything else keeps producing the exact same output as a
plain :meth:`~Template.render` call with the same data.

The session machinery is purely additive: it is implemented with environment,
context, code generator and template subclasses that are only used for the
compiled templates of one session.  Templates rendered outside of sessions are
completely unaffected.

This is an internal implementation module; the public entry points are
:class:`jinja2.Template.render_session` and :class:`RenderSession`.
"""

from __future__ import annotations

import functools
import itertools
import sys
import typing as t
from collections import abc

from markupsafe import Markup

from . import nodes
from .compiler import CodeGenerator
from .compiler import find_undeclared
from .environment import Environment
from .environment import Template
from .nodes import EvalContext
from .runtime import Context
from .runtime import LoopContext
from .runtime import Macro
from .runtime import Undefined
from .runtime import missing
from .utils import Cycler
from .utils import Joiner
from .utils import Namespace
from .utils import _PassArg

if t.TYPE_CHECKING:
    pass


# -----------------------------------------------------------------------------
# Input validation
# -----------------------------------------------------------------------------

_SCALAR_TYPES = (str, bool, int, float, type(None))


def validate_mapping(value: t.Any, path: str = "$") -> dict[str, t.Any]:
    """Validate and normalize the context mapping of a render session."""
    if not isinstance(value, dict):
        raise TypeError(
            "render_session() and update() expect a dict of context data,"
            f" got {type(value).__name__}"
        )

    _validate_value(value, path, set())
    return value


def _validate_value(value: t.Any, path: str, seen: set[int]) -> None:
    if isinstance(value, _SCALAR_TYPES):
        return

    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            raise TypeError(f"circular reference in session data at {path}")
        seen.add(marker)
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "session data mappings may only have string keys,"
                    f" got {type(key).__name__} at {path}"
                )
            _validate_value(item, f"{path}.{key}", seen)
        seen.discard(marker)
        return

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            raise TypeError(f"circular reference in session data at {path}")
        seen.add(marker)
        for index, item in enumerate(value):
            _validate_value(item, f"{path}[{index}]", seen)
        seen.discard(marker)
        return

    raise TypeError(
        "session data may only contain plain dicts, lists, tuples, strings,"
        f" numbers, booleans and None; got {type(value).__name__} at {path}"
    )


# -----------------------------------------------------------------------------
# Structural fingerprints
# -----------------------------------------------------------------------------

# Filters returning (potentially lazy) iterators that the session may eagerly
# materialize in order to memoize the per-element callback invocations.
_PIPELINE_FILTERS = frozenset(
    {"map", "select", "reject", "selectattr", "rejectattr"}
)

# Stateful objects exposed through the default namespace.  Invocations of
# these must always execute so that every render gets a fresh, mutable object.
_STATEFUL_CALLABLES = (Namespace, Cycler, Joiner)

_unique_counter = itertools.count()


def fingerprint(value: t.Any) -> tuple:
    """Return a deterministic, hashable structural signature of *value*.

    Plain data is compared structurally; runtime objects (contexts, macros,
    loop contexts, ...) are handled explicitly.  Anything unrecognized gets a
    unique token so that caching fails conservatively.
    """
    return _fingerprint(value, set())


def _fingerprint(value: t.Any, seen: set[int]) -> tuple:
    # bool must be checked before int.
    if value is None:
        return ("none",)
    if value is True or value is False:
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, Markup):
        return ("markup", str(value))
    if isinstance(value, str):
        return ("str", value)

    if isinstance(value, Context):
        # Context access is tracked through the read log instead.
        return ("context",)
    if isinstance(value, Environment):
        return ("environment",)
    if isinstance(value, EvalContext):
        return ("evalctx", bool(value.autoescape), bool(value.volatile))
    if value is missing:
        return ("missing",)
    if isinstance(value, Undefined):
        obj = value._undefined_obj
        return (
            "undefined",
            id(type(value)),
            value._undefined_name,
            value._undefined_hint,
            ("missing",) if obj is missing else _fingerprint(obj, seen),
        )

    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return ("cycle", next(_unique_counter))
        seen.add(marker)
        try:
            items = tuple(
                sorted(
                    (_fingerprint(k, seen), _fingerprint(v, seen))
                    for k, v in value.items()
                )
            )
        finally:
            seen.discard(marker)
        return ("dict", items)

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            return ("cycle", next(_unique_counter))
        seen.add(marker)
        try:
            items = tuple(_fingerprint(item, seen) for item in value)
        finally:
            seen.discard(marker)
        return ("tuple" if isinstance(value, tuple) else "list", items)

    if isinstance(value, LoopContext):
        return (
            "loop",
            value.index0,
            value.depth0,
            _fingerprint(value._current, seen),
        )

    if isinstance(value, Macro):
        closure: tuple = ()
        if value._func.__closure__:
            closure = tuple(
                _fingerprint(cell.cell_contents, seen)
                for cell in value._func.__closure__
            )
        return ("macro", value.name, id(value._func.__code__), closure)

    if isinstance(value, Namespace):
        return ("namespace", _fingerprint(value._Namespace__attrs, seen))

    if callable(value):
        # Global functions and other module level callables have stable ids
        # for the lifetime of the (fixed) session environment.  Locally
        # defined closures are recreated every render, so they miss on
        # purpose.
        return ("callable", id(value))

    # Anything else: never reuse.
    return ("object", next(_unique_counter))


def _fingerprint_kwargs(kwargs: t.Mapping[str, t.Any]) -> tuple:
    if not kwargs:
        return ()
    return tuple(
        sorted((key, _fingerprint(value, set())) for key, value in kwargs.items())
    )


def _is_template_function(runtime: "SessionRuntime", obj: t.Any) -> bool:
    """Return True if *obj* is a function generated for a session template
    in this runtime (for example a call block's caller)."""
    globals_dict = getattr(obj, "__globals__", None)
    return globals_dict is not None and globals_dict.get("__rs") is runtime


# -----------------------------------------------------------------------------
# Per render bookkeeping
# -----------------------------------------------------------------------------


class _Scope:
    """A memoized invocation that may contain nested memoized invocations."""

    __slots__ = ("path", "counters")

    def __init__(self, path: tuple) -> None:
        self.path = path
        # Number of non-template callback occurrences below this scope, keyed
        # by the calling code object and bytecode offset.
        self.counters: dict[tuple[int, int], int] = {}


class SessionRuntime:
    """Holds the memoization state of one :class:`RenderSession`.

    A single instance lives for the whole session.  Its per-render attributes
    are reset by :meth:`begin_render`/:meth:`end_render`.  Rendering is
    synchronous, so no locking is required.
    """

    def __init__(self) -> None:
        #: Persistent memo table: key -> (dependencies, result)
        self.memo: dict[tuple, tuple[tuple, t.Any]] = {}

        #: Whether a render is currently in progress.
        self.active: bool = False

        #: Current nesting of for loops, as iteration counters per depth.
        self.loop_stack: list[int] = []

        #: Stack of memoized invocations currently executing.
        self.scopes: list[_Scope] = []

        #: Context reads observed during the current render
        #: (kind, key, fingerprint(value)).
        self.reads: set[tuple] = set()

        #: True while the environment dispatches a pipeline call, so wrapped
        #: filter/test functions know they must not memoize a second time.
        self.passthrough: int = 0

        #: Stack of template construct invocation sites (macros, call blocks)
        #: that are currently executing, so calls in their bodies can be
        #: distinguished per invocation.
        self.call_markers: list[tuple] = []

        #: While revalidating cached dependencies against the current
        #: context, reads must not be recorded as new observations.
        self.suppress_reads: bool = False

    # -- loop instrumentation emitted by the session code generator -------

    def enter_loop(self) -> None:
        self.loop_stack.append(0)

    def step_loop(self) -> None:
        self.loop_stack[-1] += 1

    def unstep_loop(self) -> None:
        # Nothing to do per iteration; the marker exists for the emitted
        # try/finally structure.
        return None

    def exit_loop(self) -> None:
        self.loop_stack.pop()

    # -- macro / call block invocation markers ----------------------------

    def enter_call(self, frame: t.Any) -> None:
        self.call_markers.append(self._template_site(frame))

    def exit_call(self) -> None:
        self.call_markers.pop()

    # -- context read tracking --------------------------------------------

    def note_read(self, kind: str, key: t.Any, value: t.Any) -> None:
        if self.active and self.scopes and not self.suppress_reads:
            self.reads.add((kind, key, _fingerprint(value, set())))

    # -- call site identification -----------------------------------------

    def _template_site(self, current: t.Any) -> tuple:
        return (
            "template",
            id(current.f_code),
            current.f_lasti,
            tuple(self.loop_stack),
            tuple(self.call_markers),
        )

    def site_key(self, frame: t.Any, walk_to_template: bool = True) -> tuple:
        """Identify a callback occurrence.

        Template callbacks are identified by the compiled code object, the
        bytecode offset of the call, the enclosing loop iteration path and
        the enclosing macro/caller invocations.  Callbacks invoked from
        non-template code (pipeline internals, user helpers) are identified
        relative to the nearest memoized scope; with *walk_to_template*
        disabled the immediate frame is used as anchor instead of walking up
        to the enclosing template frame.
        """
        current = frame
        if walk_to_template:
            while current is not None:
                if current.f_globals.get("__rs") is self:
                    return self._template_site(current)
                current = current.f_back
        else:
            current = frame

        scope = self.scopes[-1] if self.scopes else None
        anchor = (id(current.f_code), current.f_lasti)
        if scope is None:
            # No memoized parent scope; do not attempt to reuse anything.
            return ("external", next(_unique_counter))
        number = scope.counters.get(anchor, 0) + 1
        scope.counters[anchor] = number
        return ("nested", scope.path, anchor[0], anchor[1], number)

    # -- memoization -------------------------------------------------------

    @staticmethod
    def _read_value_token(kind: str, key: t.Any, context: Context) -> t.Any:
        if kind == "resolve":
            value = Context.resolve_or_missing(context, key)
        elif kind == "contains":
            value = key in context
        else:
            value = Context.get_all(context)
        return _fingerprint(value, set())

    def reads_valid(self, reads: frozenset, context: Context | None) -> bool:
        if not reads:
            return True
        if context is None:
            return False
        self.suppress_reads = True
        try:
            for kind, key, token in reads:
                if self._read_value_token(kind, key, context) != token:
                    return False
            return True
        finally:
            self.suppress_reads = False

    def invoke(
        self,
        frame: t.Any,
        func: t.Callable[..., t.Any],
        args: tuple,
        kwargs: dict[str, t.Any],
        eager_iterator: bool = False,
        context: Context | None = None,
        walk_to_template: bool = True,
    ) -> t.Any:
        key = self.site_key(frame, walk_to_template=walk_to_template)

        if key[0] == "external":
            return func()

        arguments = _fingerprint(args, set())
        keyword_arguments = _fingerprint_kwargs(kwargs)

        entry = self.memo.get(key)
        if entry is not None and entry[0][:2] == (arguments, keyword_arguments):
            if self.reads_valid(entry[0][2], context):
                return entry[1]

        scope = _Scope(key)
        self.scopes.append(scope)
        before = set(self.reads)
        try:
            result = func()
            # Built-in map/select style pipelines return lazy generators.
            # Materialize them while the scope is active so that the
            # per-element callback calls are observed and memoized too.
            if eager_iterator and isinstance(result, abc.Iterator):
                result = tuple(result)
            dependencies = (
                arguments,
                keyword_arguments,
                frozenset(self.reads - before),
            )
        finally:
            self.scopes.pop()

        self.memo[key] = (dependencies, result)
        return result


# -----------------------------------------------------------------------------
# Session specific runtime classes
# -----------------------------------------------------------------------------


class SessionContext(Context):
    """Context that reports read access to the active session runtime."""

    def resolve_or_missing(self, key: str) -> t.Any:
        runtime = self.environment.session_runtime  # type: ignore[attr-defined]
        rv = super().resolve_or_missing(key)
        if runtime is not None:
            runtime.note_read("resolve", key, rv)
        return rv

    def __contains__(self, name: str) -> bool:
        runtime = self.environment.session_runtime  # type: ignore[attr-defined]
        rv = super().__contains__(name)
        if runtime is not None:
            runtime.note_read("contains", name, rv)
        return rv

    def get_all(self) -> dict[str, t.Any]:
        runtime = self.environment.session_runtime  # type: ignore[attr-defined]
        snapshot = super().get_all()
        if runtime is not None:
            runtime.note_read("enumerate", None, snapshot)
        return snapshot

    def call(self, __obj: t.Any, *args: t.Any, **kwargs: t.Any) -> t.Any:
        runtime = self.environment.session_runtime  # type: ignore[attr-defined]
        if runtime is None or not runtime.active:
            return super().call(__obj, *args, **kwargs)

        # Macros and generated caller closures define template text; push a
        # marker so that callback occurrences inside their bodies can be
        # attributed to this particular invocation.
        constructs_call = isinstance(__obj, Macro) or _is_template_function(
            runtime, __obj
        )
        if constructs_call:
            runtime.enter_call(sys._getframe().f_back)

        try:
            if constructs_call or __obj in _STATEFUL_CALLABLES:
                return Context.call(self, __obj, *args, **kwargs)

            # Mirror Context.call's derived context so reads through loop and
            # block locals can be revalidated at the same occurrence.
            effective = self
            target = __obj
            if (
                hasattr(__obj, "__call__")
                and _PassArg.from_obj(__obj.__call__) is not None
            ):
                target = __obj.__call__
            if _PassArg.from_obj(target) is _PassArg.context:
                if kwargs.get("_loop_vars"):
                    effective = effective.derived(kwargs["_loop_vars"])
                if kwargs.get("_block_vars"):
                    effective = effective.derived(kwargs["_block_vars"])

            def invoke() -> t.Any:
                return Context.call(self, __obj, *args, **kwargs)

            return runtime.invoke(
                sys._getframe().f_back,
                invoke,
                (__obj, *args),
                kwargs,
                context=effective,
            )
        finally:
            if constructs_call:
                runtime.exit_call()


class SessionCodeGenerator(CodeGenerator):
    """Code generator that annotates ``for`` loops with loop path markers."""

    def visit_For(self, node, frame) -> None:  # type: ignore[no-untyped-def]
        loop_frame = frame.inner()
        loop_frame.loop_frame = True
        test_frame = frame.inner()
        else_frame = frame.inner()

        extended_loop = (
            node.recursive
            or "loop"
            in find_undeclared(node.iter_child_nodes(only=("body",)), ("loop",))
            or any(block.scoped for block in node.find_all(nodes.Block))
        )

        loop_ref = None
        if extended_loop:
            loop_ref = loop_frame.symbols.declare_parameter("loop")

        loop_frame.symbols.analyze_node(node, for_branch="body")
        if node.else_:
            else_frame.symbols.analyze_node(node, for_branch="else")

        if node.test:
            loop_filter_func = self.temporary_identifier()
            test_frame.symbols.analyze_node(node, for_branch="test")
            self.writeline(f"{self.func(loop_filter_func)}(fiter):", node.test)
            self.indent()
            self.enter_frame(test_frame)
            self.writeline(self.choose_async("async for ", "for "))
            self.visit(node.target, loop_frame)
            self.write(" in ")
            self.write(self.choose_async("auto_aiter(fiter)", "fiter"))
            self.write(":")
            self.indent()
            self.writeline("if ", node.test)
            self.visit(node.test, test_frame)
            self.write(":")
            self.indent()
            self.writeline("yield ")
            self.visit(node.target, loop_frame)
            self.outdent(3)
            self.leave_frame(test_frame, with_python_scope=True)

        if node.recursive:
            self.writeline(
                f"{self.func('loop')}(reciter, loop_render_func, depth=0):", node
            )
            self.indent()
            self.buffer(loop_frame)
            else_frame.buffer = loop_frame.buffer

        if extended_loop:
            self.writeline(f"{loop_ref} = missing")

        for name in node.find_all(nodes.Name):
            if name.ctx == "store" and name.name == "loop":
                self.fail(
                    "Can't assign to special loop variable in for-loop target",
                    name.lineno,
                )

        if node.else_:
            iteration_indicator = self.temporary_identifier()
            self.writeline(f"{iteration_indicator} = 1")

        # -- session instrumentation: open the loop scope --------------
        self.writeline("__rs.enter_loop()", node)
        self.writeline("try:", node)
        self.indent()
        # ---------------------------------------------------------------

        self.writeline(self.choose_async("async for ", "for "), node)
        self.visit(node.target, loop_frame)
        if extended_loop:
            self.write(f", {loop_ref} in {self.choose_async('Async')}LoopContext(")
        else:
            self.write(" in ")

        if node.test:
            self.write(f"{loop_filter_func}(")
        if node.recursive:
            self.write("reciter")
        else:
            if self.environment.is_async and not extended_loop:
                self.write("auto_aiter(")
            self.visit(node.iter, frame)
            if self.environment.is_async and not extended_loop:
                self.write(")")
        if node.test:
            self.write(")")

        if node.recursive:
            self.write(", undefined, loop_render_func, depth):")
        else:
            self.write(", undefined):" if extended_loop else ":")

        self.indent()
        self.enter_frame(loop_frame)

        # -- session instrumentation: per iteration markers ------------
        self.writeline("__rs.step_loop()")
        self.writeline("try:")
        self.indent()
        # ---------------------------------------------------------------

        self.writeline("_loop_vars = {}")
        self.blockvisit(node.body, loop_frame)
        if node.else_:
            self.writeline(f"{iteration_indicator} = 0")

        # -- close the per-iteration try --------------------------------
        self.outdent()
        self.writeline("finally:")
        self.indent()
        self.writeline("__rs.unstep_loop()")
        self.outdent()
        self.outdent()
        # ---------------------------------------------------------------

        self.leave_frame(
            loop_frame, with_python_scope=node.recursive and not node.else_
        )

        if node.else_:
            self.writeline(f"if {iteration_indicator}:")
            self.indent()
            self.enter_frame(else_frame)
            self.blockvisit(node.else_, else_frame)
            self.leave_frame(else_frame)
            self.outdent()

        # -- close the loop scope ---------------------------------------
        self.outdent()
        self.writeline("finally:")
        self.indent()
        self.writeline("__rs.exit_loop()")
        self.outdent()
        # ---------------------------------------------------------------

        if node.recursive:
            self.return_buffer_contents(loop_frame)
            self.outdent()
            self.start_write(frame, node)
            self.write(f"{self.choose_async('await ')}loop(")
            if self.environment.is_async:
                self.write("auto_aiter(")
            self.visit(node.iter, frame)
            if self.environment.is_async:
                self.write(")")
            self.write(", loop)")
            self.end_write(frame)

        if self._assign_stack:
            self._assign_stack[-1].difference_update(loop_frame.symbols.stores)


# Late imports needed by the copied visit_For implementation.
class SessionTemplate(Template):
    """Template variant that exposes the session runtime as ``__rs``."""

    @classmethod
    def from_code(cls, environment, code, globals, uptodate=None):  # type: ignore[no-untyped-def]
        runtime = getattr(environment, "session_runtime", None)
        namespace = {
            "environment": environment,
            "__file__": code.co_filename,
            "__rs": runtime,
        }
        exec(code, namespace)
        rv = cls._from_namespace(environment, namespace, globals)
        rv._uptodate = uptodate
        return rv

    def _get_default_module(self, ctx: Context | None = None):  # type: ignore[override]
        # Never cache imported modules across session renders; their rendered
        # body and exported values must be rebuilt from the new snapshot.
        if self.environment.is_async:
            raise RuntimeError("Module is not available in async mode.")

        if ctx is not None:
            keys = ctx.globals_keys - self.globals.keys()
            if keys:
                return self.make_module({k: ctx.parent[k] for k in keys})

        return self.make_module()


_SESSION_ENV_CLASSES: dict[type, type] = {}


def _session_environment_class(base_cls: type) -> type:
    cls = _SESSION_ENV_CLASSES.get(base_cls)
    if cls is None:
        cls = type(
            f"_Session{base_cls.__name__}",
            (SessionEnvironment, base_cls),
            {"session_runtime": None},
        )
        _SESSION_ENV_CLASSES[base_cls] = cls
    return cls


class SessionEnvironment(Environment):
    """Overlay environment used while rendering a session.

    Its filter/test tables contain memoizing wrappers, its context and code
    generator classes perform the session instrumentation and pipeline calls
    dispatched through :meth:`call_filter`/:meth:`call_test` are memoized as
    well.
    """

    context_class = SessionContext
    code_generator_class = SessionCodeGenerator
    template_class = SessionTemplate

    #: The active :class:`SessionRuntime`, set by the owning session.
    session_runtime: SessionRuntime | None = None

    def _filter_test_common(self, name, value, args, kwargs, context, eval_ctx, is_filter):  # type: ignore[no-untyped-def]
        runtime = self.session_runtime
        if runtime is None or not runtime.active:
            return Environment._filter_test_common(
                self, name, value, args, kwargs, context, eval_ctx, is_filter
            )

        # The nearest frame outside of this environment is the pipeline
        # closure (or user code invoking call_filter/call_test directly).
        frame = sys._getframe().f_back
        while frame is not None and frame.f_globals.get("__name__") in (
            __name__,
            "jinja2.environment",
        ):
            frame = frame.f_back

        eager = is_filter and name in _PIPELINE_FILTERS

        def invoke() -> t.Any:
            runtime.passthrough += 1
            try:
                return Environment._filter_test_common(
                    self, name, value, args, kwargs, context, eval_ctx, is_filter
                )
            finally:
                runtime.passthrough -= 1

        return runtime.invoke(
            frame,
            invoke,
            (value, *(args or ())),
            kwargs or {},
            eager_iterator=eager,
            context=context,
            walk_to_template=False,
        )

    def call(self, context, __obj, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Used instead of Context.call by compiled sandboxed templates.
        runtime = self.session_runtime

        def invoke() -> t.Any:
            return super(SessionEnvironment, self).call(
                context, __obj, *args, **kwargs
            )

        if (
            runtime is None
            or not runtime.active
            or isinstance(__obj, Macro)
            or _is_template_function(runtime, __obj)
            or __obj in _STATEFUL_CALLABLES
        ):
            return invoke()

        return runtime.invoke(
            sys._getframe().f_back,
            invoke,
            (__obj, *args),
            kwargs,
            context=context,
        )


def _wrap_callable(
    environment: SessionEnvironment,
    name: str,
    func: t.Any,
    eager: bool,
) -> t.Any:
    if not callable(func):
        return func

    @functools.wraps(func)
    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        runtime = environment.session_runtime
        if (
            runtime is None
            or not runtime.active
            or runtime.passthrough
        ):
            return func(*args, **kwargs)
        context = next(
            (arg for arg in args if isinstance(arg, Context)), None
        )
        return runtime.invoke(
            sys._getframe().f_back,
            functools.partial(func, *args, **kwargs),
            args,
            kwargs,
            eager_iterator=eager,
            context=context,
        )

    wrapper.__wrapped__ = func
    return wrapper


# -----------------------------------------------------------------------------
# The public session object
# -----------------------------------------------------------------------------


class RenderSession:
    """An independent, dependency aware render session.

    Instances are created by :meth:`Template.render_session`; they are not
    meant to be instantiated directly.
    """

    def __init__(self) -> None:
        self._environment: SessionEnvironment | None = None
        self._template: Template | None = None
        self._runtime: SessionRuntime | None = None
        self._output: str | None = None
        self._closed = False

    @classmethod
    def start(cls, template: Template, mapping: t.Mapping[str, t.Any] | None) -> "RenderSession":
        if template.environment.is_async:
            raise RuntimeError(
                "render sessions are not supported by asynchronous environments"
            )

        data = validate_mapping(mapping if mapping is not None else {})

        session = cls()
        runtime = SessionRuntime()
        session._environment = _make_session_environment(template.environment, runtime)
        session._template = _compile_session_template(template, session._environment)
        session._runtime = runtime

        session._output = session._render(data)
        return session

    @property
    def closed(self) -> bool:
        """:class:`bool` that is ``True`` after :meth:`close` or an error."""
        return self._closed

    def render(self) -> str:
        """Return the complete rendered string for the current context."""
        self._ensure_usable()
        return t.cast(str, self._output)

    def update(self, mapping: t.Mapping[str, t.Any]) -> str:
        """Replace the session context with *mapping* and render again.

        Keys omitted from *mapping* are removed from the context.  Nested
        changes are expressed by submitting a fresh snapshot; uncommitted in
        place mutations are not observed.
        """
        self._ensure_usable()
        data = validate_mapping(mapping)
        self._output = self._render(data)
        return t.cast(str, self._output)

    def close(self) -> None:
        """Close the session and release cached results."""
        self._closed = True
        self._output = None
        if self._runtime is not None:
            self._runtime.memo.clear()
            self._runtime.active = False
        self._template = None
        self._environment = None

    def _ensure_usable(self) -> None:
        if self._closed:
            raise RuntimeError("this render session is closed")

    def _render(self, data: dict[str, t.Any]) -> str:
        runtime = t.cast(SessionRuntime, self._runtime)
        environment = t.cast(SessionEnvironment, self._environment)
        template = t.cast(Template, self._template)

        runtime.active = True
        runtime.loop_stack = []
        runtime.scopes = []
        runtime.reads = set()
        runtime.call_markers = []
        runtime.passthrough = 0
        runtime.suppress_reads = False

        context = template.new_context(data)
        succeeded = False
        try:
            try:
                result = environment.concat(template.root_render_func(context))
            except Exception:
                environment.handle_exception()
            else:
                succeeded = True
                return result
        finally:
            runtime.active = False
            if not succeeded:
                # Errors are propagated to the caller; a failed session is
                # closed and cannot be reused.  Other sessions stay usable.
                self._closed = True

        raise RuntimeError("unreachable")  # pragma: no cover


def _make_session_environment(
    base: Environment, runtime: SessionRuntime
) -> SessionEnvironment:
    overlay = base.overlay(cache_size=-1, bytecode_cache=None)
    overlay.__class__ = _session_environment_class(type(base))
    overlay.session_runtime = runtime

    # Copy the shared lookup tables so that wrappers do not affect the base
    # environment or other sessions.
    overlay.filters = {
        name: _wrap_callable(
            t.cast(SessionEnvironment, overlay),
            name,
            func,
            name in _PIPELINE_FILTERS,
        )
        for name, func in base.filters.items()
    }
    overlay.tests = {
        name: _wrap_callable(
            t.cast(SessionEnvironment, overlay), name, func, False
        )
        for name, func in base.tests.items()
    }
    return t.cast(SessionEnvironment, overlay)


def _compile_session_template(
    template: Template, environment: SessionEnvironment
) -> Template:
    base_environment = template.environment
    template_globals = template.globals.maps[0] if template.globals.maps else {}

    if template.name is not None and base_environment.loader is not None:
        return environment.get_template(template.name, globals=template_globals)

    source = getattr(template, "_rs_source", None)
    if source is None:
        raise RuntimeError(
            "cannot start a render session for a template without retained"
            " source; create the template with Environment.from_string or a"
            " loader"
        )
    return environment.from_string(source, globals=template_globals)
