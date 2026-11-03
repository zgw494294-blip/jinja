import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment, pass_context
import jinja2.session as S


class Recorder:
    def __init__(self):
        self.calls = []
    def reset(self):
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


env, rec = make_env()
t = env.from_string(
    "{% set total = a + b %}{{ total | f1 }}"
    "{% filter f2 %}x{{ a }}y{% endfilter %}"
)
data = {"a": 1, "b": 2}
s = t.render_session(data)
print("session runtime id:", id(s._runtime), "env id:", id(s._environment))
print("env.session_runtime id:", id(s._environment.session_runtime))
print("render1:", s.render(), "==", t.render(data))
rt = s._runtime
for k, v in list(rt.memo.items()):
    print("STORED", k, v[0])
rec.reset()
print("MEMO BEFORE UPDATE:", len(rt.memo), rt is s._environment.session_runtime, s._environment is t.environment, s._template)
orig_site = S.SessionRuntime.site_key
def logged_site(self, frame, walk_to_template=True):
    k = orig_site(self, frame, walk_to_template)
    print("SITE", k, "HAS ENTRY:", k in self.memo, "memo id:", id(self.memo))
    return k
S.SessionRuntime.site_key = logged_site
out = s.update({"a": 1, "b": 2})
print("same:", out, t.render({"a": 1, "b": 2}), rec.calls)
# recompute fingerprints as invoke would
for k, v in list(rt.memo.items()):
    print("RESTORED", k, v[0])
