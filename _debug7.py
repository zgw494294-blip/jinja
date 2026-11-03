import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment, DictLoader

calls = []
def mk(name):
    def f(value, *a):
        calls.append((name, value)); return f"{name}:{value}"
    return f

env = Environment()
env.filters["f1"] = mk("f1")
env.filters["f2"] = mk("f2")

print("=== lexical set + filter block")
t = env.from_string("{% set total = a + b %}{{ total | f1 }}{% filter f2 %}x{{ a }}y{% endfilter %}")
s = t.render_session({"a": 1, "b": 2})
print("render1:", s.render())
calls.clear()
print("same:", s.update({"a": 1, "b": 2}), calls)
for k, v in s._runtime.memo.items():
    print("  KEY", k, "args", v[0][0])

print("=== extends/import")
loader = DictLoader({
    "lib.html": "{% macro greet(name) %}hi {{ name | f1 }}{% endmacro %}",
    "base.html": "B{% block content %}base{% endblock %}E",
    "child.html": '{% from "lib.html" import greet %}{% extends "base.html" %}'
                  "{% block content %}{{ greet(v) }}|{{ super() }}{% endblock %}",
})
env2 = Environment(loader=loader)
env2.filters["f1"] = mk("f1")
t = env2.get_template("child.html")
s = t.render_session({"v": "a"})
calls.clear()
print("v=b:", s.update({"v": "b"}), calls)
calls.clear()
print("v=b again:", s.update({"v": "b"}), calls)
