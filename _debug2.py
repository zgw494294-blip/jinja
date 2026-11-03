import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")

from jinja2 import Environment
from jinja2 import session as S

calls = []
def upperish(s):
    calls.append(("u", s)); return str(s).upper()
def price(n):
    calls.append(("p", n)); return f"${n:.2f}"

env = Environment()
env.filters["upperish"] = upperish
env.filters["price"] = price

t = env.from_string("{% for row in rows %}{{ row.name | upperish }}:{{ row.price | price }};{% endfor %}")
d1 = {"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 2}]}
d2 = {"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 3}]}
d3 = {"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 3}], "other": "x"}

orig = S.SessionRuntime.invoke
def traced(self, frame, func, args, kwargs, eager_iterator=False, context=None):
    key = self.site_key(frame)
    fa = S._fingerprint(args, set()); fk = S._fingerprint_kwargs(kwargs)
    e = self.memo.get(key)
    reason = "HIT" if e is not None and e[0][:2] == (fa, fk) and self.reads_valid(e[0][2], context) else "MISS"
    if reason == "MISS":
        why = "new" if e is None else ("args" if e[0][:2] != (fa, fk) else "reads")
        print("  MISS", why, key, "stored:", e[0][:2] if e else None, "cur:", fa)
    return orig(self, frame, func, args, kwargs, eager_iterator, context)
S.SessionRuntime.invoke = traced

s = t.render_session(d1)
calls.clear(); print("-- d2")
s.update(d2); print(calls)
calls.clear(); print("-- d3")
s.update(d3); print(calls)
