import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment, DictLoader, StrictUndefined, pass_context
from markupsafe import Markup

fails = []
def check(name, got, want):
    if got != want:
        fails.append((name, got, want))
        print(f"FAIL {name}: {got!r} != {want!r}")
    else:
        print(f"ok {name}")

# 1. pass_context reads nested value + get + get_all + contains
ccalls = []
def c_price(ctx, item):
    ccalls.append(("price", item, ctx.get("multiplier", 1)))
    return item * ctx.get("multiplier", 1)

def c_known(ctx, _v):
    ccalls.append(("known", "secret" in ctx, tuple(sorted(k for k in ctx.keys() if k != "range"))))
    return "y" if "secret" in ctx else "n"

env = Environment()
env.filters["cp"] = pass_context(c_price)
env.filters["known"] = pass_context(c_known)
t = env.from_string("{{ x | cp }}|{{ y | known }}")
s = t.render_session({"x": 10, "y": 1})
ccalls.clear()
check("pc same", s.update({"x": 10, "y": 1}), t.render({"x": 10, "y": 1})); print("  calls:", ccalls)
ccalls.clear()
check("pc multiplier add", s.update({"x": 10, "y": 1, "multiplier": 3}), t.render({"x": 10, "y": 1, "multiplier": 3})); print("  calls:", ccalls)
ccalls.clear()
check("pc unrelated", s.update({"x": 10, "y": 1, "multiplier": 3, "z": 0}), t.render({"x": 10, "y": 1, "multiplier": 3, "z": 0})); print("  calls:", ccalls)
ccalls.clear()
check("pc secret add", s.update({"x": 10, "y": 1, "multiplier": 3, "secret": 1}), t.render({"x": 10, "y": 1, "multiplier": 3, "secret": 1})); print("  calls:", ccalls)

# get_all via items
gcalls = []
def dump_all(ctx, _v):
    gcalls.append(1)
    return ",".join(f"{k}={v}" for k, v in sorted(ctx.items()) if k not in ("range", "dict", "lipsum", "cycler", "joiner", "namespace"))
env.filters["dumpall"] = pass_context(dump_all)
t = env.from_string("{{ 1 | dumpall }}")
s = t.render_session({"a": 1})
gcalls.clear()
print("get_all same:", s.update({"a": 1}), gcalls)
gcalls.clear()
print("get_all change:", s.update({"a": 2}), gcalls)

# 2. global function + computed index + missing keys
gcalls2 = []
def add(a, b):
    gcalls2.append((a, b)); return a + b
env.globals["add"] = add
t = env.from_string("{{ add(xs[i], 1) }}")
s = t.render_session({"xs": [10, 20, 30], "i": 0})
gcalls2.clear()
print("same:", s.update({"xs": [10, 20, 30], "i": 0}), gcalls2)
gcalls2.clear()
print("index change:", s.update({"xs": [10, 20, 30], "i": 1}), gcalls2)
gcalls2.clear()
print("unrelated:", s.update({"xs": [10, 20, 30], "i": 1, "q": 9}), gcalls2)
gcalls2.clear()
print("value change:", s.update({"xs": [10, 21, 30], "i": 1}), gcalls2)

# 3. undefined semantics: missing key appears/deleted
u_env = Environment(undefined=StrictUndefined)
t = u_env.from_string("{% if v is defined %}[{{ v }}]{% else %}none{% endif %}")
s = t.render_session({})
print("undef none:", s.render())
print("undef set:", s.update({"v": "x"}))
print("undef gone:", s.update({}))

# 4. error propagation + closed session + other session alive
bad = env.from_string("{{ a | cp }}")
s2 = bad.render_session({"a": 1, "multiplier": 2})
try:
    s2.update({"a": 1})  # multiplier deleted; cp uses get() -> no error; use strict one
except Exception as e:
    print("unexpected", e)
strict_env = Environment(undefined=StrictUndefined)
def need(ctx, x):
    return x + ctx.resolve("m")
strict_env.filters["need"] = pass_context(need)
bt = strict_env.from_string("{{ 1 | need }}")
bs = bt.render_session({"m": 5})
try:
    bs.update({})
    print("ERROR: should have raised")
except Exception as e:
    print("error type:", type(e).__name__)
print("closed after error:", bs.closed)
try:
    bs.render()
except RuntimeError as e:
    print("reuse rejected:", e)
other = bt.render_session({"m": 5})
print("other session alive:", other.render())
bs.close()

# 5. autoescape + Markup
ae = Environment(autoescape=True)
at = ae.from_string("<b>{{ x }}</b>{{ y|safe }}")
s = at.render_session({"x": "<i>", "y": "<i>"})
want = at.render({"x": "<i>", "y": "<i>"})
check("autoescape", s.render(), want)
check("autoescape upd", s.update({"x": "<j>", "y": "<i>"}), at.render({"x": "<j>", "y": "<i>"}))

# 6. includes / extends / macros
loader = DictLoader({
    "inc.html": "[{{ n | cp }}]",
    "base.html": "H{% block b %}base {{ v }}{% endblock %}T",
    "child.html": '{% extends "base.html" %}{% block b %}child {{ v | cp }}{{ super() }}{% endblock %}',
})
le = Environment(loader=loader)
le.filters["cp"] = pass_context(c_price)
it = le.get_template("inc.html")
s = it.render_session({"n": 4, "multiplier": 2})
ccalls.clear()
print("inc same:", s.update({"n": 4, "multiplier": 2}), ccalls)
ccalls.clear()
print("inc mult:", s.update({"n": 4, "multiplier": 5}), ccalls, "want:", it.render({"n": 4, "multiplier": 5}))

ct = le.get_template("child.html")
s = ct.render_session({"v": 3, "multiplier": 2})
check("extends", s.render(), ct.render({"v": 3, "multiplier": 2}))
check("extends upd", s.update({"v": 4, "multiplier": 2}), ct.render({"v": 4, "multiplier": 2}))

# macros with args + closures
mt = env.from_string("{% macro m(a) %}{{ a | cp }}{% endmacro %}{% for x in xs %}{{ m(x) }}{% endfor %}")
s = mt.render_session({"xs": [1, 2], "multiplier": 2})
check("macro", s.render(), mt.render({"xs": [1, 2], "multiplier": 2}))
ccalls.clear()
s.update({"xs": [1, 2], "multiplier": 2})
print("macro reuse calls (expect 0):", ccalls)
ccalls.clear()
s.update({"xs": [1, 3], "multiplier": 2})
print("macro change calls:", ccalls)

# closure over lexical var
def sq(x):
    gcalls2.append(("sq", x)); return x * x
env.globals["sq"] = sq
cl = env.from_string("{% set k = 3 %}{% for x in xs %}{{ sq(x) + k }}{% endfor %}")
s = cl.render_session({"xs": [1, 2]})
gcalls2.clear()
print("closure same:", s.update({"xs": [1, 2]}), gcalls2)
gcalls2.clear()
print("closure change:", s.update({"xs": [2, 2]}), gcalls2)

print()
print("FAILS:", fails)
