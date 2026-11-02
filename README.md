# 我的英文库 · MyEnglishVault

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%"
       alt="我的英文库 MyEnglishVault — a personal English vault where words live in a real, draggable mesh of relationships">
</p>

> A personal English-learning vault where every word lives inside a real, draggable **mesh** of relationships — not a flat list, and not a single tree.

## What it is

MyEnglishVault is a local-first **desktop app** for building a personal English vocabulary. You collect words, paste external articles for reading practice, drill translations, and — most importantly — connect words to each other on a **draggable association canvas**. It runs entirely on your machine: a FastAPI backend, a SQLite file, and a vanilla-JS frontend in a `pywebview` window.

## Why it's different

Most vocabulary tools force every relationship into one hierarchy — a tree. This project treats a word's **hierarchy** (what it is a kind of, what it is made of) and its **synonym peers** (what it sits beside) as two fundamentally different things, and stores them in **two separate data models that never merge into one table**.

<p align="center">
  <img src="./assets/readme/mesh-model.svg" width="100%"
       alt="Two relationship systems: vertical is-a/meronymy tree (green) and horizontal synonym-peer group (blue), with antonyms (red) attaching orthogonally">
</p>

- **纵向 · 从属层级 (Is-a / meronymy)** — a directed tree. A word can have a parent concept *without* becoming a study entry itself (placeholder nodes).
- **横向 · 近义同伴 (Synonym peers)** — an undirected peer group on the same level.
- **反义 (Antonym)** edges attach orthogonally, on either axis.

The result is a true **network**, not a tree: the same word can be a child in one structure and a peer in another, and the two never conflict.

## Features

| Module | 中文 | What it does |
| --- | --- | --- |
| Translation practice | 翻译练习 | Drill sentences drawn from your reading; you translate back into English and an **AI grader** scores it on three weighted dimensions, then reveals the reference. |
| Vocabulary | 词汇库 | Your personal word list with meanings, usage, and ratings. |
| Reading | 外刊库 | Paste external articles; they are auto-split into 1:1 sentence pairs for practice. |
| Association canvas | 关联库 | The draggable mesh: build hierarchy and synonym groups visually. |
| Progress | 学习进度 | Analytics on what you have studied and how it is going. |
| Export | 导出 | Your vocabulary as a **JSON backup** or a **Markdown** file; any reading article as a **Word** document with per-sentence translation alongside the original. |

## AI grading — the differentiator

The practice loop is **not** "type the translation and check a key." When you translate a sentence back into English, an **AI grader** evaluates it on three weighted dimensions and tells you *which* dimension you lost points on, with a better phrasing:

| Dimension | Weight | What it measures | Why this weight |
| --- | --- | --- | --- |
| Grammar · 语法正确 | 30% | Right/wrong — a mistake is a mistake. | Binary and fixable: non-negotiable, but rarely the whole story. |
| Fluency · 表达地道 | 30% | Native-sounding or not — correct yet awkward. | Improvement, not a hard fail: worth flagging, not worth zeroing. |
| Semantic fidelity · 语义保真 | 40% | Did the meaning actually get across? | The bottom line: no meaning, no score — expression skill is irrelevant without it. |

The weights encode a product judgment: **semantic fidelity is the floor, not a tie-breaker.** A grammatically perfect sentence that misses the meaning scores lower than a clumsy one that lands it — because the whole point is to *use* the word, not just recite its definition. Grammar and fluency are "how well," but fidelity is "whether at all."

The grader talks to an **OpenAI-compatible endpoint** (tested on Doubao; switching to DeepSeek / Tongyi is a config change, not a code change). Per the local-first principle, the **only** data that ever leaves your machine is the single sentence currently being graded — your vocabulary, articles, and history never leave the local SQLite file.

## Screenshots

