/**
 * 前端 DOM 回归：星级控件 / 释义栏关联链接（XSS 红线）/ 关联库块列表 /
 * ★ AI 建议「接受 → PATCH 落库」这条风险路径。
 *
 * 运行：node tests/test_frontend_links.js
 * 说明：用 jsdom 加载**真实的** web/index.html + web/app.js（不是源码文本模拟），
 *       fetch 被替换成本地的假实现，断言打在真实 DOM 上。
 *       需要 jsdom：npm install --no-save jsdom
 *
 *⚠ 维护提醒（这个文件丢过一次）：
 *   项目**不是 git 仓库**，没有历史可恢复。本文件是"星级 / 关联链接 / AI 接受落库"
 *   唯一的真 DOM 回归，删掉它这些路径就没人看了。改完请同步备份到 C:\tmp\。
 *
 * 与当前界面的对应关系（改版时先对一遍，避免断言指向已不存在的节点）：
 *   - 主表 8 列，例句 / 来源已移入**详情页** #v-detail-usage / #v-detail-source；
 *   - 表格与详情页的星级是 **readOnly**（需求 7：浏览时不能改分）；
 *   - **可打分**的星级在编辑弹窗 #f-importance / #f-usefulness，且是 **defer** 模式
 *     （点星只改本地，保存时随表单走 PUT /api/entries/{id}）；
 *   - 唯一会发 **PATCH /api/entries/{id}/ratings** 的是 AI 建议的「接受」。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const results = [];

function check(name, ok, detail) {
  results.push({ name, ok });
  console.log('    [' + (ok ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -> ' + detail : ''));
}

function tick(n) {
  let p = Promise.resolve();
  for (let i = 0; i < (n || 4); i++) {
    p = p.then(function () {
      return new Promise(function (r) { setTimeout(r, 0); });
    });
  }
  return p;
}

// ---------------------------------------------------------------------------
// 假后端：覆盖 app.js 启动与被测路径会打到的所有接口
// ---------------------------------------------------------------------------

const XSS = '<img src=x onerror="window.__pwned=1">';
const ENTRIES = [
  { id: 1, type: 'word', term: 'abundant', pos: 'adj.', meaning_zh: XSS, usage: '', source: '', importance: 4.5, usefulness: 3.0, highlight: true, created_at: '2025-01-01T10:00:00', updated_at: '2025-01-01T10:00:00' },
  { id: 2, type: 'word', term: 'ample', pos: 'adj.', meaning_zh: '充足的', usage: '', source: '', importance: null, usefulness: null, highlight: false, created_at: '2025-01-02T10:00:00', updated_at: '2025-01-02T10:00:00' },
  { id: 3, type: 'word', term: 'plentiful', pos: 'adj.', meaning_zh: '丰富的', usage: '', source: '', importance: 0, usefulness: 0, highlight: false, created_at: '2025-01-03T10:00:00', updated_at: '2025-01-03T10:00:00' },
  // 走 renderRows 的 innerHTML + esc() 路径的字段：XSS 同样必须被挡住。
  // usage / source 现在渲染在**详情页**（需求 5），所以它们的断言放在 [2b] 的详情页段。
  { id: 4, type: 'word', term: 'evil' + XSS, pos: '<b>adj.</b>', meaning_zh: '恶意的',
    usage: XSS, source: XSS, importance: 5, usefulness: 5, highlight: true,
    created_at: '2025-01-04T10:00:00', updated_at: '2025-01-04T10:00:00' },
];

const state = {
  calls: [],
  ratingsFail: false,      // true 时所有 ratings PATCH 都 400
  ratingsFailOnce: false,  // true 时**下一次** ratings PATCH 400 后自动复位
  removeDissolved: false,  // 移出成员时后端返回 dissolved:true
  // /api/entries/ratings/suggest 的返回内容（可按用例改写）
  suggestItems: null,
  links: [
    { id: 11, relation: 'synonym', relation_label: '近义词', note: '都表示充足', created_at: '2025-01-05T10:00:00', updated_at: '2025-01-05T10:00:00', members: [ENTRIES[0], ENTRIES[1]] },
  ],
};

function respond(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status: status,
    text: function () { return Promise.resolve(JSON.stringify(body)); },
  });
}

/** 找词条；找不到时回落到 1 号，避免测试因为夹具问题而不是因为行为失败。 */
function entryById(id) {
  return ENTRIES.filter(function (e) { return e.id === id; })[0] || ENTRIES[0];
}

