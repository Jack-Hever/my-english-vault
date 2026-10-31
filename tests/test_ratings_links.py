"""P2 第一批测试：条目评分列（迁移安全 / 边界 / 重点判定 / 部分更新）+ 关联表级联。

运行：python test_ratings_links.py
说明：全程用 ENPRACTICE_HOME 指向临时目录，绝不触碰用户真实数据
（真实库在 %APPDATA%\\EnPractice\\data.db）。
"""

import os
import sqlite3
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="enpractice_ratings_")
os.environ["ENPRACTICE_HOME"] = TMP  # 必须在导入 app 之前设置

from fastapi.testclient import TestClient  # noqa: E402

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, entries  # noqa: E402
from app.main import app  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def entry_payload(term: str, **extra) -> dict:
    base = {"type": "word", "term": term, "pos": "adj.", "meaning_zh": f"{term} 的释义"}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
print("[1] 迁移安全性：旧库（无评分列）升级后数据不丢、评分留空、可重复执行")
# ---------------------------------------------------------------------------

# 手工造一个「评分列之前的」旧库：entries 表不含 importance / usefulness
legacy_dir = Path(tempfile.mkdtemp(prefix="enpractice_legacy_"))
legacy_db = legacy_dir / "data.db"
old_conn = sqlite3.connect(str(legacy_db))
old_conn.row_factory = sqlite3.Row
old_conn.execute(
    """
    CREATE TABLE entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        type        TEXT NOT NULL CHECK(type IN ('word', 'phrase')),
        term        TEXT NOT NULL,
        pos         TEXT,
        meaning_zh  TEXT NOT NULL,
        usage       TEXT,
        source      TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """
)
old_conn.execute(
    "INSERT INTO entries (type, term, pos, meaning_zh, usage, source, created_at, updated_at)"
    " VALUES ('word', 'legacy', 'adj.', '遗留条目', 'legacy usage', 'old file',"
    " '2025-01-01T00:00:00', '2025-01-01T00:00:00')"
)
old_conn.execute(
    "INSERT INTO entries (type, term, pos, meaning_zh, usage, source, created_at, updated_at)"
    " VALUES ('phrase', 'legacy phrase', NULL, '遗留短语', NULL, NULL,"
    " '2025-01-02T00:00:00', '2025-01-02T00:00:00')"
)
old_conn.commit()
old_conn.close()

# 切到旧库所在目录，让 db_path() 指向它
os.environ["ENPRACTICE_HOME"] = str(legacy_dir)
db.init_db()

legacy_conn = db.connect()
cols = {r[1] for r in legacy_conn.execute("PRAGMA table_info(entries)").fetchall()}
check("迁移后 entries 出现 importance 列", "importance" in cols, sorted(cols))
check("迁移后 entries 出现 usefulness 列", "usefulness" in cols, sorted(cols))

rows = legacy_conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
check("迁移不改行数（2 条仍在）", len(rows) == 2, f"{len(rows)} 行")
check(
    "旧条目的评分是 NULL 而不是 0",
    rows[0]["importance"] is None and rows[0]["usefulness"] is None,
    f"importance={rows[0]['importance']!r} usefulness={rows[0]['usefulness']!r}",
)
check(
    "旧条目的既有字段未被破坏",
    rows[0]["term"] == "legacy"
    and rows[0]["meaning_zh"] == "遗留条目"
    and rows[0]["usage"] == "legacy usage"
    and rows[0]["source"] == "old file",
)
check(
    "旧条目 NULL 字段仍为 NULL（没被填成空串）",
    rows[1]["pos"] is None and rows[1]["usage"] is None,
)

# 幂等：再跑一次 init_db 不应报错、不应把列加成两份
try:
    db.init_db()
    db.init_db()
    idempotent_ok = True
    detail = ""
except Exception as exc:  # pragma: no cover - 只在迁移写错时才会走到
    idempotent_ok = False
    detail = f"{type(exc).__name__}: {exc}"