<table>
  <tr>
    <td align="center"><img src="./assets/readme/screenshots/vocabulary.png" width="100%" alt="词汇库 Vocabulary screen"><br><sub>词汇库 · Vocabulary</sub></td>
    <td align="center"><img src="./assets/readme/screenshots/relation-canvas.png" width="100%" alt="关联库 hierarchy view"><br><sub>关联库 · Hierarchy</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="./assets/readme/screenshots/relation-canvas-synonyms.png" width="100%" alt="关联库 synonym peers view"><br><sub>关联库 · Synonym peers</sub></td>
    <td align="center"><img src="./assets/readme/screenshots/translation-practice.png" width="100%" alt="翻译练习 translation practice screen"><br><sub>翻译练习 · Practice</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="./assets/readme/screenshots/translation-practice-compose.png" width="100%" alt="翻译练习 sentence composition screen"><br><sub>翻译练习 · Sentence composition</sub></td>
    <td align="center"><img src="./assets/readme/screenshots/reading.png" width="100%" alt="外刊库 reading screen"><br><sub>外刊库 · Reading</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="./assets/readme/screenshots/analytics.png" width="100%" alt="学习进度 progress analytics screen"><br><sub>学习进度 · Progress</sub></td>
    <td align="center"></td>
  </tr>
</table>

## Quick start

### One-click start (recommended)

- **Windows:** double-click **`start.bat`** in this folder. It installs dependencies on first run, then opens the desktop window.
- **macOS / Linux:** from this folder, run **`bash start.sh`**.

Both scripts are idempotent — re-running them just launches the app (the dependency install is fast once cached). Your data is stored locally; nothing is uploaded.

> **Which file do I actually run?** Only **one**: `run.py`.
> The `app/`, `web/`, `tools/`, and `tests/` folders are application code, assets, and the
> automated test suite — **not** programs you open or execute.

### Prerequisites
- Python **3.11+** (developed and tested on 3.13)
- `pip`
- Desktop window: a graphical environment (uses `pywebview`)
- Frontend tests only: Node.js + `jsdom`

### Install
```bash
pip install -r requirements.txt
```
*(`requirements.txt` pins exact, regression-verified versions, so `pip install -r requirements.txt` reproduces the tested environment.)*

### Run
```bash
python run.py
```
Opens a single `pywebview` window (backend on `127.0.0.1:8765`).

### Your data
The app starts with an **empty database**. Add words through the UI — your vocabulary, articles, and practice history are stored locally in a SQLite file, with no personal information uploaded.

The bundled demo corpus (`app/seed_data.py`) is **self-authored and copyright-clean** (no external-publication text). To load it for development or testing:
```bash
python -m app.seed --db ./demo.sqlite    # refuses to touch your real data
```

## Project layout

**You run exactly one file** — `run.py`.
Everything else is code, tests, or assets. **Do not run** the files inside `app/`,
`web/`, `tools/`, or `tests/` (the last one is the automated test suite).

```
my-english-vault/
├── run.py                 # ★ THE APP — double-click start.bat, or: python run.py
├── app/                   # Backend (FastAPI): database, vocabulary logic, AI, security
├── web/                   # Frontend: index.html + app.js + style.css — the user interface
├── tools/                 # Developer scripts (seed checker, build fingerprint…) — NOT needed to run
├── tests/                 # Automated test suite (pytest + jsdom) — NOT the app
├── assets/                # Images used by this README (screenshots, diagrams)
├── requirements.txt       # Python dependencies (pinned to tested versions)
├── start.bat / start.sh   # One-click launchers (install deps + run the app)
└── LICENSE / README.md
```

## Quality & tests

**1,920 automated assertions, all green** — 1,825 backend checks across 22 test suites plus 95 frontend DOM checks.

```bash
pytest                           # backend
npm install --no-save jsdom     # one-time
node tests/test_frontend_links.js   # frontend DOM regression
```

## Made with

MyEnglishVault is a personal project by **Jack Hever**.

**Stack:** Python 3.11+ · FastAPI · SQLite · uvicorn · pywebview · Vanilla JavaScript

## License
[MIT](./LICENSE) © 2026 Jack Hever
