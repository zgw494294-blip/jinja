import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment
env = Environment(autoescape=True)
src = "<a>{{ x }}</a>"
print(env.compile(src, raw=True))
print("---")
t = env.from_string(src)
r = t.render({"x": "<"})
print(type(r), repr(r))
s = t.render_session({"x": "<"})
print(type(s.render()), repr(s.render()))
