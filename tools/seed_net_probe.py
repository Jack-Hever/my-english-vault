"""为「网状结构」探针准备数据：克服 → overcome / conquer / surmount，surmount → insurmountable。

只走 HTTP，不碰库；服务必须已在 8791 端口跑着（临时 ENPRACTICE_HOME）。
输出 ids 到 /tmp/net_probe_ids.json 供 JS 探针读取。
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8791"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else None


def add_entry(term, meaning):
    return call("POST", "/api/entries",
                {"type": "word", "term": term, "meaning_zh": meaning})["id"]


ids = {}
ids["overcome"] = add_entry("overcome", "克服；战胜")
ids["conquer"] = add_entry("conquer", "征服；攻克")
ids["surmount"] = add_entry("surmount", "克服（困难）")
ids["insurmountable"] = add_entry("insurmountable", "无法克服的")

root = call("POST", "/api/nodes/concept", {"label": "克服"})["node_id"]
ids["root"] = root

node_of = {}
for key in ("overcome", "conquer", "surmount"):
    node = call("POST", f"/api/nodes/{root}/children",
                {"child": {"kind": "entry", "entry_id": ids[key]}})
    node_of[key] = node["node_id"]

ids["surmount_node"] = node_of["surmount"]

tree = call("GET", f"/api/nodes/{root}/tree")
print(json.dumps(ids, ensure_ascii=False))
print("树节点数（应为 4）:", tree["node_count"])
print("links（此时应为空）:", tree["links"])

with open("./net_probe_ids.json", "w", encoding="utf-8") as fh:
    json.dump(ids, fh, ensure_ascii=False)