check("重复执行 init_db 不报错（迁移幂等）", idempotent_ok, detail)

again_cols = [r[1] for r in legacy_conn.execute("PRAGMA table_info(entries)").fetchall()]
check("重复执行后列不重复", len(again_cols) == len(set(again_cols)), again_cols)
check(
    "重复执行后评分列仍各只有一个",
    again_cols.count("importance") == 1 and again_cols.count("usefulness") == 1,
    again_cols,
)
legacy_conn.close()

# 切回主测试目录
os.environ["ENPRACTICE_HOME"] = TMP
db.init_db()
client = TestClient(app)

# ---------------------------------------------------------------------------
print("\n[2] 评分边界与校验")
# ---------------------------------------------------------------------------

boundaries = [
    (0, 0.0, True, "下界 0 合法"),
    (0.5, 0.5, True, "半星 0.5 合法"),
    (5, 5.0, True, "上界 5 合法"),
    (4.5, 4.5, True, "半星 4.5 合法"),
    (5.5, None, False, "超出上界 5.5 应拒绝"),
    (-1, None, False, "低于下界 -1 应拒绝"),
    (0.3, None, False, "非半星 0.3 应拒绝"),
    (4.25, None, False, "非半星 4.25 应拒绝"),
    ("4.5", 4.5, True, "字符串数字 4.5 应被接受"),
    ("abc", None, False, "非数字字符串应拒绝"),
    (None, None, True, "None 视为未评分"),
    ("", None, True, "空串视为未评分"),
]
for raw, expected, should_pass, label in boundaries:
    try:
        got = entries.normalize_rating(raw, "importance")
        ok = should_pass and got == expected
        detail = f"得到 {got!r}"
    except entries.EntryError as exc:
        got = None
        ok = (not should_pass)
        detail = str(exc)
    check(f"normalize_rating {label}", ok, detail)

# 布尔是 int 子类，不能当评分收
try:
    entries.normalize_rating(True, "importance")
    check("normalize_rating 拒绝布尔 True", False, "没有抛错")
except entries.EntryError as exc:
    check("normalize_rating 拒绝布尔 True", True, str(exc))

# NaN / inf 必须被拦（float() 能解析，但语义上不是评分）
for bad, label in ((float("nan"), "NaN"), (float("inf"), "inf")):
    try:
        entries.normalize_rating(bad, "importance")
        check(f"normalize_rating 拒绝 {label}", False, "没有抛错")
    except entries.EntryError as exc:
        check(f"normalize_rating 拒绝 {label}", True, str(exc))

# 错误消息要带上当前值，便于用户定位
try:
    entries.normalize_rating(6, "usefulness")
    msg = ""
except entries.EntryError as exc:
    msg = str(exc)
check("越界消息含字段名与当前值", "有用度" in msg and "6" in msg, msg)

# ---------------------------------------------------------------------------
print("\n[3] 经 API 的评分读写与错误码")
# ---------------------------------------------------------------------------

r = client.post(
    "/api/entries",
    json=entry_payload("abundant", importance=4.5, usefulness=3.0),
)
check("POST 带评分 -> 201", r.status_code == 201, r.text[:160])
created = r.json()
check("创建后 importance 回读正确", created["importance"] == 4.5, created["importance"])
check("创建后 usefulness 回读正确", created["usefulness"] == 3.0, created["usefulness"])
check(
    "创建后 highlight=True（重要性单维达档位即高亮）",
    created["highlight"] is True,
    created["highlight"],
)
abundant_id = created["id"]

r = client.post("/api/entries", json=entry_payload("bare"))
check("POST 不带评分 -> 201", r.status_code == 201, r.text[:160])
bare = r.json()
check("未评分 importance 为 null", bare["importance"] is None, bare["importance"])
check("未评分 usefulness 为 null", bare["usefulness"] is None, bare["usefulness"])
check("未评分不判为重点", bare["highlight"] is False, bare["highlight"])
bare_id = bare["id"]

