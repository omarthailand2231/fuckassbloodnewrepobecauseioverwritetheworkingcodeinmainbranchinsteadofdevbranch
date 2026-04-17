import json

c = None
if not isinstance(c, str):
    c = json.dumps(c, ensure_ascii=False)[:350]

print(f"c is: {repr(c)}")
