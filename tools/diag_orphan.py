# -*- coding: utf-8 -*-
"""诊断：裸 SQL DELETE 删父词条后，nodes / node_edges 的真实数据形态。

目的：反向验证② 期望「裸删会产生孤儿根」，但判据查不到。
先看清坏实现之后数据库里**真的**长什么样，再决定判据口径。
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="enp-orphan-diag-"))
os.environ["ENPRACTICE_HOME"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, entries  # noqa: E402

conn = db.connect()
db.init_db()
parent = entries.create_entry(conn, {
    "type": "word", "term": "parentword", "pos": "n.",
    "meaning_zh": "父词条", "usage": "u", "source": "seed",
})
child = entries.create_entry(conn, {
    "type": "word", "term": "childword", "pos": "n.",
    "meaning_zh": "子词条", "usage": "u", "source": "seed",
})
pid, cid = parent["id"], child["id"]
pnode = conn.execute("SELECT * FROM nodes WHERE entry_id = ?", (pid,)).fetchone()
cnode = conn.execute("SELECT * FROM nodes WHERE entry_id = ?", (cid,)).fetchone()
print("父节点行:", dict(pnode))
print("子节点行:", dict(cnode))

conn.execute(
    "INSERT OR IGNORE INTO node_edges (parent_id, child_id, position, created_at) "
    "VALUES (?, ?, 0, datetime('now'))", (pnode["id"], cnode["id"]))
conn.commit()

print("\n--- 造边之后 ---")
for r in conn.execute("SELECT * FROM node_edges").fetchall():
    print("  edge:", dict(r))

# 坏实现：裸 SQL DELETE
conn.execute("DELETE FROM entries WHERE id = ?", (pid,))
conn.commit()

print("\n--- 裸删之后 nodes 全部行 ---")
cols = [d[1] for d in conn.execute("PRAGMA table_info(nodes)").fetchall()]
print("  nodes 列:", cols)
for r in conn.execute("SELECT * FROM nodes").fetchall():
    print("  node:", {k: r[k] for k in cols})

print("\n--- 裸删之后 node_edges 全部行 ---")
for r in conn.execute("SELECT * FROM node_edges").fetchall():
    print("  edge:", dict(r))

print("\n--- 按现判据查孤儿 ---")
rows = conn.execute(
    """SELECT n.id, n.label, n.kind FROM nodes n
       WHERE n.entry_id IS NULL AND n.tombstone_label IS NULL"""
).fetchall()
print("  命中节点数:", len(rows))
for r in rows:
    has = conn.execute(
        "SELECT 1 FROM node_edges WHERE child_id = ? LIMIT 1", (r["id"],)).fetchone()
    print("   id=%s label=%s kind=%s 有子项=%s" % (r["id"], r["label"], r["kind"], bool(has)))

conn.close()
print("\nDB:", TMP / "data.db")
