# 我的英文库 · MyEnglishVault

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%"
       alt="我的英文库 MyEnglishVault — a personal English vault where words live in a real, draggable mesh of relationships">
</p>

> A personal English-learning vault where every word lives inside a real, draggable **mesh** of relationships — not a flat list, and not a single tree.

## What it is

MyEnglishVault is a local-first desktop / web app for building a personal English vocabulary. You collect words, paste external articles for reading practice, drill translations, and — most importantly — connect words to each other on a **draggable association canvas**. It runs entirely on your machine: a FastAPI backend, a SQLite file, and a vanilla-JS frontend wrapped in a `pywebview` window (or served as a plain web app).

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
| Translation practice | 翻译练习 | Drill sentences drawn from your reading; you translate back into English and an AI grader scores it on three weighted dimensions. |
| Vocabulary | 词汇库 | Your personal word list with meanings, usage, and ratings. |
| Reading | 外刊库 | Paste external articles; they are auto-split into 1:1 sentence pairs for practice. |
| Association canvas | 关联库 | The draggable mesh: build hierarchy and synonym groups visually. |
| Progress | 学习进度 | Analytics on what you have studied and how it is going. |

## AI grading — the differentiator

The translation practice doesn't just show you the reference sentence. If you provide your own AI key (OpenAI-compatible endpoint), the model grades your translation on **three weighted dimensions**:

- **Grammar (语法) — 30%**
- **Fluency (地道度) — 30%**
- **Faithfulness (语义保真) — 40%**

Faithfulness is weighted highest on purpose: a fluent sentence that says the wrong thing is worse than a slightly awkward one that stays true to the original. The grader also tells you which dimension is your weakest.

No AI key? You still get the original sentence to compare against — no fake scores, no fake translations.

Local-first: only the single sentence being scored ever leaves your machine.

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

> **Which file do I actually run?** Only **one**: `run.py` (desktop) or `web_server.py` (web).
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
