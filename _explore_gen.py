import sys
sys.path.insert(0, r"c:\SWE\data\tasks\GO-0097\runs\RUN-20261103060347-476079d3\GO-0097-workspace\src")
from jinja2 import Environment

env = Environment()
src = (
    "{% if x > 1 %}{{ f(x) | myf(y) }}{% endif %}"
    "{% for item in items %}{{ item | upper }}:{{ loop.index }}{% endfor %}"
    '{{ items | map("capitalize") | select("odd") | list }}'
)
print(env.compile(src, raw=True))
print("=" * 60)
src2 = "{% macro m(a) %}[{{ a | upper }}]{% endmacro %}{{ m(x) }}{% set z = x + 1 %}{{ z }}"
print(env.compile(src2, raw=True))
