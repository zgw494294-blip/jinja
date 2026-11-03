"""Tests for synchronous dependency aware render sessions."""

from __future__ import annotations

import copy

import pytest
from markupsafe import Markup

from jinja2 import DictLoader
from jinja2 import Environment
from jinja2 import pass_context
from jinja2 import pass_environment
from jinja2 import RenderSession
from jinja2 import StrictUndefined
from jinja2 import Template
from jinja2 import UndefinedError
from jinja2.bccache import BytecodeCache


class Recorder:
    """Record invocations of deterministic callbacks."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def reset(self) -> None:
        self.calls = []

    def filter(self, name):
        def callback(value, *args):
            self.calls.append((name, value, *args))
            return f"{name}:{value}"

        callback.__name__ = name
        return callback

    def context_filter(self, name):
        def callback(context, value, *args):
            self.calls.append((name, value, *args))
            factor = context.get("factor", 1)
            return f"{name}:{value * factor}"

        return pass_context(callback)

    def function(self, name):
        def callback(*args):
            self.calls.append((name, *args))
            return sum(args)

        return callback


def make_env(**kwargs):
    rec = Recorder()
    env = Environment(**kwargs)
    env.filters["f1"] = rec.filter("f1")
    env.filters["f2"] = rec.filter("f2")
    env.filters["cf"] = rec.context_filter("cf")
    env.tests["big"] = lambda value: value >= 10
    env.globals["add"] = rec.function("add")
    return env, rec


def check_update(session, template, rec, data):
    """Render normally first, then verify the session update matches and
    return the session output. Session callback calls are in rec.calls."""
    want = template.render(copy.deepcopy(data))
    rec.reset()
    got = session.update(copy.deepcopy(data))
    assert got == want, (got, want)
    return got


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


def test_render_session_returns_session_and_matches_render():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}")
    s = t.render_session({"a": 1})
    assert isinstance(s, RenderSession)
    assert s.render() == t.render({"a": 1}) == "f1:1"


def test_render_without_mapping():
    env, _ = make_env()
    t = env.from_string("ok")
    assert t.render_session().render() == "ok"
    assert t.render_session(None).render() == "ok"


def test_update_returns_full_string_and_render_reflects_it():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}/{{ b | f2 }}")
    s = t.render_session({"a": 1, "b": 2})
    out = s.update({"a": 5, "b": 6})
    assert out == "f1:5/f2:6"
    assert s.render() == out


def test_omitted_keys_are_deleted():
    env = Environment()
    t = env.from_string("{% if v is defined %}[{{ v }}]{% else %}none{% endif %}")
    s = t.render_session({"v": "x"})
    assert s.render() == "[x]"
    assert s.update({}) == "none"
    assert s.update({"v": "y"}) == "[y]"


def test_update_replaces_nested_snapshot():
    env = Environment()
    t = env.from_string("{{ u.v }}")
    s = t.render_session({"u": {"v": 1}})
    assert s.render() == "1"
    assert s.update({"u": {"v": 2}}) == "2"
    t2 = env.from_string("{% for x in xs %}{{ x }}{% endfor %}")
    s2 = t2.render_session({"xs": [1, 2]})
    assert s2.update({"xs": [3]}) == "3"


def test_in_place_mutation_is_not_observed_without_submission():
    env = Environment()
    t = env.from_string("{{ a }}")
    data = {"a": 1}
    s = t.render_session(data)
    assert s.render() == "1"
    # uncommitted in place mutation does not change the last produced string
    data["a"] = 2
    assert s.render() == "1"
    # submitting the (mutated) snapshot is the same as rendering that data
    assert s.update(data) == t.render(data) == "2"


def test_input_validation():
    env, _ = make_env()
    t = env.from_string("{{ a }}")
    t.render_session({"a": 1})
    t.render_session(
        {"a": "s", "b": 1.5, "c": True, "d": None,
         "e": [1, [2], (3,)], "f": {"x": {"y": 1}}}
    )
    with pytest.raises(TypeError):
        t.render_session("not a dict")
    with pytest.raises(TypeError):
        t.render_session({"a": {1, 2}})
    with pytest.raises(TypeError):
        t.render_session({"a": object()})

    s = t.render_session({"a": 1})
    with pytest.raises(TypeError):
        s.update({"a": b"bytes"})
    with pytest.raises(TypeError):
        s.update({1: "a"})

    cyc = {}
    cyc["self"] = cyc
    with pytest.raises(TypeError):
        t.render_session(cyc)
    cyc_list = [1]
    cyc_list.append(cyc_list)
    with pytest.raises(TypeError):
        t.render_session({"a": cyc_list})


def test_async_environment_rejected():
    env = Environment(enable_async=True)
    t = env.from_string("x")
    with pytest.raises(RuntimeError):
        t.render_session({})


# ---------------------------------------------------------------------------
# Callback reuse and invalidation
# ---------------------------------------------------------------------------


def test_filter_not_called_on_same_update():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}-{{ b | f2 }}")
    s = t.render_session({"a": 1, "b": 2})
    rec.reset()
    s.update({"a": 1, "b": 2})
    assert rec.calls == []


def test_unrelated_change_does_not_invoke_active_branch_callback():
    env, rec = make_env()
    t = env.from_string("{% if flag %}{{ a | f1 }}{% else %}no{% endif %}")
    s = t.render_session({"flag": True, "a": 7, "other": 1})
    assert check_update(s, t, rec, {"flag": True, "a": 7, "other": 2}) == "f1:7"


def test_branch_switch_and_reentry_uses_fresh_data():
    env, rec = make_env()
    t = env.from_string(
        "{% if flag %}{{ a | f1 }}{% elif flag2 %}{{ b | f2 }}{% else %}"
        "none{% endif %}"
    )
    s = t.render_session({"flag": True, "flag2": False, "a": 1, "b": 2})
    assert s.render() == "f1:1"
    assert check_update(s, t, rec, {"flag": False, "flag2": True, "a": 1, "b": 2}) == "f2:2"
    assert check_update(s, t, rec, {"flag": True, "flag2": False, "a": 1, "b": 2}) == "f1:1"
    # changing the reentered branch's value invalidates only it
    calls = check_update(s, t, rec, {"flag": True, "flag2": False, "a": 9, "b": 2})
    assert [c[0] for c in calls] == ["f1"]


def test_loop_body_call_reuse_per_index():
    env, rec = make_env()
    t = env.from_string(
        "{% for row in rows %}{{ row.n | f1 }};"
        "{% if row.m >= 10 %}{{ row.m | f2 }}{% endif %}{% endfor %}"
    )
    data = {"rows": [{"n": "a", "m": 1}, {"n": "b", "m": 20}]}
    s = t.render_session(data)
    assert check_update(s, t, rec, copy.deepcopy(data)) == "f1:a;f1:b;f2:20"
    changed = {"rows": [{"n": "a", "m": 1}, {"n": "b", "m": 21}]}
    calls = check_update(s, t, rec, changed)
    assert [(c[0], c[1]) for c in calls] == [("f2", 21)]


def test_loop_resize_and_duplicate_values():
    env, rec = make_env()
    t = env.from_string("{% for x in xs %}{{ x | f1 }},{% endfor %}")
    s = t.render_session({"xs": [1, 1, 2]})
    assert len(rec.calls) == 3
    assert s.render() == "f1:1,f1:1,f1:2,"
    calls = check_update(s, t, rec, {"xs": [1, 1, 2, 3]})
    assert [c[1] for c in calls] == [3]
    calls = check_update(s, t, rec, {"xs": [1, 2]})
    assert [c[1] for c in calls] == [2]


def test_nested_loops_path():
    env, rec = make_env()
    t = env.from_string(
        "{% for a in xs %}[{% for b in ys %}{{ (a * b) | f1 }}"
        "{% endfor %}]{% endfor %}"
    )
    data = {"xs": [1, 2], "ys": [10, 20]}
    s = t.render_session(data)
    assert check_update(s, t, rec, copy.deepcopy(data)) == "[f1:10f1:20][f1:20f1:40]"
    calls = check_update(s, t, rec, {"xs": [1, 3], "ys": [10, 20]})
    assert [c[1] for c in calls] == [30, 60]


def test_computed_index_invalidation():
    env, rec = make_env()
    t = env.from_string("{{ xs[i] | f1 }}")
    s = t.render_session({"xs": [10, 20], "i": 0})
    assert check_update(s, t, rec, {"xs": [10, 20], "i": 0}) == "f1:10"
    calls = check_update(s, t, rec, {"xs": [10, 20], "i": 1})
    assert [c[1] for c in calls] == [20]
    calls = check_update(s, t, rec, {"xs": [10, 21], "i": 1})
    assert [c[1] for c in calls] == [21]


def test_global_function_reuse():
    env, rec = make_env()
    t = env.from_string("{{ add(a, b) }}/{{ add(a, c) }}")
    s = t.render_session({"a": 1, "b": 2, "c": 3})
    assert s.render() == "3/4"
    calls = check_update(s, t, rec, {"a": 1, "b": 2, "c": 4})
    assert [c for c in calls] == [("add", 1, 4)]
    assert s.render() == "3/5"


def test_pipeline_map_select_reuse():
    env, rec = make_env()
    t = env.from_string('{{ xs | select("big") | map("f1") | list }}')
    s = t.render_session({"xs": [1, 10, 20]})
    assert s.render() == "['f1:10', 'f1:20']"
    assert check_update(s, t, rec, {"xs": [1, 10, 20]}) == "['f1:10', 'f1:20']"
    # a filtered-out value changes but never reaches the mapped filter
    calls = check_update(s, t, rec, {"xs": [2, 10, 20]})
    assert [c[1] for c in calls if c[0] == "f1"] == []
    # a newly selected value is mapped; the stable one is reused
    calls = check_update(s, t, rec, {"xs": [2, 11, 20]})
    assert [c[1] for c in calls if c[0] == "f1"] == [11]
    assert s.render() == "['f1:11', 'f1:20']"


def test_pipeline_attribute_and_selectattr():
    env, rec = make_env()
    env.filters["label"] = rec.filter("label")
    t = env.from_string(
        "{{ xs | selectattr('on') | map(attribute='n') | list }}"
        "{{ xs | map(attribute='n') | map('label') | join('|') }}"
    )
    data = {"xs": [{"n": "a", "on": True}, {"n": "b", "on": False}]}
    s = t.render_session(data)
    assert s.render() == "['a']label:a|label:b"
    assert check_update(s, t, rec, copy.deepcopy(data)) == "['a']label:a|label:b"
    changed = {"xs": [{"n": "a", "on": True}, {"n": "c", "on": False}]}
    calls = check_update(s, t, rec, changed)
    assert [c[1] for c in calls] == ["c"]
    toggled = {"xs": [{"n": "a", "on": True}, {"n": "c", "on": True}]}
    calls = check_update(s, t, rec, toggled)
    assert calls == []


def test_pipeline_inside_loop():
    env, rec = make_env()
    t = env.from_string(
        "{% for row in rows %}{{ row | map('f1') | list }};{% endfor %}"
    )
    data = {"rows": [[1, 2], [3, 4]]}
    s = t.render_session(data)
    assert check_update(s, t, rec, copy.deepcopy(data)) == (
        "['f1:1', 'f1:2'];['f1:3', 'f1:4'];"
    )
    # second row changes: the unchanged 3 is reused, only 5 is computed
    calls = check_update(s, t, rec, {"rows": [[1, 2], [3, 5]]})
    assert [c[1] for c in calls] == [5]


def test_pipeline_consumed_by_loop():
    env, rec = make_env()
    t = env.from_string("{% for v in xs | map('f1') %}{{ v }};{% endfor %}")
    s = t.render_session({"xs": [1, 2]})
    assert s.render() == "f1:1;f1:2;"
    assert check_update(s, t, rec, {"xs": [1, 2]}) == "f1:1;f1:2;"


def test_pass_context_reads_are_dependencies():
    env, rec = make_env()
    t = env.from_string("{{ a | cf }}")
    s = t.render_session({"a": 10})
    assert s.render() == "cf:10"
    assert check_update(s, t, rec, {"a": 10}) == "cf:10"
    calls = check_update(s, t, rec, {"a": 10, "factor": 3})
    assert [c[1] for c in calls] == [10]
    assert s.render() == "cf:30"
    calls = check_update(s, t, rec, {"a": 10, "factor": 3, "unused": 1})
    assert calls == []


def test_pass_context_get_and_enumeration_and_existence():
    log = []

    @pass_context
    def reader(context, value):
        log.append(1)
        context.get("a")
        exists = "a" in context
        tuple(sorted(context.get_all()))
        return f"{value}:{exists}"

    env = Environment()
    env.filters["reader"] = reader
    t = env.from_string("{{ 0 | reader }}")
    s = t.render_session({"a": 1})
    log.clear()
    s.update({"a": 1})
    assert log == []
    log.clear()
    s.update({"a": 1, "b": 2})  # enumeration dependency
    assert log == [1]
    log.clear()
    s.update({"a": 2, "b": 2})  # value dependency
    assert log == [1]
    log.clear()
    s.update({"b": 2})  # a deleted: existence + get dependency
    assert log == [1]
    log.clear()
    s.update({"b": 2})
    assert log == []


def test_pass_environment_function():
    log = []

    @pass_environment
    def etype(environment, value):
        log.append(value)
        return f"{type(environment).__name__}:{value}"

    env = Environment()
    env.filters["etype"] = etype
    t = env.from_string("{{ x | etype }}")
    s = t.render_session({"x": 1})
    assert s.render().endswith(":1")
    log.clear()
    s.update({"x": 1})
    assert log == []
    assert s.update({"x": 2}) == s.render()


# ---------------------------------------------------------------------------
# Control structures
# ---------------------------------------------------------------------------


def test_for_else_break_continue_and_loop_metadata():
    env = Environment(extensions=["jinja2.ext.loopcontrols"])
    t = env.from_string(
        "{% for x in xs %}"
        "{% if x == 0 %}{% continue %}{% endif %}"
        "{% if x == 3 %}{% break %}{% endif %}"
        "{{ loop.index }}:{{ x }}:{{ loop.length }}:{{ loop.first }}:"
        "{{ loop.last }},{% else %}empty{% endfor %}"
    )
    cases = (
        {"xs": [0, 1, 3, 4]},
        {"xs": [0, 1, 3]},
        {"xs": [1]},
        {"xs": []},
        {"xs": [0, 1, 2, 3, 4]},
        {"xs": [0, 1, 2]},
    )
    s = t.render_session({"xs": [0, 1, 3, 4]})
    for data in cases:
        want = t.render(data)
        assert s.update(data) == want, data


def test_recursive_loop():
    env = Environment()
    t = env.from_string(
        "{% for item in items recursive %}"
        "{{ item.name }}{% if item.children %}("
        "{{ loop(item.children) }}){% endif %};"
        "{% endfor %}"
    )
    d1 = {"items": [
        {"name": "a", "children": [
            {"name": "a1", "children": []},
            {"name": "a2", "children": [
                {"name": "a2x", "children": []}]},
        ]},
        {"name": "b", "children": []},
    ]}
    d2 = {"items": [
        {"name": "a", "children": [
            {"name": "a1", "children": []},
            {"name": "a2", "children": [
                {"name": "a2y", "children": []}]},
        ]},
        {"name": "c", "children": []},
    ]}
    s = t.render_session(d1)
    assert s.render() == t.render(d1)
    assert s.update(d2) == t.render(d2)
    assert s.update(d1) == t.render(d1)


def test_lexical_set_and_filter_block():
    env, rec = make_env()
    t = env.from_string(
        "{% set total = a + b %}{{ total | f1 }}"
        "{% filter f2 %}x{{ a }}y{% endfilter %}"
    )
    s = t.render_session({"a": 1, "b": 2})
    assert check_update(s, t, rec, {"a": 1, "b": 2}) == "f1:3f2:x1y"
    calls = check_update(s, t, rec, {"a": 2, "b": 2})
    assert {c[0] for c in calls} == {"f1", "f2"}


def test_macro_arguments_and_closures():
    env, rec = make_env()
    t = env.from_string(
        "{% macro m(p, q=10) %}{{ p | f1 }}/{{ q | f2 }}/{{ k | cf }}"
        "{% endmacro %}"
        "{{ m(a) }}{{ m(a, b) }}"
    )
    s = t.render_session({"a": 1, "b": 2, "k": 4})
    assert s.render() == t.render({"a": 1, "b": 2, "k": 4})
    # factor appears for the first time: the context reading macro re-runs
    calls = check_update(s, t, rec, {"a": 1, "b": 2, "k": 4, "factor": 1})
    assert [c[0] for c in calls] == ["cf", "cf"]
    # only b changes: one f2 occurrence inside the macro
    calls = check_update(s, t, rec, {"a": 1, "b": 3, "k": 4, "factor": 1})
    assert [(c[0], c[1]) for c in calls] == [("f2", 3)]
    # factor changes: both macro invocations re-read it
    calls = check_update(s, t, rec, {"a": 1, "b": 3, "k": 4, "factor": 2})
    assert [c[0] for c in calls] == ["cf", "cf"]


def test_call_block():
    env = Environment()

    def wrap(value, caller):
        return f"<{value}|{caller()}>"

    env.globals["wrap"] = wrap
    t = env.from_string(
        "{% for x in xs %}{% call(y) wrap(x) %}[{{ y }}-{{ x }}]"
        "{% endcall %}{% endfor %}"
    )
    data = {"xs": [1, 2]}
    s = t.render_session(data)
    assert s.render() == t.render(data)
    assert s.update({"xs": [1, 3]}) == t.render({"xs": [1, 3]})
    assert s.update(data) == t.render(data)


def test_static_include():
    loader = DictLoader({
        "inc.html": "{{ n | f1 }}-{{ factor }}",
        "main.html": '{% include "inc.html" %}|{{ m | f2 }}',
    })
    env, rec = make_env(loader=loader)
    t = env.get_template("main.html")
    data = {"n": 1, "m": 2, "factor": 9}
    s = t.render_session(data)
    assert s.render() == t.render(data)
    assert check_update(s, t, rec, {"n": 1, "m": 2, "factor": 9}) == "f1:1-9|f2:2"
    calls = check_update(s, t, rec, {"n": 5, "m": 2, "factor": 9})
    assert [c[0] for c in calls] == ["f1"]


def test_import_and_extends_super():
    loader = DictLoader({
        "lib.html": "{% macro greet(name) %}hi {{ name | f1 }}{% endmacro %}",
        "base.html": "B{% block content %}base {{ v }}{% endblock %}E",
        "child.html":
            '{% from "lib.html" import greet %}'
            '{% extends "base.html" %}'
            "{% block content %}{{ greet(v) }}|{{ super() }}{% endblock %}",
    })
    env, rec = make_env(loader=loader)
    t = env.get_template("child.html")
    s = t.render_session({"v": "a"})
    assert s.render() == "Bhi f1:a|base aE"
    assert check_update(s, t, rec, {"v": "a"}) == "Bhi f1:a|base aE"
    calls = check_update(s, t, rec, {"v": "b"})
    assert [c[1] for c in calls] == ["b"]


# ---------------------------------------------------------------------------
# Undefined, autoescape, errors
# ---------------------------------------------------------------------------


def test_undefined_semantics_preserved():
    env = Environment(undefined=StrictUndefined)
    t = env.from_string("{% if v is defined %}{{ v }}{% else %}undef{% endif %}")
    s = t.render_session({})
    assert s.render() == "undef"
    assert s.update({"v": "x"}) == "x"
    with pytest.raises(UndefinedError):
        env.from_string("{{ v }}").render_session({})


def test_debug_undefined_content():
    import jinja2

    env = Environment(undefined=jinja2.DebugUndefined)
    t = env.from_string("{{ v }}")
    s = t.render_session({})
    assert s.render() == "{{ v }}"
    assert s.update({"v": 1}) == "1"
    assert s.update({}) == "{{ v }}"


def test_autoescape_and_markup():
    env = Environment(autoescape=True)
    t = env.from_string("<a href='{{ x }}'>{{ y | safe }}</a>")
    data = {"x": "<>&\"'", "y": "<b>"}
    s = t.render_session(data)
    out = s.render()
    assert out == t.render(data)
    # unsafe data is escaped, explicitly safe data is not
    assert "<b>" in out
    assert "&lt;&gt;&amp;&#34;&#39;" in out
    assert s.update({"x": "n", "y": "<b>"}) == t.render({"x": "n", "y": "<b>"})

    # Markup values passed as data keep their meaning
    t2 = env.from_string("{{ x }}{{ x | safe }}")
    s2 = t2.render_session({"x": Markup("<i>")})
    assert s2.render() == "<i><i>"
    assert s2.update({"x": Markup("<u>")}) == "<u><u>"


def test_error_propagates_closes_session_and_leaves_others_intact():
    env, rec = make_env()
    t = env.from_string("{% for x in xs %}{{ x | f1 }}{% endfor %}")
    good = t.render_session({"xs": [1, 2]})

    strict = Environment(undefined=StrictUndefined)
    bt = strict.from_string("{{ a }}")
    bs = bt.render_session({"a": 1})
    with pytest.raises(UndefinedError):
        bs.update({})
    assert bs.closed
    with pytest.raises(RuntimeError):
        bs.render()
    with pytest.raises(RuntimeError):
        bs.update({"a": 1})
    bs.close()
    # untouched sessions keep working independently
    assert good.render() == "f1:1f1:2"
    assert good.update({"xs": [9]}) == "f1:9"


def test_close_is_idempotent_and_releases_cache():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}")
    s = t.render_session({"a": 1})
    runtime = s._runtime
    assert len(runtime.memo) >= 1
    s.close()
    assert s.closed
    assert runtime.memo == {}
    s.close()
    with pytest.raises(RuntimeError):
        s.render()


def test_sessions_are_independent():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}")
    s1 = t.render_session({"a": 1})
    s2 = t.render_session({"a": 100})
    assert s1.render() == "f1:1"
    assert s2.render() == "f1:100"
    assert s1.update({"a": 2}) == "f1:2"
    assert s2.render() == "f1:100"
    assert s2.update({"a": 200}) == "f1:200"
    assert s1.render() == "f1:2"


# ---------------------------------------------------------------------------
# Non-session behavior stays unchanged
# ---------------------------------------------------------------------------


def test_normal_render_still_invokes_every_callback():
    env, rec = make_env()
    t = env.from_string("{{ a | f1 }}{% for x in xs %}{{ x | f1 }}{% endfor %}")
    t.render({"a": 1, "xs": [1, 2]})
    assert len(rec.calls) == 3
    rec.reset()
    t.render({"a": 1, "xs": [1, 2]})
    assert len(rec.calls) == 3


def test_generator_stream_unaffected():
    env, rec = make_env()
    t = env.from_string("{% for x in xs %}{{ x | f1 }}{% endfor %}")
    assert "".join(t.generate({"xs": [1, 2]})) == "f1:1f1:2"
    assert list(t.stream({"xs": [1, 2]})) == ["f1:1", "f1:2"]


def test_bytecode_cache_remains_default_for_normal_templates():
    used = []

    class Cache(BytecodeCache):
        def load_bytecode(self, bucket):
            used.append("load")

        def dump_bytecode(self, bucket):
            used.append("dump")

    env = Environment(bytecode_cache=Cache())
    t = env.from_string("{{ a }}")
    assert t.render({"a": 1}) == "1"
    assert t.render({"a": 2}) == "2"
    s = t.render_session({"a": 3})
    assert s.render() == "3"
    assert t.render({"a": 4}) == "4"


def test_sandboxed_session():
    from jinja2.sandbox import SandboxedEnvironment

    env = SandboxedEnvironment()
    t = env.from_string("{{ 'x' | upper }}:{{ 1 + 2 }}")
    s = t.render_session({})
    assert s.render() == "X:3"
    assert s.update({}) == "X:3"


def test_namespace_usage_resets_per_render():
    env = Environment()
    t = env.from_string(
        "{% set ns = namespace(total=0) %}"
        "{% for x in xs %}{% set ns.total = ns.total + x %}{% endfor %}"
        "{{ ns.total }}"
    )
    s = t.render_session({"xs": [1, 2, 3]})
    assert s.render() == "6"
    assert s.update({"xs": [1, 2]}) == "3"
    assert s.update({"xs": [1, 2, 3, 4]}) == "10"
    assert s.update({"xs": [1, 2, 3]}) == "6"


def test_finalize_still_applied():
    env = Environment(finalize=lambda v: f"({v})")
    t = env.from_string("{{ a }}")
    s = t.render_session({"a": 1})
    assert s.render() == "(1)"
    assert s.update({"a": 2}) == "(2)"


# ---------------------------------------------------------------------------
# Randomized equivalence against plain render
# ---------------------------------------------------------------------------


TEMPLATES = [
    "{{ a }}-{{ b }}-{{ u.v }}-{{ word }}",
    "{{ a | f1 }}/{{ b | cf }}",
    "{% if flag %}[{{ a | f1 }}]{% else %}[{{ b | f2 }}]{% endif %}",
    "{% if a > 5 %}{{ a | f1 }}{% elif b > 5 %}{{ b | f2 }}"
    "{% else %}{{ add(a, b) }}{% endif %}",
    "{% for x in xs %}{{ loop.index }}/{{ x | f1 }};"
    "{% if x > 2 %}{{ x | cf }}{% endif %}{% endfor %}",
    "{% for x in xs %}{% if x % 2 == 0 %}e{{ x }}"
    "{% else %}o{{ x }}{% endif %}{% else %}none{% endfor %}",
    "{% for row in rows %}{{ row.k | f1 }}:{{ row.v | cf }};"
    "[{% for x in row.z %}{{ x | f2 }}{% endfor %}]{% endfor %}",
    '{{ xs | select("big") | map("f1") | list }}',
    '{{ rows | selectattr("ok") | map(attribute="k") | list }}',
    "{% set t = add(a, b) %}{{ t | f1 }}{{ t * 2 }}",
    "{% macro cell(v) %}<{{ v | f1 }}>{% endmacro %}"
    "{% for x in xs %}{{ cell(x) }}{% endfor %}",
    "{% for x in xs recursive %}{{ x | f1 }}{% endfor %}",
]


def _initial_data(rng):
    return {
        "a": rng() % 10,
        "b": rng() % 10,
        "u": {"v": rng() % 10},
        "word": ["hi", "ho", "xx"][rng() % 3],
        "flag": rng() % 2 == 0,
        "factor": 1 + rng() % 3,
        "xs": [rng() % 12 for _ in range(rng() % 5)],
        "rows": [
            {"k": f"k{i}", "v": rng() % 8, "ok": rng() % 2 == 0,
             "z": [rng() % 5 for _ in range(rng() % 3)]}
            for i in range(rng() % 4)
        ],
    }


def _mutate(data, rng):
    data = copy.deepcopy(data)
    for _ in range(1 + rng() % 2):
        kind = rng() % 9
        if kind == 0:
            data["a"] = rng() % 10
        elif kind == 1:
            data["b"] = rng() % 10
        elif kind == 2:
            data["u"] = {"v": rng() % 10}
        elif kind == 3:
            data["word"] = ["hi", "ho", "xx"][rng() % 3]
        elif kind == 4:
            data["flag"] = not data["flag"]
        elif kind == 5:
            data["factor"] = 1 + rng() % 3
        elif kind == 6:
            data["xs"] = [rng() % 12 for _ in range(rng() % 5)]
        elif kind == 7 and data["rows"]:
            data["rows"][rng() % len(data["rows"])]["v"] = rng() % 8
        elif kind == 8 and data["rows"]:
            row = data["rows"][rng() % len(data["rows"])]
            row["ok"] = not row["ok"]
    return data


class _Rng:
    def __init__(self, seed):
        self.state = seed

    def __call__(self):
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state


def test_randomized_equivalence():
    env, rec = make_env()
    rng = _Rng(1234)
    for template_src in TEMPLATES:
        t = env.from_string(template_src)
        data = _initial_data(rng)
        session = t.render_session(copy.deepcopy(data))
        assert session.render() == t.render(data), (template_src, data)
        for _ in range(20):
            data = _mutate(data, rng)
            got = session.update(copy.deepcopy(data))
            want = t.render(data)
            assert got == want, (template_src, data, got, want)
