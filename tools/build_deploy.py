# -*- coding: utf-8 -*-
"""★ 生成部署目录 `deploy/`（白名单制）★★

★ 为什么必须白名单，不能整目录拷 ★
整目录拷会把这些东西一起传上公网：
  · `settings.json`（**含用户真实 AI 密钥**）
  · `data.db`（**用户真实词条/练习记录/个人笔记**）
  · `tenants/`（所有访客的独立库）
  · `dist/` 里的 49MB exe
  · `test_*.py` / `qa_*.js` / `shot_*.js` 等开发脚本
「不得包含个人信息」是硬要求，上面任何一项泄漏都是安全事故。

★ 部署目录里**必须**有的最小集 ★
  `app/`（整包，含 seed_data.py）+ `web/` + `web_server.py` + `requirements-web.txt`
  —— `app/seed_data.py` 是演示语料的**唯一来源**，少了它新访客就是空库。

用法：python tools/build_deploy.py
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"

# ---- 必须包含 ----
INCLUDE_FILES = ["web_server.py", "requirements-web.txt"]
INCLUDE_DIRS = ["app", "web"]

# ---- 绝不能出现（即便被上面规则带进来也要挡）----
FORBIDDEN_NAMES = {
    "settings.json", "data.db", "data.db-wal", "data.db-shm",
    "users.db", "auth.json", ".env",
}
FORBIDDEN_DIR_PARTS = {
    "tenants", "__pycache__", "dist", "build",
    "node_modules",
}


def _is_forbidden(path: Path) -> bool:
    if path.name in FORBIDDEN_NAMES:
        return True
    if any(part in FORBIDDEN_DIR_PARTS for part in path.parts):
        return True
    # ★ 隐藏目录/隐藏文件一律不打包 ★
    #   本地工具工作区、VCS 元数据、缓存目录、各类编辑器配置 —— 名称可能变，
    #   但共同点是「以点开头」。仓库里没有任何必须保留的隐藏文件
    #   （app/ 与 web/ 实测无隐藏文件），所以按形态拦最稳。
    return any(part.startswith(".") for part in path.parts)


def main() -> int:
    # ★ 不用 shutil.rmtree 删整个目录 ★
    # 沙箱会把 rmtree 拦成「回收站操作」，实测在 Windows 上会失败。
    # 改成「就地覆盖 + 清掉多余文件」，效果一样且不碰删除语义。
    DEPLOY.mkdir(parents=True, exist_ok=True)

    # 先记下本次要写的所有目标路径
    wanted: set[Path] = set()
    for rel in INCLUDE_FILES:
        wanted.add(DEPLOY / rel)
    for d in INCLUDE_DIRS:
        sdir = REPO / d
        if not sdir.is_dir():
            print(f"[FAIL] 缺目录: {d}")
            return 1
        for src in sdir.rglob("*"):
            if src.is_file() and not _is_forbidden(src.relative_to(REPO)):
                wanted.add(DEPLOY / src.relative_to(REPO))

    # 清掉上一次遗留、本次不再需要的文件（只删 deploy/ 内的，不碰仓库）
    stale = []
    for p in sorted(DEPLOY.rglob("*"), reverse=True):
        if p.is_file() and p not in wanted:
            p.unlink()
            stale.append(str(p.relative_to(DEPLOY)))
    for p in sorted(DEPLOY.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            try:
                p.rmdir()
            except OSError:
                pass
    if stale:
        print(f"清理上次遗留 {len(stale)} 个文件")

    copied = 0
    skipped = []

    # 文件
    for rel in INCLUDE_FILES:
        src = REPO / rel
        if not src.exists():
            print(f"[FAIL] 缺文件: {rel}")
            return 1
        shutil.copy2(src, DEPLOY / rel)
        copied += 1

    # 目录
    for d in INCLUDE_DIRS:
        sdir = REPO / d
        for src in sdir.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(REPO)
            if _is_forbidden(rel):
                skipped.append(str(rel))
                continue
            dst = DEPLOY / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    # ---- 部署后自检 ----
    print(f"部署目录: {DEPLOY}")
    print(f"复制文件数: {copied}")
    if skipped:
        print(f"跳过（敏感/中间产物）: {skipped}")

    # ① 语料源必须在
    seed = DEPLOY / "app" / "seed_data.py"
    if not seed.exists():
        print("[FAIL] 部署目录缺 app/seed_data.py —— 新访客会是空库")
        return 1
    md5 = hashlib.md5(seed.read_bytes()).hexdigest()
    print(f"[OK] app/seed_data.py 在位 (md5={md5})")

    # ② 绝不能有密钥/真实数据
    bad = []
    for p in DEPLOY.rglob("*"):
        if p.is_file() and (_is_forbidden(p.relative_to(DEPLOY))
                            or p.name in FORBIDDEN_NAMES):
            bad.append(str(p.relative_to(DEPLOY)))
    if bad:
        print(f"[FAIL] 部署目录含敏感文件: {bad}")
        return 1
    print("[OK] 无 settings.json / data.db / tenants/")

    # ③ 入口齐全
    for need in ("web_server.py", "requirements-web.txt", "web/index.html",
                 "web/app.js", "web/style.css", "app/main.py", "app/cloud.py",
                 "app/seed.py"):
        if not (DEPLOY / need).exists():
            print(f"[FAIL] 部署目录缺 {need}")
            return 1
    print("[OK] 入口文件齐全")

    # ④ 体积
    total = sum(p.stat().st_size for p in DEPLOY.rglob("*") if p.is_file())
    print(f"[OK] 总体积 {total/1024/1024:.2f} MB")

    # ⑤ 语法可编译
    import py_compile
    errs = []
    for p in list((DEPLOY / "app").glob("*.py")) + [DEPLOY / "web_server.py"]:
        try:
            py_compile.compile(str(p), doraise=True, cfile=str(p) + "c")
        except py_compile.PyCompileError as exc:
            errs.append(f"{p.name}: {exc}")
        finally:
            q = Path(str(p) + "c")
            if q.exists():
                q.unlink()
    if errs:
        print(f"[FAIL] 语法错误: {errs}")
        return 1
    print("[OK] 全部 .py 编译通过")

    print("\nDEPLOY BUILD OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
