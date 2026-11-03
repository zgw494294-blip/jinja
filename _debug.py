import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")

from jinja2 import Environment
from jinja2 import session as S

calls = []
def upperish(s):
    calls.append(s)
    return str(s).upper()

env = Environment()
env.filters["upperish"] = upperish

t = env.from_string("{% for row in rows %}{{ row.name | upperish }};{% endfor %}")
data = {"rows": [{"name": "a"}, {"name": "b"}]}

orig_invoke = S.SessionRuntime.invoke
def traced(self, frame, func, args, kwargs, eager_iterator=False, context=None):
    key = self.site_key(frame)
    fp_args = S._fingerprint(args, set())
    fp_kw = S._fingerprint_kwargs(kwargs)
    entry = self.memo.get(key)
    if entry is None:
        reason = "no-entry"
    elif entry[0][:2] != (fp_args, fp_kw):
        reason = f"args-changed {entry[0][0]} != {fp_args}"
    elif not self.reads_valid(entry[0][2], context):
        reason = "reads-changed"
    else:
        reason = "HIT"
    if "upperish" in repr(getattr(func, "func", func)):
        print("upperish call:", reason, key)
    return orig_invoke(self, frame, func, args, kwargs, eager_iterator, context)
S.SessionRuntime.invoke = traced

s = t.render_session(data)
calls.clear()
print("--- same")
s.update({"rows": [{"name": "a"}, {"name": "b"}]})
calls.clear()
print("--- +other")
s.update({"rows": [{"name": "a"}, {"name": "b"}], "other": "x"})