function fakeFetch(url, options) {
  const opts = options || {};
  const method = (opts.method || 'GET').toUpperCase();
  let body = null;
  try { body = opts.body ? JSON.parse(opts.body) : null; } catch (e) { body = opts.body; }
  state.calls.push({ url: url, method: method, body: body });

  if (url.indexOf('/api/health') === 0) return respond(200, { ok: true, db_path: '/tmp/fake.db' });
  if (url.indexOf('/api/stats') === 0) return respond(200, { entries: 4, passages: 0, practices: 0, articles: 0 });

  // --- AI 评分建议（必须排在下面的 /api/entries 前缀匹配之前） ---
  if (url.indexOf('/api/entries/ratings/suggest') === 0 && method === 'POST') {
    const ids = (body && body.entry_ids) || [];
    const items = state.suggestItems || ids.map(function (id, i) {
      const e = entryById(id);
      const had = e.importance !== null || e.usefulness !== null;
      return {
        entry_id: id, term: e.term,
        // 故意与库里的旧值不同：overwrite=true 时才可能走到这里，
        // 数值不同才能证明"接受后真的覆盖了旧手动分"而不是恰好相等。
        importance: 2.0 + i * 0.5, usefulness: 3.5,
        reason: { importance: '命中 4/12 篇，雅思论证常见', usefulness: '可替换平庸论证表达' },
        skipped: false, had_rating: had, error: '',
      };
    });
    return respond(200, {
      items: items, total: items.length,
      evaluated: items.filter(function (i) { return !i.skipped && !i.error; }).length,
      skipped: items.filter(function (i) { return i.skipped; }).length,
      failed: items.filter(function (i) { return i.error; }).length,
      is_mock: false,
    });
  }

  // --- PATCH /api/entries/{id}/ratings：AI 建议「接受」的唯一落库入口 ---
  const rm = url.match(/\/api\/entries\/(\d+)\/ratings$/);
  if (rm && method === 'PATCH') {
    if (state.ratingsFailOnce) {
      state.ratingsFailOnce = false;
      return respond(400, { detail: '重要度应在 0 ~ 5 之间，当前 9' });
    }
    if (state.ratingsFail) {
      return respond(400, { detail: '重要度应在 0 ~ 5 之间，当前 9' });
    }
    const id = parseInt(rm[1], 10);
    const patched = Object.assign({}, entryById(id));
    if (body && 'importance' in body) patched.importance = body.importance;
    if (body && 'usefulness' in body) patched.usefulness = body.usefulness;
    return respond(200, patched);
  }

  // --- PUT /api/entries/{id}：编辑弹窗保存（评分随表单一起提交） ---
  const um = url.match(/^\/api\/entries\/(\d+)$/);
  if (um && method === 'PUT') {
    const id = parseInt(um[1], 10);
    const saved = Object.assign({}, entryById(id), body || {});
    return respond(200, saved);
  }
  if (url.indexOf('/api/entries') === 0 && method === 'POST') {
    return respond(200, Object.assign({ id: 99, created_at: '2025-02-01T10:00:00' }, body || {}));
  }

  // --- 单条 / 列表 ---
  const one = url.match(/^\/api\/entries\/(\d+)$/);
  if (one) return respond(200, entryById(parseInt(one[1], 10)));
  if (/\/api\/entries\/\d+\/links$/.test(url)) {
    return respond(200, {
      entry_id: 1,
      links: [{ link_id: 11, relation: 'synonym', relation_label: '近义词', entry: { id: 2, term: 'ample', meaning_zh: '充足的', type: 'word' } }],
    });
  }
  if (url.indexOf('/api/entries?') === 0 || url.indexOf('/api/entries') === 0) {
    return respond(200, { items: ENTRIES, total: ENTRIES.length, page: 1, page_size: 20 });
  }

  if (url.indexOf('/api/nodes/tombstones') === 0) return respond(200, { total: 0 });
  if (url.indexOf('/api/links?') === 0) return respond(200, { items: state.links, total: state.links.length, page: 1, page_size: 20 });
  if (/\/api\/links\/\d+\/remove$/.test(url)) {
    return respond(200, { link_id: 11, removed: 2, dissolved: state.removeDissolved });
  }
  if (/\/api\/links\/\d+$/.test(url) && method === 'DELETE') return respond(200, { deleted: 11 });
  if (url.indexOf('/api/links') === 0 && method === 'POST') {
    state.links.push({ id: 12, relation: body.relation, relation_label: '近义词', note: body.note || '', created_at: '2025-01-06T10:00:00', updated_at: '2025-01-06T10:00:00', members: ENTRIES.slice(0, 2) });
    return respond(201, state.links[state.links.length - 1]);
  }
  return respond(404, { detail: '未预期的请求：' + url });
}

