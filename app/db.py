"""SQLite 连接与建表。"""

from __future__ import annotations

import sqlite3

from .config import db_path

SCHEMA = [
    # 词汇 / 短语条目
    # importance / usefulness：用户对条目的两维评分（0~5，半星步进）。
    # 刻意允许 NULL 表示「还没评过」，不用 0 顶替——0 是用户明确给出的负面评价
    # （"这条没用"），与"没看过这条"在加权和统计里必须分得开。
    # 取值范围与步进只由 entries.RATING_* 常量在业务层把关，SQL 侧不写 CHECK：
    # SQLite 无法修改既有约束，一旦要放宽取值就得整表重建。
    """
    CREATE TABLE IF NOT EXISTS entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        type        TEXT NOT NULL CHECK(type IN ('word', 'phrase')),
        term        TEXT NOT NULL,
        pos         TEXT,
        meaning_zh  TEXT NOT NULL,
        usage       TEXT,
        source      TEXT,
        importance  REAL,
        usefulness  REAL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entries_term ON entries(term)",
    "CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at)",
    # 复习状态（SM-2 简化版）
    """
    CREATE TABLE IF NOT EXISTS review_state (
        entry_id         INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
        easiness         REAL NOT NULL DEFAULT 2.5,
        interval_days    INTEGER NOT NULL DEFAULT 0,
        reps             INTEGER NOT NULL DEFAULT 0,
        lapses           INTEGER NOT NULL DEFAULT 0,
        due_at           TEXT NOT NULL,
        last_reviewed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        reviewed_at TEXT NOT NULL,
        result      TEXT NOT NULL CHECK(result IN ('remembered', 'forgot'))
    )
    """,
    # 翻译练习素材
    # origin 取值：paste 手动粘贴 / url 网络抓取（旧）/ library 来自外刊库 / web 来自网络外刊
    """
    CREATE TABLE IF NOT EXISTS passages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT,
        source_url      TEXT,
        origin          TEXT NOT NULL CHECK(origin IN ('paste', 'url', 'library', 'web')),
        text_en         TEXT NOT NULL,
        text_zh         TEXT,
        focus_entry_ids TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    # 练习记录与 AI 点评
    """
    CREATE TABLE IF NOT EXISTS practices (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        passage_id   INTEGER REFERENCES passages(id) ON DELETE SET NULL,
        user_text    TEXT NOT NULL,
        ai_feedback  TEXT,
        score_grammar  REAL,
        score_fluency  REAL,
        score_fidelity REAL,
        score_total    REAL,
        created_at   TEXT NOT NULL
    )
    """,
    # 外刊文章库（本地素材来源）。注意：网络抓取的外刊内容按用户要求**不入库**，
    # 只在当次练习中使用并附带真实链接，故这里只保存手动粘贴/本地导入的文章。
    # content_zh：用户附带的中文译文（选填）。与 content 句数一致时逐句对齐到
    # article_sentences.text_zh，供翻译练习直接用作参考译文；不一致时仅存文章级。
    # 标签（多对多）见下方 tags / article_tags 两表；旧的 topic 单列已停用，
    # 存量值由 _migrate_article_tags 迁成标签关联，新库不再建该列。
    """
    CREATE TABLE IF NOT EXISTS articles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        source_name TEXT,
        url         TEXT,
        content     TEXT NOT NULL,
        content_zh  TEXT NOT NULL DEFAULT '',
        -- 其他信息：作者信息/文章背景等文章级元数据（用户自由填写）。
        -- 刻意存文章级而非句子级——改正文触发重切句时不该冲掉这类备注。
        extra_info  TEXT NOT NULL DEFAULT '',
        -- 中文标题（选填）。原标题是外刊英文原文，用户额外给一个中文说法。
        -- 可空无默认：NULL = 用户没填（列表/详情按空串兜底），与「填了空串」
        -- 语义分开。旧库由 _migrate_article_title_zh 幂等补列。
        title_zh    TEXT,
        added_at    TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # 文章句子（按句切分，用于「按词检索选句」）。
    # 存句子而非整篇，是为了让练习能精确取到含目标词的那一句。
    # text_zh：该句对应的中文译文（仅当用户附带的译文与英文句数一致时才有值）。
    """
    CREATE TABLE IF NOT EXISTS article_sentences (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        seq        INTEGER NOT NULL,
        text       TEXT NOT NULL,
        text_zh    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sentences_article ON article_sentences(article_id)",
    # 文章标签（多对多）：一篇文章可打多个标签；标签名全局唯一，用户随手输入的
    # 新标签自动创建并沉淀，供下次复用与按标签多选筛选。
    """
    CREATE TABLE IF NOT EXISTS tags (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS article_tags (
        article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (article_id, tag_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id)",
    # 条目关联（P2 关联学习）：把若干条目打包成一个「关联组」，组内互为同伴。
    # 为什么是「组」而不是「两两互为关联」：
    #   - 加一条关联只插 1 行；两两模型要插 C(n,2) 行，且删成员时难保持一致；
    #   - 「查某条目的所有同伴」= 先按 entry_id 查到所属 link_id，再按 link_id 取回同组
    #     其余成员，一次自连接即可，不必存反向行（存反向行只会带来不一致风险）。
    # relation 是语义标签（synonym 近义 / antonym 反义 / collocation 搭配 /
    # confusable 易混 / custom 用户自定义）。同样不写 SQL CHECK：取值会随用户自定义
    # 扩展，写死就得重建表；合法性交给业务层白名单。
    """
    CREATE TABLE IF NOT EXISTS entry_links (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        relation   TEXT NOT NULL DEFAULT 'custom',
        note       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entry_link_items (
        link_id  INTEGER NOT NULL REFERENCES entry_links(id) ON DELETE CASCADE,
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        PRIMARY KEY (link_id, entry_id)
    )
    """,
    # 反查「这个条目属于哪些组」走 entry_id，必须单独建索引，否则自连接会退化成全表扫描
    "CREATE INDEX IF NOT EXISTS idx_link_items_entry ON entry_link_items(entry_id)",
    # ---- 概念节点 + 层级树（需求 6）----------------------------------------
    # 「节点身份」超集：概念节点（kind='concept'）与真实词条节点（kind='entry'）
    # 共用同一套 node_id，层级关系退化成 nodes 上的邻接表。
    #
    # ★ 为什么概念节点单独一张表、而不是在 entries 里插 is_concept 占位行 ★
    # 那样做必须改动 entries.type 的 CHECK（要重建全库最大的表），且要在**每一个**
    # 读 entries 的地方都记得加过滤（词条列表 / 导出 / 查重 / 复习候选池 / 练习候选池 /
    # 高亮），漏一处就穿帮。独立表让「概念节点不在 entries 里」成为**结构保证**：
    # SELECT ... FROM entries 无论怎么写都查不到它，零过滤 = 零漏网。
    #
    # real 不单独存列：kind='entry' 即 real=true（由下面 CHECK 保证 kind 与
    # entry_id / label 三者自洽，杜绝「kind 说 entry、entry_id 却为空」的脏行）；
    # JSON 响应里再派生一个布尔 real 供前端判断，前端只读不算。
    # label 只在 kind='concept' 时有意义；kind='entry' 时名字实时取 entries.term，
    # **不冗余存一份**——否则用户改了词条名，树上的显示就成了旧词。
    # 这是新增表，可以自由写 CHECK 与部分唯一索引（既有表加 CHECK 才需要重建）。
    """
    CREATE TABLE IF NOT EXISTS nodes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT    NOT NULL CHECK(kind IN ('concept', 'entry')),
        label       TEXT,
        note        TEXT    NOT NULL DEFAULT '',
        entry_id    INTEGER REFERENCES entries(id) ON DELETE CASCADE,
        -- tombstone_label：非 NULL 即表示「这是一个占位节点」——原词条被删除，但它在
        -- 概念树里还有子项，于是节点原地留下、标成已删除，子节点继续挂在它下面。
        -- 值是删除那一刻的词条名（用户自由文本，前端一律 textContent 渲染）。
        -- tombstoned_at：删除发生的时间。
        --
        -- ★ 为什么占位节点复用 kind='concept'、而不是新增 kind='tombstone' ★
        -- 下面那条 CHECK 恰好已经允许「kind='concept' AND label IS NOT NULL AND
        -- entry_id IS NULL」这一形状。把词条节点的 label 填成原词条名、entry_id 置 NULL、
        -- kind 改成 concept，一条 UPDATE 就成了合法的占位节点——**不需要重建表、
        -- 不需要动那条 CHECK**，存量库只要补两列即可。顺带白拿概念节点的全部既有保证：
        -- 它不在 entries 表里 ⇒ 结构上不可能参与评分、不可能跳词条详情、进不了练习候选池。
        -- 见 nodes.py 的「占位节点（tombstone）」章节。
        tombstone_label TEXT,
        tombstoned_at   TEXT,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        CHECK (
            (kind = 'concept' AND label IS NOT NULL AND entry_id IS NULL) OR
            (kind = 'entry'   AND label IS NULL     AND entry_id IS NOT NULL)
        )
    )
    """,
    # 反查「这个条目挂在哪个节点下」必须走 entry_id；
    # 部分唯一索引同时承担两个职责：查得动 + 一个条目最多一个节点。
    # （普通 UNIQUE 对多个 NULL 互不冲突，正好用不上，必须写部分索引。）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_entry ON nodes(entry_id) WHERE entry_id IS NOT NULL",
    # 概念节点按名字搜索（kind='concept' 的行才命中，entry 节点的 label 是 NULL）
    "CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label) WHERE kind = 'concept'",
    # 层级 / 从属关系：parent 是上位概念（父），child 是下位（子），一行 = 一条有向边。
    # 双向一致天然成立：查「B 的父」= SELECT parent_id WHERE child_id=B，
    # 查「A 的子」= SELECT child_id WHERE parent_id=A，两条都是查同一张表的不同方向，
    # 不存在反向行失步（与 links.py 的「组模型双向」同一哲学）。
    # 刻意用邻接表而不是 nodes.parent_id 自引用列：
    #   ① UNIQUE(child_id) 才能表达「一个节点只有一个父」，放在边表上只是一行索引；
    #   ② 取整棵树用递归 CTE 一次查完，不需要多次往返；
    #   ③ position 天然是「同一父下的第几个子」，支持拖拽排序。
    """
    CREATE TABLE IF NOT EXISTS node_edges (
        parent_id  INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        child_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        position   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (parent_id, child_id),
        CHECK (parent_id <> child_id)   -- 兜底：节点不能是自己的父（业务层还会先查一次）
    )
    """,
    # 取整棵树 / 排子节点：从父出发按 position 顺序取子
    "CREATE INDEX IF NOT EXISTS idx_edges_parent ON node_edges(parent_id, position, child_id)",
    # 「这节点的父是谁 / 面包屑 / 环检测」的反查入口。
    # **这个唯一索引同时就是「一个节点只有一个父」的约束载体**：child_id 全局唯一
    # ⇒ 同一节点不可能同时挂到两个父下。将来要放开跨树复用，删掉它即可，其余设计不用改。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_child ON node_edges(child_id)",
    # 库级元信息（种子版本号等）。**刻意做成最普通的 key-value**：
    # 以后要记 schema 版本、构建时间、访客来源，都往这里塞，不必再改 schema。
    # 桌面版写入 0 行 —— 它是云端幂等灌种子的判据（见 app/seed.py）。
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]

# 等待写锁的超时（秒）。SQLite 默认 5.0s 偏紧：批量导入（数千条）持锁可能超过 5s，
# 期间用户的保存请求会直接失败并报 "database is locked"。抬到 15s 盖过导入窗口。
DB_TIMEOUT_SECONDS = 15.0

# 复习状态初始值：新增条目时同步播种一行 review_state，供 P2 复习模块直接使用
DEFAULT_EASINESS = 2.5

# 为 P1 之前已存在的历史条目补齐复习状态（幂等，可重复执行）
BACKFILL_REVIEW_STATE = """
INSERT INTO review_state (entry_id, easiness, interval_days, reps, lapses, due_at, last_reviewed_at)
SELECT e.id, ?, 0, 0, 0, e.created_at, NULL
FROM entries e
WHERE e.id NOT IN (SELECT entry_id FROM review_state)
"""

# 为既有词条补齐节点行（幂等，可重复执行）。与 BACKFILL_REVIEW_STATE 同一套路。
BACKFILL_ENTRY_NODES = """
INSERT INTO nodes (kind, label, note, entry_id, created_at, updated_at)
SELECT 'entry', NULL, '', e.id, e.created_at, e.updated_at
FROM entries e
WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.entry_id = e.id)
"""


def _backfill_entry_nodes(conn: sqlite3.Connection) -> None:
    """为每个已有词条补一个节点行（幂等）。

    为什么需要：层级的父 / 子都是 node_id，真实词条要能当子节点就必须先有 node 行。
    新建词条时由 entries.create_entry 同步播种，但**本功能上线前就存在的词条**没有
    这一步，需要这里补齐——用户是在旧库上直接跑新版 exe 的。

    守卫：先比数量，只在真的缺行时才执行 INSERT。否则每次启动都对 entries / nodes
    做两次 COUNT，纯粹的无谓开销（数据量小也不能养成习惯）。
    """
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    linked = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE entry_id IS NOT NULL"
    ).fetchone()[0]
    if total == linked:
        return
    conn.execute(BACKFILL_ENTRY_NODES)


def normalize_term(value: object) -> str:
    """词条归一化：去首尾空白、压缩中间空白、转小写。用于重复录入判定。"""
    if value is None:
        return ""
    return " ".join(str(value).split()).lower()


def _migrate_passages_origin(conn: sqlite3.Connection) -> None:
    """扩展 passages.origin 取值：新增 library（外刊库）与 web（网络外刊）。

    SQLite 不支持修改 CHECK 约束，只能重建表。此处只在检测到旧约束时执行，
    且 passages 非数据时直接重建——若表内已有数据则抛错而非静默丢弃。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'passages'"
    ).fetchone()
    if row is None or "'library'" in (row[0] or ""):
        return  # 新表或已是新结构
    count = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
    if count:
        raise RuntimeError(
            f"passages 表存在 {count} 条旧数据且 origin 约束需扩展，请人工备份后迁移"
        )
    conn.execute("DROP TABLE passages")
    for stmt in SCHEMA:
        if "CREATE TABLE IF NOT EXISTS passages" in stmt:
            conn.execute(stmt)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_article_translation(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐中文译文列（幂等）。

    articles.content_zh / article_sentences.text_zh 是后加的功能列。
    SQLite 的 ADD COLUMN 带默认值即可原地扩展，无需重建表；
    每次启动都执行，靠 PRAGMA table_info 判重，重复执行无副作用。
    """
    if "content_zh" not in _table_columns(conn, "articles"):
        conn.execute("ALTER TABLE articles ADD COLUMN content_zh TEXT NOT NULL DEFAULT ''")
    if "text_zh" not in _table_columns(conn, "article_sentences"):
        conn.execute("ALTER TABLE article_sentences ADD COLUMN text_zh TEXT")


def _migrate_article_extra_info(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐「其他信息」列（幂等）。

    articles.extra_info 是后加的文章级元数据列（作者信息/文章背景等）。
    沿用 ADD COLUMN 带默认值原地扩展，不重建表；靠 PRAGMA table_info 判重，
    每次 init_db 都执行，重复执行无副作用。
    """
    if "extra_info" not in _table_columns(conn, "articles"):
        conn.execute("ALTER TABLE articles ADD COLUMN extra_info TEXT NOT NULL DEFAULT ''")


def _migrate_article_title_zh(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐「中文标题」列（幂等）。

    articles.title_zh 是后加的文章级字段：外刊原文标题是英文，用户还要一个
    中文标题。与 extra_info 同一套路 —— ADD COLUMN 原地扩展，不重建表、
    不动既有行；靠 PRAGMA table_info 判重，每次 init_db 都执行，
    重复执行无副作用（用户会在旧库上直接跑新版 exe，这是硬要求）。

    刻意用**可空无默认**而不是 NOT NULL DEFAULT ''：
    「没填中文标题」与「填了空串」语义不同，NULL 才是干净的「未提供」，
    且旧行自动为 NULL，不需要回填。读取侧一律按 NULL/'' 兜底成空串。
    """
    if "title_zh" not in _table_columns(conn, "articles"):
        conn.execute("ALTER TABLE articles ADD COLUMN title_zh TEXT")


def _drop_legacy_tags(conn: sqlite3.Connection) -> None:
    """删除废弃的【词汇】标签体系（用户确认词汇/短语不再需要标签）。

    旧体系的签名是 entry_tags 表存在（那时的 tags 表挂在词汇条目上）。
    只有检出旧体系才连 tags 一起删；没有 entry_tags 时，tags 表属于新体系
    （外刊文章标签，见 SCHEMA），绝不能误删。先删子表再删父表。
    """
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entry_tags'"
    ).fetchone()
    if legacy is None:
        return
    conn.execute("DROP TABLE IF EXISTS entry_tags")
    conn.execute("DROP TABLE IF EXISTS tags")


def _migrate_entry_ratings(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐条目评分列（幂等）。

    entries.importance / usefulness 是后加的评分列。两列都是可空 REAL，
    SQLite 的 ADD COLUMN 可原地扩展，不需要重建表、也不动既有行的值
    （老行自动为 NULL = 未评分，正是我们想要的语义）。
    靠 PRAGMA table_info 判重，每次 init_db 都执行，重复执行无副作用。
    """
    columns = _table_columns(conn, "entries")
    if "importance" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN importance REAL")
    if "usefulness" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN usefulness REAL")


def _migrate_article_tags(conn: sqlite3.Connection) -> None:
    """把存量 articles.topic 单值无损转成标签关联（幂等，可重复执行）。

    外刊分类已从「单主题」升级为「多标签」：历史上的 topic 值（如「经济」）
    转为该文章的第一个标签，数据不丢失。INSERT OR IGNORE 保证重复执行
    不产生重复关联。topic 列自此停用（不再读写），保留只为避免 DROP COLUMN
    的重建表风险；新库本就不建该列。
    """
    if "topic" not in _table_columns(conn, "articles"):
        return
    rows = conn.execute(
        "SELECT id, topic FROM articles WHERE topic IS NOT NULL AND topic != ''"
    ).fetchall()
    for row in rows:
        name = (row["topic"] or "").strip()
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (row["id"], tag_id),
        )


def _open(path) -> sqlite3.Connection:
    """裸连接：只开连接 + 设 PRAGMA / 注册函数，**不做任何云端守卫**。

    专供建库流程（cloud.ensure_tenant_db）使用 —— 那里的目标文件路径是
    显式传进来的 tmp 文件，与「当前请求是谁」无关，不能被访客守卫拦下。
    """
    conn = sqlite3.connect(str(path), timeout=DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 注册归一化函数，供 SQL 侧做大小写/空白无关的重复判定（deterministic 便于索引与比较）
    conn.create_function("norm_term", 1, normalize_term, deterministic=True)
    return conn


def connect() -> sqlite3.Connection:
    from . import cloud
    if cloud.cloud_mode() and not cloud.current_tenant.get():
        # ★ 云端不该有「无访客」的连接 ★
        #   一旦发生（lifespan / 后台任务 / 忘了套中间件），就会连到全局库，
        #   把 A 访客的数据写进公共文件 —— 那是数据泄露。
        #   ★ 故意抛错而不是静默回退全局库 ★
        #     静默串数据是**安全事故**且不可诊断；抛错是**可诊断的故障**。
        #   桌面：cloud_mode() 为 False，下面一行与改造前逐字等价。
        raise RuntimeError("云端模式必须在访客请求上下文中使用 db.connect()")
    return _open(db_path())


def _migrate_link_updated_at(conn: sqlite3.Connection) -> None:
    """为既有库补齐 entry_links.updated_at（幂等，可重复执行）。

    建表时只写了 created_at，但关联组的成员/关系/备注都能改，缺 updated_at 就没法
    告诉用户「这组最近动过」。补列用可空 TEXT，ADD COLUMN 原地扩展、不碰存量行；
    老行的 updated_at 回填成 created_at，避免列表里出现空时间戳。
    """
    cols = _table_columns(conn, "entry_links")
    if "updated_at" in cols:
        return
    conn.execute("ALTER TABLE entry_links ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE entry_links SET updated_at = created_at WHERE updated_at IS NULL")


def _migrate_node_tombstones(conn: sqlite3.Connection) -> None:
    """为既有数据库补齐占位节点的两列（幂等，可重复执行）。

    nodes.tombstone_label / nodes.tombstoned_at 是需求 3「删词条留占位」后加的列。
    两列都**可空**，SQLite 的 ADD COLUMN 原地扩展即可：既不重建 nodes 表、
    也不碰任何存量行，老行的这两列自动是 NULL。

    ★ 「存量节点不误变占位」是白拿的 ★
    占位的判定口径就是 `tombstone_label IS NOT NULL`，而新增列在老行上恒为 NULL，
    所以任何既有节点（包括全部 kind='entry' 的真实词条节点）在迁移后都不可能是占位。
    刻意**不写**「迁移时把老节点标成占位」这类数据搬迁语句——那需要额外判断
    「这个节点对应的词条还在不在」，而词条都在（否则 CASCADE 早就把节点删了）。

    与 _migrate_article_translation / _migrate_entry_ratings 同一套路：
    每次 init_db 都执行，靠 PRAGMA table_info 判重，重复执行无副作用。
    """
    columns = _table_columns(conn, "nodes")
    if "tombstone_label" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN tombstone_label TEXT")
    if "tombstoned_at" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN tombstoned_at TEXT")


def _apply_schema(conn: sqlite3.Connection) -> None:
    """建表 + 迁移 + 回填。**不 commit**，由调用方决定事务边界。

    ★ 为什么抽出来 ★
    云端建库要「schema + 种子」在同一个库里一次性落位（不能出现
    「库建好了但种子还没灌」的窗口），所以需要一个能接收现成连接的版本。
    本函数体与原 init_db() 的函数体**逐字同源**（只是不 commit）。
    """
    _drop_legacy_tags(conn)  # 先清旧体系再建新表（tags 表名被新体系复用）
    for stmt in SCHEMA:
        conn.execute(stmt)
    _migrate_passages_origin(conn)
    _migrate_article_translation(conn)
    _migrate_article_extra_info(conn)
    _migrate_article_title_zh(conn)
    _migrate_article_tags(conn)
    _migrate_entry_ratings(conn)
    _migrate_link_updated_at(conn)
    _migrate_node_tombstones(conn)
    _backfill_entry_nodes(conn)  # 新表已建完，这里才能给存量词条补节点行
    conn.execute(BACKFILL_REVIEW_STATE, (DEFAULT_EASINESS,))


def init_db() -> None:
    """桌面版初始化。签名与行为与改造前完全一致。"""
    conn = connect()
    try:
        _apply_schema(conn)
        conn.commit()
    finally:
        conn.close()
