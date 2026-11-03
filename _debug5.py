import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment, DictLoader, pass_context
from jinja2 import session as S

calls = []
def f1(value, *args):
    calls.append(("f1", value)); return f"f1:{value}"
def cf(context, value):
    factor = context.get("factor", 1)
    calls.append(("cf", value)); return f"cf:{value * factor}"

env = Environment()
env.filters["f1"] = f1
env.filters["cf"] = pass_context(cf)

orig = S.SessionRuntime.invoke
def traced(self, frame, func, args, kwargs, eager_iterator=False, context=None, walk_to_template=True):
    key = self.site_key(frame, walk_to_template=walk_to_template)
    fa = S._fingerprint(args, set()); fk = S._fingerprint_kwargs(kwargs)
    e = self.memo.get(key)
    hit = e is not None and e[0][:2] == (fa, fk) and self.reads_valid(e[0][2], context)
    fname = getattr(getattr(func, "func", None), "__name__", repr(func))
    rv = orig(self, frame, func, args, kwargs, eager_iterator, context, walk_to_template)
    print("   ", "HIT " if hit else "MISS", key, "post-in-memo:", key in self.memo)
    return rv
S.SessionRuntime.invoke = traced

print("=== pipeline")
t = env.from_string('{{ xs | select("big") | map("f1") | list }}')
env.tests["big"] = lambda v: v >= 10
s = t.render_session({"xs": [1, 10, 20]})
rt = s._runtime
map_keys = [k for k in rt.memo if k[0] == "nested"]
print("MEMO total keys:", len(rt.memo))
print("MEMO nested keys after render1:", len(map_keys))
for k in map_keys:
    print("  ", k)
print("same runtime as env?", s._environment.session_runtime is rt)

print("same runtime as env?", s._environment.session_runtime is rt)
print("render1:", s.render())
calls.clear()
print("update [2,11,20]:")
print("   out:", s.update({"xs": [2, 11, 20]}), "calls:", calls)

print("=== cf unused")
t = env.from_string("{{ a | cf }}")
s = t.render_session({"a": 10})
calls.clear()
print("add factor")
s.update({"a": 10, "factor": 3}); calls.clear()
print("add unused:")
s.update({"a": 10, "factor": 3, "unused": 1})
print("calls:", calls)

print("=== lexical set")
t = env.from_string("{% set total = a + b %}{{ total | f1 }}{% filter f1 %}x{{ a }}y{% endfilter %}")
s = t.render_session({"a": 1, "b": 2})
calls.clear()
print("same:")
s.update({"a": 1, "b": 2})
print("calls:", calls)

print("=== extends")
loader = DictLoader({
    "lib.html": "{% macro greet(name) %}hi {{ name | f1 }}{% endmacro %}",
    "base.html": "B{% block content %}base{% endblock %}E",
    "child.html": '{% from "lib.html" import greet %}{% extends "base.html" %}'
                  "{% block content %}{{ greet(v) }}|{{ super() }}{% endblock %}",
})
env2 = Environment(loader=loader)
env2.filters["f1"] = f1
t = env2.get_template("child.html")
s = t.render_session({"v": "a"})
calls.clear()
print("v=b:", repr(s.update({"v": "b"})), "calls:", calls)