r = client.post("/api/entries", json=entry_payload("over5", importance=5.5))
check("POST importance=5.5 -> 400", r.status_code == 400, r.text[:160])
check("400 响应体带可读中文原因", "重要度" in r.json().get("detail", ""), r.json().get("detail"))

r = client.post("/api/entries", json=entry_payload("halfstar", importance=0.25))
check("POST importance=0.25（非半星）-> 400", r.status_code == 400, r.text[:160])

# ---------------------------------------------------------------------------
print("\n[4] 部分更新：不传评分不动，显式 null 才清除")
# ---------------------------------------------------------------------------

r = client.put(
    f"/api/entries/{abundant_id}",
    json={"type": "word", "term": "abundant", "meaning_zh": "改过的释义"},
)
check("PUT 只改释义 -> 200", r.status_code == 200, r.text[:160])
updated = r.json()
check("释义已更新", updated["meaning_zh"] == "改过的释义", updated["meaning_zh"])
check(
    "未提及评分时 importance 保持不变（关键：不能被清零）",
    updated["importance"] == 4.5,
    updated["importance"],
)
check(
    "未提及评分时 usefulness 保持不变",
    updated["usefulness"] == 3.0,
    updated["usefulness"],
)
check("highlight 仍为 True", updated["highlight"] is True, updated["highlight"])
check(
    "未提及的文本字段仍按全量替换清空（与导入 overwrite 一致）",
    updated["usage"] == "" and updated["source"] == "",
    f"usage={updated['usage']!r} source={updated['source']!r}",
)

r = client.put(
    f"/api/entries/{abundant_id}",
    json={"type": "word", "term": "abundant", "meaning_zh": "改过的释义", "importance": None},
)
check("PUT 显式 importance=null -> 200", r.status_code == 200, r.text[:160])
cleared = r.json()
check("显式 null 清除了 importance", cleared["importance"] is None, cleared["importance"])
check("另一个评分未被牵连", cleared["usefulness"] == 3.0, cleared["usefulness"])
check(
    "清除后不再判为重点（剩下的 3.0 未达档位）",
    cleared["highlight"] is False,
    cleared["highlight"],
)

r = client.put(
    f"/api/entries/{bare_id}",
    json={"type": "word", "term": "bare", "meaning_zh": "bare 的释义", "usefulness": 4.5},
)
check("PUT 只给一个维度 -> 200", r.status_code == 200, r.text[:160])
one_dim = r.json()
check("给出的维度生效", one_dim["usefulness"] == 4.5, one_dim["usefulness"])
check("另一个维度仍是 null", one_dim["importance"] is None, one_dim["importance"])
check(
    "单维达标即判为重点（OR 语义：需求是「单个属性获得高分即可参与高亮」）",
    one_dim["highlight"] is True,
    one_dim["highlight"],
)

# ---------------------------------------------------------------------------
print("\n[5] 导入 / 导出：评分随文件走，旧版文件覆盖不清分")
# ---------------------------------------------------------------------------

r = client.post(
    "/api/entries",
    json=entry_payload("resilient", importance=5.0, usefulness=4.5),
)
target_id = r.json()["id"]

r = client.get("/api/entries/export", params={"q": "resilient"})
check("GET /api/entries/export -> 200", r.status_code == 200, r.text[:160])
export_payload = r.json()
item = export_payload["entries"][0]
check("导出含 importance", item.get("importance") == 5.0, item.get("importance"))
check("导出含 usefulness", item.get("usefulness") == 4.5, item.get("usefulness"))
check("format_version 保持 1（可缺省字段，非破坏性变更）",
      export_payload["format_version"] == 1, export_payload["format_version"])

