#!/usr/bin/env python3
"""Smoke-test the OpenAI-compatible endpoint: chat template, reasoning split, tool calling."""
import json
import sys
import time

import httpx

URL = "http://127.0.0.1:8181/v1/chat/completions"
TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["c", "f"]}},
                    "required": ["city"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
]


def chat(messages, tools=None, **kw):
    body = {"model": "local", "messages": messages, "max_tokens": 4096, **kw}
    if tools:
        body["tools"] = tools
    t = time.time()
    r = httpx.post(URL, json=body, timeout=600).json()
    return r, time.time() - t


ok = True
r, dt = chat([{"role": "user", "content": "In one sentence: what is a mixture-of-experts model?"}])
m = r["choices"][0]["message"]
print(f"[plain] {dt:.1f}s  reasoning={len(m.get('reasoning_content') or '')} chars  content={m['content'][:120]!r}")
ok &= bool(m["content"]) and "<think>" not in m["content"]

r, dt = chat([{"role": "user", "content": "What's the weather in Lisbon in celsius?"}], TOOLS)
m = r["choices"][0]["message"]
calls = m.get("tool_calls") or []
print(f"[tool] {dt:.1f}s  finish={r['choices'][0]['finish_reason']}  calls={[(c['function']['name'], c['function']['arguments']) for c in calls]}")
ok &= len(calls) == 1 and calls[0]["function"]["name"] == "get_weather" and "lisbon" in calls[0]["function"]["arguments"].lower()

r, dt = chat([{"role": "user", "content": "Get the weather in Paris AND in Tokyo. Call the tool for both cities in this one turn."}], TOOLS)
calls = r["choices"][0]["message"].get("tool_calls") or []
print(f"[parallel] {dt:.1f}s  calls={[(c['function']['name'], json.loads(c['function']['arguments']).get('city')) for c in calls]}")
ok &= len(calls) >= 2

# multi-turn: feed a tool result back
msgs = [{"role": "user", "content": "What's the weather in Oslo?"}]
r, _ = chat(msgs, TOOLS)
m = r["choices"][0]["message"]
if m.get("tool_calls"):
    msgs += [m, {"role": "tool", "tool_call_id": m["tool_calls"][0]["id"], "content": json.dumps({"temp_c": -3, "sky": "snow"})}]
    r, dt = chat(msgs, TOOLS)
    final = r["choices"][0]["message"]["content"] or ""
    print(f"[tool-result] {dt:.1f}s  final={final[:120]!r}")
    ok &= "-3" in final or "3" in final
else:
    ok = False
print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