// ---------------------------------------------------------------------------

async function main() {
  const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
  const appjs = fs.readFileSync(path.join(ROOT, 'web', 'app.js'), 'utf8');

  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost/' });
  const win = dom.window;
  win.fetch = fakeFetch;
  // ★ 越权注入的基线：在跑 app.js **之前**量一次 index.html 静态自带的 img 数。
  //   后面用它判定「渲染过程有没有新增 img」。
  //   刻意不用「文档里 img 数 == 0」当判据：那是与 XSS 无关的全局事实，
  //   index.html L79/L93 本来就有两张梵高画作装饰，写死0 等于永久假红、
  //   还会把真的注入红一起淹没。
  const BASELINE_IMGS = win.document.querySelectorAll('img').length;

  win.eval(appjs);
  await tick(8);

  const $ = function (id) { return win.document.getElementById(id); };
  const rows = function () { return win.document.querySelectorAll('#v-body tr[data-id]'); };
  const ratingCell = function (rowIndex, field) {
    return rows()[rowIndex].querySelector('.cell-rating[data-rating="' + field + '"]');
  };
  const starFills = function (cellSel) {
    const cell = typeof cellSel === 'string' ? $(cellSel) : cellSel;
    if (!cell) return null;
    return Array.prototype.map.call(cell.querySelectorAll('.star-fill'), function (f) { return f.style.width; });
  };

  console.log('[1] starValueFromPoint：左半 = 半星，右半 = 整星（纯函数，直接喂假 rect）');
  check('第 1 颗左半 -> 0.5', win.starValueFromPoint(0, 12, { left: 10, width: 20 }) === 0.5,
    win.starValueFromPoint(0, 12, { left: 10, width: 20 }));
  // rect = {left:10,width:20}：clientX 20 是正中，25 → ratio 0.75（右半）
  check('第 1 颗右半 -> 1', win.starValueFromPoint(0, 25, { left: 10, width: 20 }) === 1,
    win.starValueFromPoint(0, 25, { left: 10, width: 20 }));
  check('正中（ratio 恰好 0.5）-> 整星', win.starValueFromPoint(0, 20, { left: 10, width: 20 }) === 1,
    win.starValueFromPoint(0, 20, { left: 10, width: 20 }));
  check('第 5 颗右半 -> 5', win.starValueFromPoint(4, 25, { left: 10, width: 20 }) === 5,
    win.starValueFromPoint(4, 25, { left: 10, width: 20 }));
  check('第 1 颗贴着左边缘 -> 0.5', win.starValueFromPoint(0, 10, { left: 10, width: 20 }) === 0.5,
    win.starValueFromPoint(0, 10, { left: 10, width: 20 }));
  check('第 3 颗左半 -> 2.5', win.starValueFromPoint(2, 12, { left: 10, width: 20 }) === 2.5,
    win.starValueFromPoint(2, 12, { left: 10, width: 20 }));
  // jsdom 的 getBoundingClientRect 全返回 0，这里必须退化成整星而不是 NaN
  const zero = win.starValueFromPoint(0, 0, { left: 0, width: 0 });
  check('rect 宽度为 0 时退化成整星（不产生 NaN）', zero === 1 && !isNaN(zero), zero);

  console.log('\n[2] 词汇库：释义列必须 DOM 构建（XSS 红线）');
  check('表格渲染出 4 行', rows().length === 4, rows().length);
  const meaning = rows()[0].querySelector('.cell-meaning');
  check('释义文本原样保留（未被转义成实体、也未丢）',
    meaning.textContent.indexOf(XSS) >= 0, meaning.textContent.slice(0, 60));
  check('XSS 串没有变成 <img> 元素', meaning.querySelectorAll('img').length === 0,
    meaning.querySelectorAll('img').length + ' 个 img');
  //★ 判据收窄为「除了 index.html 静态自带的装饰图之外，没有注入出来的 img」。
  //  原写法是"整份文档 img 数 == 0"，但 index.html L79/L93 本来就带两张梵高画作
  //  （.rv[data-rail] 的背景装饰），与 XSS 无关 —— 拿它当判据等于永久假红，
  //  真红也会被一起淹没。基线 = 渲染表格之前文档里已有的 img 数，
  //  动态注入的才算数（这个数在表格渲染前就量好了，不受表格内容影响）。
  check('渲染表格没有注入新的 img（文档 img 数未增加）',
    win.document.querySelectorAll('img').length === BASELINE_IMGS,
    BASELINE_IMGS + ' -> ' + win.document.querySelectorAll('img').length);
  check('没有被执行的 onerror（window.__pwned 仍未定义）', win.__pwned === undefined, win.__pwned);
  check('释义正文用 textContent 写入而非 innerHTML（源码约束）',
    /meaningText\.textContent\s*=\s*entry\.meaning_zh/.test(appjs));
  check('释义正文有独立容器 .meaning-text（卡片主题对这层做截断）',
    /className\s*=\s*['"]meaning-text['"]/.test(appjs));
  check('关联链接有独立容器 .meaning-links（不混在正文文字流里）',
    /className\s*=\s*['"]meaning-links['"]/.test(appjs)
      && /appendMeaningLinks\(linksBox/.test(appjs));

  console.log('\n[2b] 走 innerHTML + esc() 的字段：XSS 串同样不得成为元素');
  const evilRow = rows()[3];
  check('词条名里的 XSS 没变成 img', evilRow.querySelector('.cell-term img') === null,
    evilRow.querySelectorAll('img').length + ' 个 img');
  check('词条名文本原样可见（被转义而非被删）',
    evilRow.querySelector('.cell-term').textContent.indexOf(XSS) >= 0,
    evilRow.querySelector('.cell-term').textContent.slice(0, 50));
  check('词性列的 <b> 没变成元素', evilRow.querySelectorAll('b').length === 0,
    evilRow.querySelectorAll('b').length + ' 个 b');

  // 例句 / 来源已移入详情页（需求 5），XSS 断言跟着页面一起搬过来
  evilRow.querySelector('[data-detail]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  check('详情页已打开', $('v-detail').hidden === false, $('v-detail').hidden);
  const dUsage = $('v-detail-usage');
  const dSource = $('v-detail-source');
  check('详情页例句的 XSS 没变成 img', dUsage.querySelectorAll('img').length === 0,
    dUsage.querySelectorAll('img').length);
  check('详情页来源的 XSS 没变成 img', dSource.querySelectorAll('img').length === 0,
    dSource.querySelectorAll('img').length);
  check('详情页例句文本原样可见', dUsage.textContent.indexOf(XSS) >= 0, dUsage.textContent.slice(0, 40));
  check('详情页来源文本原样可见', dSource.textContent.indexOf(XSS) >= 0, dSource.textContent.slice(0, 40));
  check('详情页里也没有 img、onerror 仍未执行',
    $('v-detail').querySelectorAll('img').length === 0 && win.__pwned === undefined,
    win.__pwned);
  $('v-detail-close').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(2);
  check('详情页能正常关闭', $('v-detail').hidden === true, $('v-detail').hidden);

  console.log('\n[3] 释义栏关联链接');
  const link = meaning.querySelector('a.meaning-link');
  check('释义列里有关联链接', !!link, link ? link.textContent : '无');
  const linksBox = meaning.querySelector('.meaning-links');
  check('链接在独立的 .meaning-links 容器里', !!linksBox && !!link && linksBox.contains(link),
    linksBox ? '容器存在' : '容器缺失');
  check('链接不在正文截断容器 .meaning-text 内（长释义不会把它吃掉）',
    !!link && !link.closest('.meaning-text'),
    link ? (link.closest('.meaning-text') ? '仍在正文内' : '已独立') : '无链接');
  check('链接文案形如「[近义词] ample」', link && link.textContent === '[近义词] ample', link && link.textContent);
  check('href 只含数字 id（不拼用户可控文本）', link && /^#entry-\d+$/.test(link.getAttribute('href')),
    link && link.getAttribute('href'));
  check('链接用 textContent 写入（源码约束）', /a\.textContent\s*=/.test(appjs));
  check('关联链接阻止默认跳转', /addEventListener\('click'[\s\S]{0,160}?preventDefault\(\)/.test(appjs));

  const before = state.calls.length;
  link.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  check('点击链接会去读该词条（复用现有打开逻辑）',
    state.calls.slice(before).some(function (c) { return c.method === 'GET' && /\/api\/entries\/2$/.test(c.url); }),
    state.calls.slice(before).map(function (c) { return c.method + ' ' + c.url; }).join(', '));
  check('打开的是详情页（只读），不是编辑框', $('v-detail').hidden === false, $('v-detail').hidden);
  $('v-detail-close').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(2);

  console.log('\n[4] 表格星级渲染：4.5 分 / 未评分 / 0 分，且是**只读**态');
  check('4.5 分 -> 前 4 颗满、第 5 颗半',
    JSON.stringify(starFills(ratingCell(0, 'importance'))) === JSON.stringify(['100%', '100%', '100%', '100%', '50%']),
    JSON.stringify(starFills(ratingCell(0, 'importance'))));
  check('3.0 分 -> 前 3 颗满',
    JSON.stringify(starFills(ratingCell(0, 'usefulness'))) === JSON.stringify(['100%', '100%', '100%', '0%', '0%']),
    JSON.stringify(starFills(ratingCell(0, 'usefulness'))));
  const unratedStars = ratingCell(1, 'importance').querySelector('.stars');
  check('未评分整组淡化（stars-unrated）', unratedStars.classList.contains('stars-unrated'),
    unratedStars.className);
  check('未评分一颗都不点亮（不能显示成 0 分）',
    JSON.stringify(starFills(ratingCell(1, 'importance'))) === JSON.stringify(['0%', '0%', '0%', '0%', '0%']),
    JSON.stringify(starFills(ratingCell(1, 'importance'))));
  check('0 分与未评分要区分开：0 分不带 stars-unrated',
    !ratingCell(2, 'importance').querySelector('.stars').classList.contains('stars-unrated'));

  // 需求 7：浏览时不能改分。表格星级必须是只读态，且点击不发任何写请求。
  check('表格星级是只读态（stars-readonly + aria-readonly）',
    unratedStars.classList.contains('stars-readonly') && unratedStars.getAttribute('aria-readonly') === 'true',
    unratedStars.className);
  check('只读态不渲染「清除」按钮',
    ratingCell(1, 'importance').querySelector('.stars-clear') === null);
  state.calls.length = 0;
  ratingCell(1, 'importance').querySelector('.star')
    .dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(4);
  check('★ 只读态点星星不发任何写请求（浏览时误改分这条需求被守住）',
    !state.calls.some(function (c) { return c.method === 'PATCH' || c.method === 'PUT'; }),
    state.calls.map(function (c) { return c.method + ' ' + c.url; }).join(', ') || '无写请求');

  console.log('\n[5] 编辑弹窗：可打分态是 defer 语义（点星不发请求，保存才提交）');
  rows()[1].querySelector('[data-edit]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  check('编辑弹窗已打开', $('v-modal').hidden === false, $('v-modal').hidden);
  check('编辑弹窗里有可打分星级（不是只读态）',
    !!$('f-importance').querySelector('.stars') &&
    !$('f-importance').querySelector('.stars').classList.contains('stars-readonly'));
  check('可打分态有「清除」按钮', !!$('f-usefulness').querySelector('.stars-clear'));
  state.calls.length = 0;
  $('f-importance').querySelector('.star')
    .dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(4);
  check('defer：点星只改本地显示（dataset.value 变了）',
    $('f-importance').querySelector('.stars').dataset.value === '1',
    $('f-importance').querySelector('.stars').dataset.value);
  check('★ defer：点星不发 PATCH、也不发 PUT',
    !state.calls.some(function (c) { return c.method === 'PATCH' || c.method === 'PUT'; }),
    state.calls.map(function (c) { return c.method + ' ' + c.url; }).join(', ') || '无写请求');

  // 只改了一维 -> 只带这一维（没改动过的维度绝不能被清成 null）
  state.calls.length = 0;
  $('f-save').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(8);
  const put = state.calls.filter(function (c) { return c.method === 'PUT'; })[0];
  check('点保存才发 PUT /api/entries/{id}', !!put && /\/api\/entries\/2$/.test(put.url),
    put ? put.method + ' ' + put.url : '无 PUT');
  check('★ 只带被改动的那一维（usefulness 没动就不带，绝不能被清成 null）',
    put && put.body.importance === 1 && !('usefulness' in put.body),
    put ? JSON.stringify(put.body) : '无');

  console.log('\n[6] 清除按钮 -> 显式 null（与"没带这个 key"是两件事）');
  // 用 1 号词条（importance 4.5 / usefulness 3.0，两个维度都有分值）。
  // 刻意**不用**未评分的词条：清空一个本来就是 null 的字段是 no-op（不带key），
  // 那种情况验不到"显式 null"这条语义。
  rows()[0].querySelector('[data-edit]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  check('编辑弹窗已打开（1 号词条，原 usefulness=3.0）', $('v-modal').hidden === false, $('v-modal').hidden);
  state.calls.length = 0;
  $('f-usefulness').querySelector('.stars-clear')
    .dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(4);
  check('清除把本地值置空（dataset.value 变成空串）',
    $('f-usefulness').querySelector('.stars').dataset.value === '',
    JSON.stringify($('f-usefulness').querySelector('.stars').dataset.value));
  check('★ 清除本身不发请求（defer：仍随表单一起提交）',
    !state.calls.some(function (c) { return c.method === 'PATCH' || c.method === 'PUT'; }),
    state.calls.map(function (c) { return c.method + ' ' + c.url; }).join(', ') || '无写请求');
  state.calls.length = 0;
  $('f-save').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(8);
  const putClear = state.calls.filter(function (c) { return c.method === 'PUT'; })[0];
  check('★ 清除提交的是显式 null（NULL≠0，后端靠 exclude_unset 区分）',
    putClear && putClear.body.usefulness === null,
    putClear ? JSON.stringify(putClear.body) : '无 PUT');
  check('★ 只带被清除的那一维，没动过的importance 不带 key',
    putClear && !('importance' in putClear.body),
    putClear ? JSON.stringify(Object.keys(putClear.body)) : '无 PUT');
  check('清除用的是按钮而不是右键菜单（webview 会吃掉右键）',
    /stars-clear/.test(appjs) && !/contextmenu/.test(appjs));

  // 反面：清空一个**本来就是未评分**的字段必须是 no-op（不带 key），
  // 否则会把"保持原值"误写成"清除"，是对未改动字段的误伤。
  rows()[1].querySelector('[data-edit]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  $('f-usefulness').querySelector('.stars-clear')
    .dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(2);
  state.calls.length = 0;
  $('f-save').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(8);
  const putNoop = state.calls.filter(function (c) { return c.method === 'PUT'; })[0];
  check('★ 清空「本来就是未评分」的字段是 no-op：请求里不带这个 key',
    putNoop && !('usefulness' in putNoop.body),
    putNoop ? JSON.stringify(Object.keys(putNoop.body)) : '无 PUT');

  console.log('\n[7] 关联库：块列表 + 画布');
  check('关联库面板有搜索 / 筛选 / 计数 / 新建 / 块容器',
    ['lk-search', 'lk-filters', 'lk-count', 'lk-add', 'lk-blocks'].every(function (id) { return !!$(id); }));
  const filterBtns = $('lk-filters').querySelectorAll('.lk-filter-btn');
  check('筛选键覆盖 全部/近似词/反义词/层级', filterBtns.length === 4,
    Array.prototype.map.call(filterBtns, function (b) { return b.dataset.filter; }).join(','));
  const blocks = $('lk-blocks').querySelectorAll('.lk-block');
  check('关联组渲染成块', blocks.length >= 1, blocks.length + ' 个块');
  check('块里出现成员词条名（XSS 词条名不产生 img）',
    $('lk-blocks').querySelectorAll('img').length === 0 &&
    $('lk-blocks').textContent.indexOf('abundant') >= 0);
  check('关系标签取白名单中文（未知值回退原值）',
    win.relationLabel('synonym') === '近义词' && win.relationLabel('antonym') === '反义词' &&
    win.relationLabel('parent') === 'parent',
    win.relationLabel('synonym') + '/' + win.relationLabel('antonym') + '/' + win.relationLabel('parent'));

  state.calls.length = 0;
  $('lk-add').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(6);
  check('点「增加关联」打开画布', $('cv-modal').hidden === false, $('cv-modal').hidden);
  check('画布标题是「新增关联」', $('cv-title').textContent === '新增关联', $('cv-title').textContent);

  // dissolved:true —— 移出后组内不足 2 人会连带解散整组，必须提示且刷新
  state.removeDissolved = true;
  state.calls.length = 0;
  $('cv-close') && $('cv-close').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(2);
  check('源码处理了 dissolved（移出成员可能连带解散整组）',
    /result && result\.dissolved/.test(appjs) && /连带解散/.test(appjs));

  console.log('\n[8] 样式：星级不可选中 + 未评分隐藏清除按钮 + 只读态');
  const css = fs.readFileSync(path.join(ROOT, 'web', 'style.css'), 'utf8');
  check('.stars 显式声明 user-select:none（拖选不会干扰点击）',
    /\.stars\s*\{[^}]*user-select:\s*none/.test(css));
  check('未评分时隐藏「清除」按钮', /\.stars-unrated\s+\.stars-clear\s*\{[^}]*display:\s*none/.test(css));
  check('.meaning-link 有虚线下划线样式', /\.meaning-link\s*\{[^}]*border-bottom:\s*1px dashed/.test(css));
  check('.star-fill 用 overflow:hidden 裁半星（不用图片/SVG）',
    /\.star-fill\s*\{[^}]*overflow:\s*hidden/.test(css) && !/\.star-fill[^}]*url\(/.test(css));
  check('只读态有独立样式（stars-readonly）', /\.stars-readonly\s*\{/.test(css));

  // =======================================================================
  // [9] ★ 本轮改动的风险路径：详情页 overwrite=true → 接受 → PATCH 落库
  // =======================================================================
  console.log('\n[9] ★ AI 建议：详情页 overwrite=true → 接受 → PATCH 落库');

  state.calls.length = 0;
  rows()[0].querySelector('[data-detail]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  check('详情页已打开（1 号词条，原分 4.5 / 3.0）', $('v-detail').hidden === false);
  $('v-detail-ai').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(10);
  const suggestCall = state.calls.filter(function (c) { return c.url === '/api/entries/ratings/suggest'; })[0];
  check('★ 详情页入口传 overwrite:true（再次点击 = 重新评估）',
    !!suggestCall && suggestCall.body.overwrite === true,
    suggestCall ? JSON.stringify(suggestCall.body) : '无请求');
  check('★ 只评估这一条（entry_ids 只有它自己）',
    !!suggestCall && Array.isArray(suggestCall.body.entry_ids) && suggestCall.body.entry_ids.length === 1 &&
    suggestCall.body.entry_ids[0] === 1,
    suggestCall ? JSON.stringify(suggestCall.body.entry_ids) : '无');
  check('确认面板已打开', $('ai-modal').hidden === false, $('ai-modal').hidden);
  const aiCards = function () { return win.document.querySelectorAll('#ai-list .ai-card'); };
  check('建议进了确认队列（未自动写入）', aiCards().length === 1, aiCards().length + ' 张');
  check('★ 拿到建议后没有任何 PATCH（AI 绝不自动落库）',
    !state.calls.some(function (c) { return c.method === 'PATCH'; }),
    state.calls.filter(function (c) { return c.method === 'PATCH'; }).length + ' 个 PATCH');
  check('had_rating=true 的条目给出「已有手动评分，接受将覆盖」提示',
    aiCards()[0].textContent.indexOf('接受将覆盖') >= 0,
    aiCards()[0].querySelector('.ai-card-warn') ? aiCards()[0].querySelector('.ai-card-warn').textContent : '无提示');

  // --- 接受 → PATCH 200 ---
  state.calls.length = 0;
  const acceptBtn = aiCards()[0].querySelector('.ai-ops .btn-primary');
  check('卡片上有「接受」按钮', !!acceptBtn, acceptBtn ? acceptBtn.textContent : '无');
  acceptBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(10);
  const accPatch = state.calls.filter(function (c) { return c.method === 'PATCH'; })[0];
  check('★ 点「接受」发出 PATCH /api/entries/1/ratings',
    !!accPatch && /\/api\/entries\/1\/ratings$/.test(accPatch.url),
    accPatch ? accPatch.method + ' ' + accPatch.url : '无 PATCH');
  check('★ PATCH body 是 AI 的建议值，且与旧手动分不同（证明真的覆盖）',
    !!accPatch && accPatch.body.importance === 2 && accPatch.body.usefulness === 3.5 &&
    accPatch.body.importance !== 4.5,
    accPatch ? JSON.stringify(accPatch.body) : '无');
  check('PATCH 成功后卡片标记为已写入',
    aiCards()[0].classList.contains('ai-card-done') && aiCards()[0].textContent.indexOf('已写入') >= 0,
    aiCards()[0].className);

  // --- PATCH 400：不能被当成已写入 ---
  $('ai-close').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(2);
  state.calls.length = 0;
  rows()[1].querySelector('[data-detail]').dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
  await tick(5);
  $('v-detail-ai').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(10);
  state.ratingsFail = true;
  state.calls.length = 0;
  const card2 = aiCards()[0];
  card2.querySelector('.ai-ops .btn-primary').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(10);
  const failPatch = state.calls.filter(function (c) { return c.method === 'PATCH'; })[0];
  check('★ 写失败时确实发了 PATCH（不是被本地挡住）', !!failPatch,
    failPatch ? failPatch.method + ' ' + failPatch.url : '无 PATCH');
  check('★ 400 时**不**标记已写入（失败不能被当成成功）',
    !card2.classList.contains('ai-card-done') && card2.textContent.indexOf('已写入') < 0,
    card2.className);
  check('★ 400 的中文原因进了 toast',
    $('toast').hidden === false && /写入失败|重要度应在/.test($('toast').textContent),
    $('toast').textContent);
  state.ratingsFail = false;

  // --- 全部接受：串行，且任一条失败不吞掉后续 ---
  $('ai-close').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(2);
  // 选中 3 条走批量入口。注意：前面 [4] 在行内点过星星，而星星不是 button/a，
  // 所以那一击**同时把该行选中了**（真实行为，不是 bug）。这里不假设初始状态，
  // 改成「点到达标为止」，让用例对前序副作用免疫。
  const selCount = function () {
    const m = /已选 (\d+)/.exec($('v-ai-selected').textContent || '');
    return m ? parseInt(m[1], 10) : 0;
  };
  check('行内点星星会连带选中该行（事件落在 span 上，不是 button/a）', selCount() >= 1, selCount());
  for (let guard = 0; guard < 12 && selCount() < 3; guard++) {
    const target = rows()[selCount() % 4];
    if (target) target.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await tick(1);
  }
  check('已选中 3 个词条', selCount() === 3, selCount() + ' 条');
  state.suggestItems = null;
  state.calls.length = 0;
  $('v-ai-selected').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(12);
  const batchCards = aiCards().length;
  check('批量入口把 3 条都铺进确认队列', batchCards === 3, batchCards + ' 张');
  check('批量入口传 overwrite:false（不重新评估）',
    state.calls.filter(function (c) { return c.url === '/api/entries/ratings/suggest'; })
      .every(function (c) { return c.body.overwrite === false; }));

  // 第一条 PATCH 失败 -> 后面的条目仍要发出去
  state.ratingsFailOnce = true;
  state.calls.length = 0;
  $('ai-accept-all').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await tick(30);
  const seq = state.calls.filter(function (c) { return c.method === 'PATCH' && /\/ratings$/.test(c.url); })
    .map(function (c) { return (c.url.match(/entries\/(\d+)\/ratings/) || [])[1]; });
  check('★ 全部接受是**逐条串行**（顺序与队列一致，不并发）',
    seq.join(',') === '1,2,3', seq.join(','));
  check('★ 第一条 400 之后，后续条目仍然发了 PATCH（失败不吞掉后续）',
    seq.length === 3, seq.length + ' 条');
  check('★ 串行接受后队列被关闭', $('ai-modal').hidden === true, $('ai-modal').hidden);

  console.log('\n[6] 网状层级块：源码契约（行为级证明在 probe_net_structure.js 真浏览器探针）');
  // ① 2026-10-27 之前 app.js 有一行
  //    showError('父子关系要从概念节点出发（真实词条只能作为子项）。')
  //    它把「surmount → insurmountable」这种链整个判死。真浏览器探针证明摘掉
  //    之后这条链建得出来（旧代码下同一探针 8/12 FAIL）；这里防的是有人把它加回去。
  check('★ 「词条不能当父」的硬拦没有回来',
    appjs.indexOf('父子关系要从概念节点出发') < 0);
  check('★ 近义连线走 joinRelationGroup（3 人组才造得出来，不再恒发 2 个 id）',
    appjs.indexOf('function joinRelationGroup') >= 0
    && appjs.indexOf('joinRelationGroup(a, b, linking.relation)') >= 0);
  // ② /api/entries/{id}/links 返回的是扁平自连接行（一行一个同伴、不含自己），
  //    不是组。不按 link_id 聚合并把 a 补回成员表，PUT 的全量替换语义会把 a
  //    从它自己所在的组里删掉。
  check('joinRelationGroup 按 link_id 聚合扁平行（并补回 a 自己）',
    /byLink\[gid\]/.test(appjs) && /ids: \[a\.id\]/.test(appjs));
  check('层级画布消费 tree 响应的 links（重开画布后横向虚线才还在）',
    appjs.indexOf('drawTreeLinks(data && data.links)') >= 0);
  // ③ 放射布局半径随深度递增：三层链（克服 → surmount → insurmountable）
  //    会把第三层算到 y=-90，直接被画布顶边裁掉。修法是整树平移归一。
  check('★ 放射布局有负坐标归一（三层链不再被顶边裁掉）',
    appjs.indexOf('40 - minY') >= 0 && appjs.indexOf('40 - minX') >= 0);

  const passed = results.filter(function (r) { return r.ok; }).length;
  const failed = results.length - passed;
  console.log('\n' + '='.repeat(60));
  console.log('PASS=' + passed + '  FAIL=' + failed + '  TOTAL=' + results.length);
  if (failed) {
    console.log('\n失败项：');
    results.forEach(function (r) { if (!r.ok) console.log('  - ' + r.name); });
  }
  process.exit(failed ? 1 : 0);
}

main().catch(function (err) {
  console.error('\n测试自身异常：', err);
  process.exit(1);
});