# 旧版导出文件（没有评分键）覆盖导入 —— 不能把已打的分清掉
legacy_file = {
    "app": "EnPractice",
    "format_version": 1,
    "entries": [
        {
            "type": "word",
            "term": "resilient",
            "pos": "adj.",
            "meaning_zh": "旧版文件里的释义",
            "usage": "",
            "source": "",
            "created_at": item["created_at"],
        }
    ],
}
r = client.post("/api/entries/import", json=legacy_file, params={"on_duplicate": "overwrite"})
check("旧版文件 overwrite 导入 -> 200", r.status_code == 200, r.text[:200])
stats = r.json()
check("命中 1 条并更新", stats["updated"] == 1, stats)

r = client.get(f"/api/entries/{target_id}")
after_import = r.json()
check("释义按 overwrite 被替换", after_import["meaning_zh"] == "旧版文件里的释义", after_import["meaning_zh"])
check(
    "旧版文件没有评分键 → 评分保持不变（不能被静默清零）",
    after_import["importance"] == 5.0 and after_import["usefulness"] == 4.5,
    f"{after_import['importance']} / {after_import['usefulness']}",
)

# 导出→再导入，评分应当原样往返
r = client.post("/api/entries/import", json=export_payload, params={"on_duplicate": "overwrite"})
check("导出结果回导 -> 200", r.status_code == 200, r.text[:200])
r = client.get(f"/api/entries/{target_id}")
roundtrip = r.json()
check(
    "导出→导入评分往返无损",
    roundtrip["importance"] == 5.0 and roundtrip["usefulness"] == 4.5,
    f"{roundtrip['importance']} / {roundtrip['usefulness']}",
)

# ---------------------------------------------------------------------------
print("\n[6] 排序白名单不含评分列（不应 400，应静默回退）")
# ---------------------------------------------------------------------------

r = client.get("/api/entries", params={"sort": "importance", "order": "desc"})
check("sort=importance 不报错 -> 200", r.status_code == 200, r.text[:160])
check("评分列未进入排序白名单", "importance" not in entries.SORT_COLUMNS, sorted(entries.SORT_COLUMNS))

# ---------------------------------------------------------------------------
print("\n[7] 关联表：外键级联与自连接取同伴")
# ---------------------------------------------------------------------------

conn = db.connect()
now = entries.now_iso()
cur = conn.execute(
    "INSERT INTO entry_links (relation, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
    ("synonym", "近义组", now, now),
)
link_id = int(cur.lastrowid)
conn.executemany(
    "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)",
    [(link_id, abundant_id), (link_id, bare_id), (link_id, target_id)],
)
conn.commit()

peers = conn.execute(
    """
    SELECT e.term FROM entry_link_items i1
    JOIN entry_link_items i2 ON i2.link_id = i1.link_id
    JOIN entries e ON e.id = i2.entry_id
    WHERE i1.entry_id = ? AND i2.entry_id <> ?
    ORDER BY e.id
    """,
    (abundant_id, abundant_id),
).fetchall()
peer_terms = [row["term"] for row in peers]
check(
    "自连接能取到同组同伴（无需存反向行）",
    sorted(peer_terms) == sorted(["bare", "resilient"]),
    peer_terms,
)

# 删条目：只清掉该成员的行，组还在，其他成员不受影响
client.delete(f"/api/entries/{bare_id}")
left = conn.execute(
    "SELECT entry_id FROM entry_link_items WHERE link_id = ? ORDER BY entry_id", (link_id,)
).fetchall()
left_ids = [int(row["entry_id"]) for row in left]
check(
    "删条目后其关联行被 CASCADE 清掉",
    bare_id not in left_ids,
    left_ids,
)
check("删条目后其他成员仍在", abundant_id in left_ids and target_id in left_ids, left_ids)
check(
    "删条目后组本身保留（P2 业务层需自行清理空组）",
    conn.execute("SELECT COUNT(*) FROM entry_links WHERE id = ?", (link_id,)).fetchone()[0] == 1,
)

