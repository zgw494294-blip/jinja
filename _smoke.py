import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")

from jinja2 import Environment, DictLoader

calls = []

def upperish(s):
    calls.append(("upperish", s))
    return str(s).upper()

def price_format(n):
    calls.append(("price", n))
    return f"${n:.2f}"

env = Environment()
env.filters["upperish"] = upperish
env.filters["price"] = price_format

t = env.from_string(
    "{% for row in rows %}{{ row.name | upperish }}:{{ row.price | price }};{% endfor %}"
)
data = {"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 2}]}

normal = t.render(data)
calls.clear()
s = t.render_session(data)
print("initial:", s.render(), "==", normal, "->", s.render() == normal)
print("initial calls:", len(calls))

calls.clear()
out = s.update({"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 2}]})
print("same update calls (expect 0):", calls, out)

calls.clear()
out = s.update({"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 3}]})
print("one changed (expect 1 price call):", calls)
print("out:", out)

calls.clear()
out = s.update({"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 3}], "other": "x"})
print("unrelated add (expect 0):", calls, out == t.render({"rows": [{"name": "a", "price": 1}, {"name": "b", "price": 3}], "other": "x"}))

# pipeline
pcalls = []
def shout(x):
    pcalls.append(x)
    return str(x) + "!"

env2 = Environment()
env2.filters["shout"] = shout
t2 = env2.from_string('{{ items | select("odd") | map("shout") | list }}')
d2 = {"items": [1, 2, 3]}
s2 = t2.render_session(d2)
pcalls.clear()
print(s2.update({"items": [1, 2, 3]}), "pipeline same calls (expect 0):", pcalls)
pcalls.clear()
print(s2.update({"items": [1, 2, 4]}), "pipeline changed:", pcalls)
print("normal check:", t2.render({"items": [1, 2, 4]}))

# branches
bcalls = []
def bf(x):
    bcalls.append(x)
    return x * 10

env3 = Environment()
env3.filters["bf"] = bf
t3 = env3.from_string("{% if flag %}{{ x | bf }}{% else %}none{% endif %}|{% for i in xs %}{{ i | bf }},{% endfor %}")
s3 = t3.render_session({"flag": True, "x": 2, "xs": [1, 2]})
bcalls.clear()
print("branch same:", s3.update({"flag": True, "x": 2, "xs": [1, 2]}), bcalls)
bcalls.clear()
print("branch switch:", s3.update({"flag": False, "x": 2, "xs": [1, 2]}), bcalls)
bcalls.clear()
print("branch back:", s3.update({"flag": True, "x": 5, "xs": [1, 2]}), bcalls)
