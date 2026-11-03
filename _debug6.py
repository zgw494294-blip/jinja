import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment, pass_context

calls = []
def cf(context, value):
    factor = context.get("factor", 1)
    calls.append(("cf", value, factor))
    return f"cf:{value * factor}"

env = Environment()
env.filters["cf"] = pass_context(cf)
t = env.from_string("{{ a | cf }}")
s = t.render_session({"a": 10})
calls.clear()
s.update({"a": 10})
print("same:", calls)
calls.clear()
s.update({"a": 10, "factor": 3})
print("factor add:", calls)
calls.clear()
s.update({"a": 10, "factor": 3, "unused": 1})
print("unused add:", calls)
print("memo keys:")
for k, v in s._runtime.memo.items():
    print("  ", k, "->", v[0])