# 删组：成员行全部清掉，条目本身不受影响
conn.execute("DELETE FROM entry_links WHERE id = ?", (link_id,))
conn.commit()
remaining = conn.execute(
    "SELECT COUNT(*) FROM entry_link_items WHERE link_id = ?", (link_id,)
).fetchone()[0]
check("删组后成员行被 CASCADE 全清", remaining == 0, remaining)
check(
    "删组不影响条目本体",
    conn.execute("SELECT COUNT(*) FROM entries WHERE id = ?", (abundant_id,)).fetchone()[0] == 1,
)

# 外键真的生效（不是摆设）：插入不存在的 entry_id 必须失败
fk_error = ""
try:
    cur = conn.execute(
        "INSERT INTO entry_links (relation, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("custom", "", now, now),
    )
    tmp_link = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)", (tmp_link, 999999)
    )
    conn.commit()
except sqlite3.IntegrityError as exc:
    fk_error = str(exc)
    conn.rollback()
check("关联行指向不存在的条目时外键报错", bool(fk_error), fk_error)

# 组内重复成员被主键挡住
dup_error = ""
try:
    cur = conn.execute(
        "INSERT INTO entry_links (relation, note, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("custom", "", now, now),
    )
    tmp_link2 = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)",
        (tmp_link2, abundant_id),
    )
    conn.execute(
        "INSERT INTO entry_link_items (link_id, entry_id) VALUES (?, ?)",
        (tmp_link2, abundant_id),
    )
    conn.commit()
except sqlite3.IntegrityError as exc:
    dup_error = str(exc)
    conn.rollback()
check("同一组内重复成员被主键挡住", bool(dup_error), dup_error)

# 反查索引存在（否则自连接会退化成全表扫描）
idx = conn.execute(
    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_link_items_entry'"
).fetchone()
check("idx_link_items_entry 索引已建", idx is not None, idx)
conn.close()

# ---------------------------------------------------------------------------
print("\n[8] is_highlight / DEFAULT_RATING 口径（OR 语义）")
# ---------------------------------------------------------------------------

cases = [
    (4.0, 4.0, True, "两维都刚到档位 → 高亮"),
    (4.5, 5.0, True, "两维都高 → 高亮"),
    (5.0, 3.5, True, "仅重要性达标 → 高亮（OR）"),
    (3.5, 5.0, True, "仅实用性达标 → 高亮（OR）"),
    (4.0, None, True, "重要性达标 + 实用性未评 → 仍高亮"),
    (None, 4.5, True, "实用性达标 + 重要性未评 → 仍高亮"),
    (3.5, 3.5, False, "两维都低 → 不高亮"),
    (None, None, False, "都未评分 → 不高亮"),
    (0.0, 0.0, False, "两维都是 0 → 不高亮"),
]
for imp, use, expected, label in cases:
    got = entries.is_highlight({"importance": imp, "usefulness": use})
    check(f"is_highlight {label}", got is expected, f"({imp}, {use}) -> {got}")

check(
    "is_highlight 收的是条目字典（契约：entry: dict）",
    entries.is_highlight({"importance": 5.0}) is True,
    entries.is_highlight({"importance": 5.0}),
)

check("HIGHLIGHT_THRESHOLD = 4.0", entries.HIGHLIGHT_THRESHOLD == 4.0, entries.HIGHLIGHT_THRESHOLD)
check(
    "评分区间与步进常量正确",
    (entries.RATING_MIN, entries.RATING_MAX, entries.RATING_STEP) == (0.0, 5.0, 0.5),
    f"{entries.RATING_MIN}/{entries.RATING_MAX}/{entries.RATING_STEP}",
)
check("未评分兜底取中位数 3.0", entries.DEFAULT_RATING == 3.0, entries.DEFAULT_RATING)

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print("\n" + "=" * 60)
print(f"PASS={passed}  FAIL={failed}  TOTAL={len(results)}")
if failed:
    print("\n失败项：")
    for name, ok in results:
        if not ok:
            print("  - " + name)
raise SystemExit(1 if failed else 0)
