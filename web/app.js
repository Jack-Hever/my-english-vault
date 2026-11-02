// P0：模块切换与后端连通性检查。

/**
 * 切换到指定模块。
 *
 * ★ 为什么要抽成函数而不是各写各的 ★
 * 「学习进度」有两个入口：顶栏 Tab（data-module="analytics"）和
 * 顶栏右上角的按钮（#stats-open）。两个入口必须**状态完全一致**：
 * 从按钮进去，Tab 也要高亮；从 Tab 进去，按钮也要有按下态。
 * 各写一份切换逻辑迟早会出现"两边不同步"，所以统一走这里。
 *
 * 顺带解决一个老问题：原来的切换代码假定 panel 一定存在，
 * 一旦某个 Tab 找不到对应 panel 就会抛异常中断后续所有绑定。
 * 现在找不到时直接返回，不影响其他监听器。
 */
/**
 * 各模块的大标题（eyebrow + 巨大中文标题）。
 * 文案逐字取自 design-explore 各候选原型的 .phead：
 *   星月夜 → Practice · 翻译练习 / 翻译练习
 *   向日葵 → Practice · 翻译练习 / 今日朝向
 *   蒙娜丽莎 → Practice · 翻译练习 / 句子悬在暗处
 *   吸血鬼 → Practice · 翻译练习 / 今日手记
 *   漫威   → Practice · 翻译练习 / 把生词放回句子里
 *   关联库 → Links · 关联库 / 词与词的连接
 *   学习进度 → Progress · 学习进度 / 30 天趋势
 * 朴素(reading) 与 克制(vesper) 不注入 —— 它们的顶栏就是设计表达，不加标题。
 *
 * ★ 星月夜那行原本是「回译练习」，术语统一后与 eyebrow 里的「翻译练习」重复；
 *   PAGE_TITLES 实际是**全主题共用**的一套标题（主题只改配色、不改文案），
 *   上面这些差异化表述是设计原型里的原始文案，保留备查。
 */
var PAGE_TITLES = {
  practice: { eyebrow: 'Practice · 翻译练习', main: '翻译练习' },
  vocab:    { eyebrow: 'Vocabulary · 词汇库', main: '我的词库' },
  articles: { eyebrow: 'Articles · 外刊库', main: '外刊选句' },
  links:    { eyebrow: 'Links · 关联库', main: '词与词的连接' },
  analytics:{ eyebrow: 'Progress · 学习进度', main: '30 天趋势' },
};

/**
 * 注入/更新当前面板的大标题。
 * ★ 朴素与克制不注入 ★ —— 这两个方向的原有外观一行都不能动。
 * 标题容器建在 .panel 的**第一个子位置**（不是 append 到末尾），
 * 否则会落在工具栏下面，视觉上「大标题」跑到筛选器后面去了。
 */
var titleHost = null;
function ensureTitleHost(panel) {
  if (!panel) { return null; }
  var t = panel.querySelector(':scope > .page-title');
  if (!t) {
    t = document.createElement('div');
    t.className = 'page-title';
    panel.insertBefore(t, panel.firstChild);
  }
  return t;
}

function applyPageTitle(module) {
  var panel = document.getElementById('panel-' + module);
  if (!panel) { return; }
  var theme = document.documentElement.getAttribute('data-theme');
  // 朴素 / 克制：保持原样，把可能存在的标题移除
  if (theme === 'reading' || theme === 'vesper') {
    var old = panel.querySelector(':scope > .page-title');
    if (old && old.parentNode) { old.parentNode.removeChild(old); }
    return;
  }
  var spec = PAGE_TITLES[module];
  if (!spec) { return; }
  var host = ensureTitleHost(panel);
  if (!host) { return; }
  host.textContent = '';
  var eb = document.createElement('span');
  eb.className = 'pt-eyebrow';
  eb.textContent = spec.eyebrow;
  var mn = document.createElement('span');
  mn.className = 'pt-main';
  mn.textContent = spec.main;
  host.appendChild(eb);
  host.appendChild(mn);
}

/**
 * 给**所有**面板应用大标题。
 * ★ 为什么不能只挂 switchModule ★
 *   首屏时 panel-practice 在 HTML 里就带着 .active，不走 switchModule；
 *   而 applyTheme（用户点色板切主题）也不走 switchModule ——
 *   两条路径都不经过 applyPageTitle，标题就永远不出现（实测 5 个方向全部缺失）。
 *   所以这里做成「对所有面板都刷一遍」，两个入口都调它。
 */
function applyAllPageTitles() {
  Object.keys(PAGE_TITLES).forEach(function (mod) {
    applyPageTitle(mod);
  });
}

function switchModule(module) {
  var panel = document.getElementById('panel-' + module);
  if (!panel) { return; }
  document.querySelectorAll('.tab').forEach(function (b) {
    b.classList.toggle('active', b.dataset.module === module);
  });
  document.querySelectorAll('.panel').forEach(function (p) {
    p.classList.remove('active');
  });
  panel.classList.add('active');
  applyAllPageTitles();
  // 学习进度是按需拉取的数据页：第一次进入才请求，避免拖慢启动
  if (module === 'analytics' && typeof window.EnPracticeAnalytics !== 'undefined') {
    window.EnPracticeAnalytics.onEnter();
  }
  if (typeof window.EnPracticeAnalytics !== 'undefined' && module !== 'analytics') {
    window.EnPracticeAnalytics.onLeave();
  }
}

document.querySelectorAll('.tab').forEach(function (btn) {
  btn.addEventListener('click', function () {
    switchModule(btn.dataset.module);
  });
});

// 顶栏右上角的「学习进度」按钮（用户指定的位置：设置按钮之前）
(function bindStatsEntry() {
  var btn = document.getElementById('stats-open');
  if (!btn) { return; }
  btn.addEventListener('click', function () {
    switchModule('analytics');
  });
})();

(function init() {
  // ★ 先同步建好挂载点与 ready 门闩 ★
  //   下面的 /api/app-info 是异步的，而首屏提示与设置弹窗的模块 IIFE 是
  //   同步执行的 —— 谁先跑完不确定。所以给它们一个门闩：ready 兑现之前，
  //   各模块先别急着按「云端」渲染。这不是过度设计：把形态判断散到各模块里
  //   猜时序，早晚会出现「提示已按云端渲染、密钥却是桌面读 settings.json」的
  //   组合bug —— 而那正好是密钥泄露的路径。
  window.EnPracticeCloud = window.EnPracticeCloud || {};
  window.EnPracticeCloud.info = { mode: 'desktop' };
  window.EnPracticeCloud.isCloud = false;
  window.EnPracticeCloud.networkFetch = true;
  window.EnPracticeCloud.apiKey = '';
  // ★ 访客自填的接口地址 / 模型 / 超时（云端用；与 apiKey 同构）★
  //   只从 localStorage 读，绝不来自服务端回显。
  window.EnPracticeCloud.aiBaseUrl = '';
  window.EnPracticeCloud.aiModel = '';
  window.EnPracticeCloud.aiTimeout = '';
  window.EnPracticeCloud.ready = new Promise(function (resolve) {
    window.EnPracticeCloud._resolveReady = resolve;
  });

  function finishCloudInfo(info) {
    var c = window.EnPracticeCloud;
    c.info = info || { mode: 'desktop', network_fetch: true };
    c.isCloud = !!(info && info.mode === 'cloud');
    c.networkFetch = !!(info && info.network_fetch);
    try {
      c.apiKey = localStorage.getItem('enp_ai_key') || '';
      c.aiBaseUrl = localStorage.getItem('enp_ai_base_url') || '';
      c.aiModel = localStorage.getItem('enp_ai_model') || '';
      c.aiTimeout = localStorage.getItem('enp_ai_timeout') || '';
    } catch (e) {
      // 隐私模式禁 localStorage：降级为"没配密钥"，不报错
      c.apiKey = '';
      c.aiBaseUrl = '';
      c.aiModel = '';
      c.aiTimeout = '';
    }
    document.dispatchEvent(new CustomEvent('enp:cloud-info'));
    c._resolveReady(c);
  }

  fetch('/api/health')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      document.getElementById('status').textContent = d.ok ? '已连接' : '后端异常';
      // ★ 云端不返回 db_path（那是服务器绝对路径，泄露目录结构 + 访客 id）。
      //   桌面版继续显示本地路径 —— 那对用户有用，行为不变。
      var dbEl = document.getElementById('dbpath');
      if (!dbEl) return;
      dbEl.textContent = d.db_path ? ('数据文件：' + d.db_path) : '数据已隔离存储';
    })
    .catch(function () {
      document.getElementById('status').textContent = '后端未响应';
    });

  fetch('/api/stats')
    .then(function (r) { return r.json(); })
    .then(function (d) { renderStats(d); })
    .catch(function () {});

  // ★ 部署形态（云端 / 桌面）★
  //   /api/health 只在云端才带 mode，桌面不带 —— 那个接口的桌面返回值得逐字不变，
  //   所以形态单独走 /api/app-info。首屏提示与设置弹窗都要按它分叉。
  fetch('/api/app-info')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(finishCloudInfo)
    .catch(function () { finishCloudInfo(null); });
})();

function renderStats(d) {
  document.getElementById('counts').textContent =
    '条目 ' + d.entries + ' · 素材 ' + d.passages + ' · 练习 ' + d.practices + ' · 文章 ' + d.articles;
}

function refreshStats() {
  // 此处有意静默：统计数字只是底部装饰信息，刷新失败不影响任何功能，
  // 弹提示反而打扰用户。保持静默但保留注释——勿改成"无注释的静默"，也勿改成 toast。
  return fetch('/api/stats').then(function (r) { return r.json(); }).then(renderStats).catch(function () {});
}

// ---------------------------------------------------------------------------
// 公共工具（全局作用域，vocab / articles / practice 三个模块共用）
// 注意：不要把这些函数再塞回某个模块的 IIFE，否则其它模块引用不到。
// ---------------------------------------------------------------------------

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/**
 * 导出完成后的提示文案。
 *
 * ★ 为什么桌面端和云端说法不同 ★
 * 云端跑在真浏览器里：blob + <a download> 直接落进浏览器的下载目录，
 * 点了就有文件，提示「已导出」是准确的。
 * 桌面端跑在 pywebview 里：ALLOW_DOWNLOADS 打开后，pywebview 会先弹一个
 * 系统「另存为」对话框，**文件落在用户自己选的位置**，默认名由对话框决定。
 * 这时说「已导出」会让人以为文件已经在某个固定目录里了 —— 而用户真正
 * 需要知道的是「接下来有个保存对话框，选好位置就落盘」。
 */
function exportToast(name, extra) {
  var cloud = !!(window.EnPracticeCloud && window.EnPracticeCloud.isCloud);
  var head = cloud
    ? ('已导出 ' + name + (extra ? ('（' + extra + '）') : ''))
    : ('已生成 ' + name + (extra ? ('（' + extra + '）') : '') + '，请在弹出的保存对话框中选择位置');
  toast(head, 'ok');
}

/**
 * 下载一个「文件型」接口的返回（导出 Markdown / Word 用）。
 *
 * ★ 为什么不复用 api() ★
 * api() 只按 JSON 解析响应体；导出接口返回的是文件字节，必须走 fetch +
 * blob 才能落成文件。
 *
 * ★ 文件名从哪来 ★
 * 服务端在 Content-Disposition 里给出最终文件名（Word 用文章标题派生，
 * 含中文），所以这里从响应头解析；解析不到就用调用方给的兜底名。
 */
function downloadFile(url, fallbackName) {
  return fetch(url, { credentials: 'same-origin' })
    .then(function (res) {
      if (!res.ok) {
        return res.text().then(function (text) {
          var detail = '';
          try { detail = JSON.parse(text).detail || ''; } catch (e) { detail = ''; }
          throw new Error(detail || ('HTTP ' + res.status));
        });
      }
      var name = filenameFromDisposition(res.headers.get('Content-Disposition'), fallbackName);
      return res.blob().then(function (blob) { return { blob: blob, name: name }; });
    })
    .then(function (r) {
      var link = document.createElement('a');
      link.href = URL.createObjectURL(r.blob);
      link.download = r.name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
      exportToast(r.name);
    })
    .catch(function (err) { toast('导出失败：' + err.message, 'error'); });
}

/** 从 Content-Disposition 里取文件名。
 *  优先 RFC 5987 的 filename*（含中文时靠它才不乱码），其次普通 filename，
 *  都取不到就用 fallback。 */
function filenameFromDisposition(disposition, fallback) {
  if (!disposition) return fallback;
  var star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''));
    } catch (e) { /* 编码坏了就落到下面的普通 filename */ }
  }
  var plain = /filename\s*=\s*"([^"]*)"/i.exec(disposition);
  if (plain && plain[1]) return plain[1];
  return fallback;
}

/**
 * 洗出一个能安全放进 href 的 URL：只放行 http/https（含协议相对 //），
 * 其余一律返回空串。
 *
 * ★ 为什么 esc() 之外还要这一层 ★
 * esc() 只转义 HTML 元字符，不过滤协议 —— 用户在文章链接栏填一个
 * javascript: 开头的值，转义后照样是个能点击的执行入口。渲染侧（列表的
 * 「原文」链接）与表单侧（保存前校验）都走它，两层都放。
 */
function safeUrl(u) {
  var s = String(u == null ? '' : u).trim();
  return /^(https?:)?\/\//i.test(s) ? s : '';
}

/**
 * 把一个值洗成能安全放进 HTTP 请求头的 ASCII 串。
 *
 * ★ 为什么必须洗 ★
 * fetch 的请求头值只能是 ByteString —— 塞进非 ASCII 字符会直接抛
 * TypeError（"Failed to execute 'fetch'... Invalid value"），整个请求发不出去。
 * 而访客可能在接口地址里粘贴中文域名、在模型名里粘贴奇怪字符。
 * ⇒ 这里剔掉所有非 ASCII 字符与控制字符，剩下的才是能安全发出去的。
 *   服务端还有一层校验（app/egress.py），这里是第一层，避免请求根本发不出去。
 */
function asciiSafe(v) {
  return String(v == null ? '' : v).replace(/[^\x20-\x7E]/g, '').trim();
}

/** 统一请求封装：把后端 detail 转成可读错误，失败直接抛 Error。 */
function api(url, options) {
  var opts = options || {};
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  // ★ 云端：把访客自带的 AI 密钥放在请求头里带上去 ★
  //   密钥只存在浏览器 localStorage，绝不写进 /api/settings（那是服务端磁盘）。
  //   这里只加一处 —— 全站所有请求都走 api()，不存在"某个模块忘了加"的可能。
  var hk = window.EnPracticeCloud && window.EnPracticeCloud.apiKey;
  if (hk && !opts.headers['X-Enp-Api-Key']) {
    opts.headers['X-Enp-Api-Key'] = hk;
  }
  // ★ 云端：访客自填的「接口地址 / 模型 / 超时」同样走请求头 ★
  //   与密钥完全同构：三者都只存在本浏览器 localStorage，随请求头带给服务器，
  //   服务端**不落盘**（磁盘上不留任何访客指定的出网地址）。
  //   ★ 为什么必须走请求头、而不是 PUT /api/settings ★
  //     落盘 = 把访客指定的出网地址长期留在服务器上；请求头 = 只活在这一次请求。
  var c = window.EnPracticeCloud;
  if (c) {
    if (c.aiBaseUrl && !opts.headers['X-Enp-Ai-Base-Url']) {
      opts.headers['X-Enp-Ai-Base-Url'] = asciiSafe(c.aiBaseUrl);
    }
    if (c.aiModel && !opts.headers['X-Enp-Ai-Model']) {
      opts.headers['X-Enp-Ai-Model'] = asciiSafe(c.aiModel);
    }
    if (c.aiTimeout && !opts.headers['X-Enp-Ai-Timeout']) {
      opts.headers['X-Enp-Ai-Timeout'] = asciiSafe(c.aiTimeout);
    }
  }
  return fetch(url, opts).then(function (res) {
    return res.text().then(function (text) {
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
      if (!res.ok) {
        var detail = data && data.detail;
        var message;
        if (Array.isArray(detail)) {
          message = detail.map(function (item) {
            return item && item.msg ? item.msg : String(item);
          }).join('；');
        } else if (detail) {
          message = String(detail);
        } else {
          message = '请求失败（HTTP ' + res.status + '）';
        }
        var err = new Error(message);
        err.status = res.status;
        err.data = data;
        throw err;
      }
      return data;
    });
  });
}

/** 全局轻提示。kind: 'ok' | 'error' | 空。 */
/**
 * 顶部轻提示。
 * ★ ms 参数为什么要有 ★
 *   默认 2600ms 适合「已保存」这种一眼就懂的话。
 *   但有的提示**必须让用户读完** —— 比如「已删除你的全部数据，
 *   此处将保持为空库」：这句话要是不给人读完的时间，
 *   页面 reload 一下就没了，用户只看到一闪而过，然后刷新页面发现
 *   「数据又有了」，反而以为没删成功。所以那种场合要显式传长时长。
 *   可选参数，不影响任何现有两参调用。
 */
function toast(msg, kind, ms) {
  var el = $('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' toast-' + kind : '');
  el.hidden = false;
  clearTimeout(toast._t);
  var dur = (typeof ms === 'number' && ms > 0) ? ms : 2600;
  toast._t = setTimeout(function () { el.hidden = true; }, dur);
}

/**
 * 通用确认弹窗。vocab 的删除有自己的 pendingDeleteId 流程，
 * 这里另开一个通用实例，避免两个模块抢同一个 state。
 */
var confirmBusy = false;
function openConfirm(text, onOk) {
  confirmBusy = true;
  $('g-text').textContent = text;
  $('g-modal').hidden = false;
  $('g-ok').onclick = function () {
    closeConfirm();
    if (onOk) onOk();
  };
}

function closeConfirm() {
  confirmBusy = false;
  $('g-modal').hidden = true;
  $('g-ok').onclick = null;
}

// 通用确认弹窗的取消（vocab 的 d-modal 有自己的关闭流程，不受影响）
document.addEventListener('DOMContentLoaded', function () {
  $('g-cancel').addEventListener('click', closeConfirm);
});

// ---------------------------------------------------------------------------
// P2：关联关系白名单 + 评分星级控件（全局作用域：词汇库列表与关联库共用）
// ---------------------------------------------------------------------------

/**
 * 关联关系白名单。与后端 app/links.py 的 RELATIONS 一一对应。
 *
 * short 是画布线上标记方块里写的字（SYM / OPP），cls 是线型 class：
 * 三种关系的**颜色和形状都不同**，不靠颜色单独区分（色弱用户也要能分辨）。
 * 写成数组而不是散落的 if：后端加关系时这里加一行即可，渲染逻辑不动。
 */
var LINK_RELATIONS = [
  { value: 'synonym', label: '近义词', short: 'SYM', cls: 'cv-line-syn' },
  { value: 'antonym', label: '反义词', short: 'OPP', cls: 'cv-line-ant' },
];

/** 关系值 → 中文标签；未知值原样返回（后端若先加了新关系，前端不至于显示空白）。 */
function relationLabel(value) {
  for (var i = 0; i < LINK_RELATIONS.length; i++) {
    if (LINK_RELATIONS[i].value === value) return LINK_RELATIONS[i].label;
  }
  return value || '';
}

/**
 * 按鼠标横坐标相对星形的位置算取值：点左半 = 半星，右半 = 整星。
 * starIndex 从 0 开始，所以第 1 颗星的取值是 0.5 或 1。
 * 抽成纯函数（不碰 DOM）是为了能在 node 里用假 rect 直接测左右半分支。
 */
function starValueFromPoint(starIndex, clientX, rect) {
  if (!rect || !rect.width) return starIndex + 1;
  var ratio = (clientX - rect.left) / rect.width;
  return ratio < 0.5 ? starIndex + 0.5 : starIndex + 1;
}

/**
 * 造一个五星控件（0 / 0.5 / 1 … 5）。
 *
 * 半星用「覆盖层按百分比裁剪宽度」实现：每颗星是 base + fill 两层 ★，
 * fill 绝对定位、宽度 100% / 50% / 0%，配 overflow:hidden 裁出半颗。
 * 不用图片也不用 SVG —— 本项目离线可用、无外部依赖是硬约束。
 *
 * 三种模式（需求 7：星级只在编辑态可打分，其余场景只读展示）：
 *   - readOnly: true —— 纯展示。不绑 mousemove / click、不渲染「清除」按钮、
 *     光标 default、不进 tab 序列（连 tabindex 都不给）。点它没反应。
 *   - defer: true    —— 可打分但**不立即发请求**（词条编辑弹窗用）。点击只改
 *     本地显示并回调 onChange，由所在表单随保存一起提交。避免评分与正文
 *     分两次写：中途失败会留下半新半旧的条目。
 *   - 默认            —— 可打分且立即 PATCH /api/entries/{id}/ratings。
 *
 * 三者共用同一个函数的分支，而不是各写一份：半星裁剪、paint 逻辑、
 * 未评分态与流光样式的 class 挂载点必须完全一致，复制两份必然随时间漂移。
 *
 * options: { entryId, field: 'importance'|'usefulness', value: number|null,
 *            readOnly: true|false, defer: true|false,
 *            onChange: function(value), onCommitted: function(entry) }
 */
function buildStars(options) {
  var TOTAL = 5;
  var readOnly = options.readOnly === true;
  var defer = options.defer === true;
  var el = document.createElement('span');
  el.className = readOnly ? 'stars stars-readonly' : 'stars';
  el.dataset.field = options.field;
  if (readOnly) el.setAttribute('aria-readonly', 'true');
  var current = (options.value === null || options.value === undefined)
    ? null : Number(options.value);
  var busy = false;

  /** 按当前值画星星；value 为 null 时全部不点亮并整组淡化（未评分 ≠ 打了 0 分）。 */
  function paint(value) {
    var fills = el.querySelectorAll('.star-fill');
    for (var i = 0; i < fills.length; i++) {
      var part = (value === null) ? 0 : value - i;
      // 只取 100 / 50 / 0 三档：评分步进是半星，不存在 25% 这种中间态
      fills[i].style.width = part >= 1 ? '100%' : (part >= 0.5 ? '50%' : '0%');
    }
    if (value === null) el.classList.add('stars-unrated');
    else el.classList.remove('stars-unrated');
    el.dataset.value = (value === null) ? '' : String(value);
    el.title = (value === null) ? '未评分' : String(value) + ' 分';
  }

  var layer = document.createElement('span');
  layer.className = 'stars-layer';
  for (var i = 0; i < TOTAL; i++) {
    var star = document.createElement('span');
    star.className = 'star';
    star.dataset.index = String(i);
    var base = document.createElement('span');
    base.className = 'star-base';
    base.textContent = '★';
    var fill = document.createElement('span');
    fill.className = 'star-fill';
    fill.textContent = '★';
    star.appendChild(base);
    star.appendChild(fill);
    layer.appendChild(star);
    // 只读态不绑任何交互，也不给 tabindex —— 不可聚焦的只读展示不该占 Tab 序列
    if (!readOnly) bindStar(star, i);
  }
  el.appendChild(layer);

  var hint = document.createElement('span');
  hint.className = 'stars-hint';
  el.appendChild(hint);

  // 「清除」是可打分态的专属控件：只读态压根不渲染它，免得误导用户以为能改
  if (!readOnly) {
    var clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'stars-clear';
    clear.textContent = '清除';
    // 不用右键菜单：webview 里右键可能被宿主窗口吃掉，点了没反应
    clear.addEventListener('click', function () { send(null); });
    el.appendChild(clear);
    el.addEventListener('mouseleave', function () { if (!busy) paint(current); });
  }

  function bindStar(star, index) {
    star.addEventListener('mousemove', function (e) {
      if (busy) return;
      paint(starValueFromPoint(index, e.clientX, star.getBoundingClientRect()));
    });
    star.addEventListener('click', function (e) {
      if (busy) return;
      send(starValueFromPoint(index, e.clientX, star.getBoundingClientRect()));
    });
  }

  function send(value) {
    // 惰性模式：只更新本地显示并通知外层，绝不发请求。
    // 评分随编辑表单的保存一起提交（PUT /api/entries/{id}），
    // 这样评分与正文要么都成功、要么都不改，不会留下半新半旧的条目。
    if (defer) {
      current = (value === null || value === undefined) ? null : Number(value);
      paint(current);
      if (options.onChange) options.onChange(current);
      return;
    }
    busy = true;
    el.classList.add('stars-busy');
    var body = {};
    body[options.field] = value; // 显式 null = 清除；后端用 exclude_unset 区分「没传」
    api('/api/entries/' + options.entryId + '/ratings', {
      method: 'PATCH',
      body: JSON.stringify(body)
    }).then(function (entry) {
      current = (entry[options.field] === null || entry[options.field] === undefined)
        ? null : Number(entry[options.field]);
      paint(current);
      if (options.onCommitted) options.onCommitted(entry);
    }).catch(function (err) {
      paint(current);          // 失败要回滚到原值，不能停在预览态
      toast(err.message, 'error');
    }).then(function () {
      busy = false;
      el.classList.remove('stars-busy');
    });
  }

  paint(current);
  return el;
}

// ---------------------------------------------------------------------------
// P1：词汇库
// 原生 JS，无构建步骤、无外部依赖，离线可用。
// ---------------------------------------------------------------------------
(function vocab() {
  'use strict';

  var PAGE_SIZE = 20;

  var state = {
    q: '',
    type: '',
    dateFrom: '',
    dateTo: '',
    page: 1,
    total: 0,
    editingId: null, // 表单当前编辑的条目 ID，null 表示新增
    detailId: null,  // 详情页当前展示的条目 ID，null 表示已关闭
    // 编辑弹窗里两个评分控件的「打开时原值」。只有与原值不同的那一维
    // 才会进提交体，避免误清评分。详见 applyFormRatings。
    formRatings: null,
    pendingDeleteId: null,
    saving: false,   // 保存中：防止双击「保存」/回车+点击造成并发提交
    importing: false, // 导入中：批量导入会长时间持有写锁，期间禁止保存
    // 是否有保存请求在飞行中。为 true 时禁止 setSaving(false) 提前解锁：
    // openForm() 会复位保存按钮，若无此标志，用户在请求未回来时
    // 「取消 → 再点新增条目」就能重开闸门、连发第二次 POST。
    inflight: false,
    // ---- 需求 4：多选 + AI 评分 ----
    // 选中的词条 id 集合。用数组而非 Set：这套代码是 ES5 风格（var + function），
    // Set 虽然现代浏览器都有，但保持与文件其余部分同一语言水平。
    selected: [],
    // AI 建议队列：[{entry_id, term, importance, usefulness, reason, skipped, error}]
    aiQueue: [],
    // 队列里当前正在确认的下标；-1 表示没有待确认项
    aiIndex: -1,
    // 批量评估的控制标志。aborted 只在批与批之间检查——单次请求已发出时
    // 强行 abort 反而可能让后端算完这次却拿不到结果，白花钱。
    aiBatchRunning: false,
    aiBatchAbort: false,
    // 确认面板的语义模式：'batch'（批量，跳过已评分）/ 'single'（详情页重新评估）
    aiMode: 'batch'
  };

  /** 选中项是否在集合里（严格数值比较，id 一律 parseInt 过）。 */
  function isSelected(id) {
    return state.selected.indexOf(id) >= 0;
  }

  function toggleRowSelect(id) {
    var at = state.selected.indexOf(id);
    if (at >= 0) state.selected.splice(at, 1);
    else state.selected.push(id);
    syncSelectionUI();
  }

  /** 选中态的任何可见痕迹都从这里出，避免多处各改一遍导致不同步。 */
  function syncSelectionUI() {
    Array.prototype.forEach.call(document.querySelectorAll('#v-body tr[data-id]'), function (tr) {
      var id = parseInt(tr.dataset.id, 10);
      if (isSelected(id)) tr.classList.add('row-selected');
      else tr.classList.remove('row-selected');
    });
    var n = state.selected.length;
    $('v-selcount').hidden = n === 0;
    $('v-selcount').textContent = '已选 ' + n + ' 条';
    $('v-ai-selected').disabled = n === 0;
    $('v-ai-selected').textContent = 'AI 评分（已选 ' + n + '）';
  }

  // ---------- 工具 ----------

  function queryString() {
    var parts = [];
    if (state.q) parts.push('q=' + encodeURIComponent(state.q));
    if (state.type) parts.push('type=' + encodeURIComponent(state.type));
    if (state.dateFrom) parts.push('date_from=' + encodeURIComponent(state.dateFrom));
    if (state.dateTo) parts.push('date_to=' + encodeURIComponent(state.dateTo));
    parts.push('page=' + state.page);
    parts.push('page_size=' + PAGE_SIZE);
    return parts.join('&');
  }

  function formatTime(value) {
    if (!value) return '';
    return String(value).replace('T', ' ').slice(0, 16);
  }

  // ---------- 列表 ----------

  function loadEntries() {
    $('v-loading').hidden = false;
    return api('/api/entries?' + queryString()).then(function (data) {
      state.total = data.total;
      renderRows(data.items);
      renderPager();
      // 列表重绘后 tr 是全新的，row-selected class 会丢，
      // 必须重新同步一次选中态，否则"选中 → 翻页/搜索回来"选中就消失了
      syncSelectionUI();
      $('v-loading').hidden = true;
      $('v-empty').hidden = data.total !== 0;
      $('v-count').textContent = '共 ' + data.total + ' 条';
    }).catch(function (err) {
      $('v-loading').hidden = true;
      toast('列表加载失败：' + err.message, 'error');
    });
  }

  function itemById(items, id) {
    for (var i = 0; i < items.length; i++) {
      if (items[i].id === id) return items[i];
    }
    return null;
  }

  /**
   * 释义列：**必须 DOM 构建**。
   * 释义是用户自由填写的，用 innerHTML 拼字符串时，里面带
   * `<img src=x onerror=...>` 会被解析成真元素并立刻执行。
   * 所以文本走 createTextNode、链接走 createElement('a') + textContent，
   * href 里只拼数字 id，不拼任何用户可控字符串。
   */
  function fillMeaningCell(cell, entry) {
    // ★ 释义正文与关联链接**分层**（用户反馈：卡片主题下中文释义一长，
    //   混在文字流尾部的链接被截断区吃掉，根本看不见）。
    //   正文包进 .meaning-text（卡片主题只对这一层做行数截断），
    //   链接容器 .meaning-links 独立成块（卡片主题里是释义下方的一行，
    //   永不随正文截断；朴素/克制的原生表格里保持行内流，见 style.css）。
    //   两层依旧全 DOM 构建：释义与链接文案都是用户可控文本，绝不 innerHTML。
    cell.textContent = '';
    var meaningText = document.createElement('span');
    meaningText.className = 'meaning-text';
    meaningText.textContent = entry.meaning_zh || '';
    cell.appendChild(meaningText);
    var linksBox = document.createElement('span');
    linksBox.className = 'meaning-links';
    cell.appendChild(linksBox);
    appendMeaningLinks(linksBox, entry.id);
  }

  /** 关联词条链接随后异步补上：先让释义本身立刻可见，链接到了再追加。 */
  function appendMeaningLinks(cell, entryId) {
    cell.dataset.linksFor = String(entryId);
    api('/api/entries/' + entryId + '/links')
      .then(function (data) {
        var links = (data && data.links) || [];
        if (!links.length) return;
        links.forEach(function (item) {
          cell.appendChild(document.createTextNode(' '));
          cell.appendChild(buildMeaningLink(item));
        });
      })
      .catch(function () {
        // 有意静默：关联词条只是释义列的附加信息，拉取失败不该弹窗打断看列表。
        // 保留注释——勿改成「无注释的 catch」。
      });
  }

  function buildMeaningLink(item) {
    var a = document.createElement('a');
    a.className = 'meaning-link';
    var id = parseInt(item.entry && item.entry.id, 10);
    a.href = '#entry-' + id; // href 只含数字，不含用户可控文本
    a.textContent = '[' + relationLabel(item.relation) + '] ' + ((item.entry && item.entry.term) || '');
    a.addEventListener('click', function (e) {
      e.preventDefault();
      // 跳**详情页**而不是编辑弹窗：浏览近义词时误开编辑窗正是需求 7 要治的毛病
      openDetailById(id);
    });
    return a;
  }

  /** 兼容旧调用方：语义已从「打开编辑弹窗」变成「打开详情页」。 */
  function openEntryById(id) {
    return openDetailById(id);
  }

  /**
   * 评分列：**只读展示**（需求 7）。
   * 表格里不再能打分——浏览时误改分值是这条需求要消除的行为。
   * 要打分进编辑弹窗（#v-modal），那里挂的是可打分态控件。
   */
  function fillRatingCells(tr, entry) {
    var cells = tr.querySelectorAll('.cell-rating');
    Array.prototype.forEach.call(cells, function (cell) {
      var field = cell.dataset.rating; // 'importance' | 'usefulness'
      cell.appendChild(buildStars({
        readOnly: true,
        field: field,
        value: entry[field]
      }));
    });
  }

  /**
   * 词条详情页（需求 5）：例句 / 来源的完整内容在这里看。
   *
   * 全部字段走 textContent / DOM 构建 —— 词条名、释义、例句、来源都是用户自由
   * 文本，用 innerHTML 拼字符串时 `<img src=x onerror=...>` 会被解析成真元素并
   * 立刻执行。空值一律显式写「（未填写）」，不留白让人猜是漏填还是没数据。
   */
  var EMPTY_MARK = '（未填写）';

  /** 空值兜底：null / undefined / 全空白字符串都算「没填」。 */
  function detailText(value) {
    var text = (value === null || value === undefined) ? '' : String(value);
    return text.trim() ? text : EMPTY_MARK;
  }

  /** 词汇模块 · 词条详情页（item = 词条对象，不是 id）。与外刊模块的 openArticleDetail(id) 是两个函数，勿同名。 */
  function openDetail(item) {
    state.detailId = item ? item.id : null;
    $('v-detail-title').textContent = item ? item.term : '词条详情';
    $('v-detail-meta').textContent = (item && item.created_at)
      ? ('添加于 ' + formatTime(item.created_at)) : '';
    $('v-detail-term').textContent = detailText(item && item.term);
    $('v-detail-type').textContent = (item && item.type === 'phrase') ? '短语' : '单词';
    $('v-detail-pos').textContent = detailText(item && item.pos);
    $('v-detail-meaning').textContent = detailText(item && item.meaning_zh);
    $('v-detail-usage').textContent = detailText(item && item.usage);
    $('v-detail-source').textContent = detailText(item && item.source);

    // 详情页的星级同样只读（需求 7③）：进详情不等于进编辑态
    var imp = $('v-detail-importance');
    var use = $('v-detail-usefulness');
    imp.textContent = '';
    use.textContent = '';
    imp.appendChild(buildStars({ readOnly: true, field: 'importance', value: item && item.importance }));
    use.appendChild(buildStars({ readOnly: true, field: 'usefulness', value: item && item.usefulness }));

    $('v-detail').hidden = false;
  }

  function closeDetail() { $('v-detail').hidden = true; state.detailId = null; }

  /** 拉详情再打开；id 来自数据属性，调用前一律 parseInt 过。 */
  function openDetailById(id) {
    return api('/api/entries/' + id)
      .then(function (item) { openDetail(item); })
      .catch(function (err) { toast('读取条目失败：' + err.message, 'error'); });
  }

  function renderRows(items) {
    var body = $('v-body');
    if (!items.length) { body.innerHTML = ''; return; }
    // 列顺序必须与 index.html 的 colgroup / thead 严格一致（8 列）：
    // 词条 / 类型 / 词性 / 中文释义 / 重要性 / 实用性 / 添加时间 / 操作。
    // 例句与来源已移入详情页，不再出现在主视图（需求 5）。
    body.innerHTML = items.map(function (item) {
      return '<tr data-id="' + esc(String(item.id)) + '">' +
        '<td class="cell-term">' + esc(item.term) + '</td>' +
        '<td><span class="badge badge-' + esc(item.type) + '">' +
          (item.type === 'phrase' ? '短语' : '单词') + '</span></td>' +
        '<td>' + (esc(item.pos) || '<span class="muted">-</span>') + '</td>' +
        '<td class="cell-meaning"></td>' +
        '<td class="cell-rating" data-rating="importance"></td>' +
        '<td class="cell-rating" data-rating="usefulness"></td>' +
        '<td class="muted">' + esc(formatTime(item.created_at)) + '</td>' +
        '<td class="cell-ops">' +
          '<button class="linkbtn" data-detail="' + esc(String(item.id)) + '">详情</button>' +
          '<button class="linkbtn" data-edit="' + esc(String(item.id)) + '">编辑</button>' +
          '<button class="linkbtn linkbtn-danger" data-del="' + esc(String(item.id)) + '">删除</button>' +
        '</td></tr>';
    }).join('');

    // 释义列与评分列改成 DOM 构建（上面只留空壳 td）：
    // 释义含用户自由文本，评分要挂只读控件，两者都不适合拼字符串。
    Array.prototype.forEach.call(body.querySelectorAll('tr[data-id]'), function (tr) {
      var entry = itemById(items, parseInt(tr.dataset.id, 10));
      if (!entry) return;
      fillMeaningCell(tr.querySelector('.cell-meaning'), entry);
      fillRatingCells(tr, entry);
      // 点行选中（需求 4 的多选）。监听挂在 tr 上而不是每个 td，
      // 但必须排除操作列与释义列里的链接/按钮 —— 否则点「详情」「编辑」「删除」
      // 或近义词链接会连带切换选中，用户体验是「我只想打开它却被选中了」。
      // 用 capture 阶段 + closest 判定：链接与按钮都在各自的子树里，
      // 事件冒泡到 tr 时 e.target 仍是最初那个元素。
      tr.addEventListener('click', function (e) {
        if (e.target.closest('button, a')) return;
        toggleRowSelect(parseInt(tr.dataset.id, 10));
      });
    });

    body.querySelectorAll('[data-detail]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        openDetailById(parseInt(btn.dataset.detail, 10));
      });
    });
    body.querySelectorAll('[data-edit]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var id = parseInt(btn.dataset.edit, 10);
        api('/api/entries/' + id).then(function (item) { openForm(item); })
          .catch(function (err) { toast('读取条目失败：' + err.message, 'error'); });
      });
    });
    body.querySelectorAll('[data-del]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var id = parseInt(btn.dataset.del, 10);
        var term = btn.closest('tr').querySelector('.cell-term').textContent;
        openConfirm(id, term);
      });
    });
  }

  function renderPager() {
    var pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    if (state.page > pages) state.page = pages;
    $('v-page').textContent = '第 ' + state.page + ' / ' + pages + ' 页（每页 ' + PAGE_SIZE + ' 条）';
    $('v-prev').disabled = state.page <= 1;
    $('v-next').disabled = state.page >= pages;
  }

  // ---------- 表单 ----------

  function syncTypeUI() {
    $('f-pos-row').hidden = $('f-type').value === 'phrase';
  }

  function showFormError(message) {
    var el = $('f-error');
    if (!message) { el.hidden = true; el.textContent = ''; return; }
    el.textContent = message;
    el.hidden = false;
  }

  /**
   * 挂上编辑弹窗里的两个评分控件（需求 7②）。
   *
   * 按当前值预填，并记住「打开时的原值」。只有用户真的动过的那一维，
   * 提交时才会带上这个 key —— 没改动就不带，后端 exclude_unset 保持原值。
   * 这样「打开编辑弹窗随手点一下又改回来」不会把评分误清成 null。
   *
   * 控件用 defer 模式：点击只改本地显示，不发请求（评分随保存一起 PUT）。
   * 未评分（null）预填时控件是空心态，不显示成「0 分」。
   */
  function mountFormRatings(item) {
    state.formRatings = {};
    ['importance', 'usefulness'].forEach(function (field) {
      var raw = item ? item[field] : null;
      var value = (raw === null || raw === undefined) ? null : Number(raw);
      state.formRatings[field] = { original: value };
      var box = $('f-' + field);
      box.textContent = '';
      box.appendChild(buildStars({
        defer: true,
        entryId: item ? item.id : null,
        field: field,
        value: value
      }));
    });
  }

  /**
   * 按「改动过才带」的原则把评分并进 payload。
   *
   * NULL ≠ 0：未评分发 null（清除），真打了 0 分发 0，两者绝不能混。
   * 没改动则**不带这个 key**，后端 model_dump(exclude_unset=True) 会保持原值。
   * 值从 DOM 的 dataset.value 读，不另存一份 state，避免两处状态走偏。
   */
  function applyFormRatings(payload) {
    ['importance', 'usefulness'].forEach(function (field) {
      var snapshot = state.formRatings && state.formRatings[field];
      if (!snapshot) return;
      var el = $('f-' + field).querySelector('.stars');
      if (!el) return;
      var value = el.dataset.value === '' ? null : Number(el.dataset.value);
      var unchanged = value === snapshot.original
        || (value === null && snapshot.original === null);
      if (!unchanged) payload[field] = value;
    });
    return payload;
  }

  function openForm(item) {
    state.editingId = item ? item.id : null;
    showFormError('');
    $('v-modal-title').textContent = item ? '编辑条目' : '新增条目';
    $('f-type').value = item ? item.type : 'word';
    $('f-term').value = item ? item.term : '';
    $('f-pos').value = item ? item.pos : '';
    $('f-meaning').value = item ? item.meaning_zh : '';
    $('f-usage').value = item ? item.usage : '';
    $('f-source').value = item ? item.source : '';
    mountFormRatings(item);
    setSaving(false);
    syncTypeUI();
    $('v-modal').hidden = false;
    $('f-term').focus();
  }

  function closeForm() { $('v-modal').hidden = true; }

  function setSaving(flag) {
    // 有请求在飞时禁止解锁；解锁只由 submitForm 末位 then 负责（详见 state.inflight）
    if (!flag && state.inflight) return;
    state.saving = flag;
    $('f-save').disabled = flag || state.importing;  // 导入期间保持禁用
    $('f-save').textContent = flag ? '保存中…' : '保存';
  }

  function setImporting(flag) {
    state.importing = flag;
    $('v-import').disabled = flag;
    $('v-import').textContent = flag ? '导入中…' : '导入 JSON 备份';
    // 保存按钮在导入期间一并禁用：导入长时间持有写锁，此时保存会失败
    $('f-save').disabled = flag || state.saving;
  }

  function submitForm() {
    if (state.saving) return;  // 双击「保存」或「回车 + 点击」只发一次请求
    if (state.importing) { toast('导入进行中，请稍候再保存', 'error'); return; }
    var payload = {
      type: $('f-type').value,
      term: $('f-term').value.trim(),
      pos: $('f-pos').value.trim(),
      meaning_zh: $('f-meaning').value.trim(),
      usage: $('f-usage').value.trim(),
      source: $('f-source').value.trim()
    };
    if (!payload.term) { showFormError('词条不能为空'); return; }
    if (!payload.meaning_zh) { showFormError('中文释义不能为空'); return; }
    if (payload.type === 'phrase') payload.pos = '';

    // 评分随表单一起提交：新增时后端从创建体里读（EntryIn 已声明这两个字段），
    // 编辑时靠 exclude_unset 区分「没传（保持原值）」与「显式 null（清除）」。
    applyFormRatings(payload);

    var editing = state.editingId;
    var request = editing
      ? api('/api/entries/' + editing, { method: 'PUT', body: JSON.stringify(payload) })
      : api('/api/entries', { method: 'POST', body: JSON.stringify(payload) });

    state.inflight = true;
    setSaving(true);
    request.then(function (saved) {
      closeForm();
      toast(editing ? '已保存修改' : '已新增「' + saved.term + '」', 'ok');
    }).then(function () { return loadEntries(); })
      .then(refreshStats)
      // loadEntries / refreshStats 各自内部已 catch，返回的必然是已解决的
      // promise，永远不会 reject —— 因此本 catch 只处理"保存请求本身失败"（如 409 重复录入、
      // 网络错误），弹窗保持打开、错误显示在原表单上供用户改后重试，正是期望行为。
      // 勿在此加内层 catch（刷新失败到不了这里），也勿把 closeForm() 挪到这里（会让失败也关窗）。
      .catch(function (err) {
        showFormError(err.message);
      })
      // 无论成败都必须复位，否则一次 409 后永久无法保存。
      // 顺序不能反：必须先清 inflight，否则 setSaving 的守卫会把这次解锁也挡掉。
      .then(function () {
        state.inflight = false;
        setSaving(false);
      });
  }

  // ---------- 删除确认 ----------

  function openConfirm(id, term) {
    state.pendingDeleteId = id;
    $('d-text').textContent = '确定删除「' + term + '」吗？其复习状态会一并清除，且无法恢复。';
    $('d-modal').hidden = false;
  }

  function closeConfirm() { $('d-modal').hidden = true; state.pendingDeleteId = null; }

  function doDelete() {
    var id = state.pendingDeleteId;
    if (!id) return;
    api('/api/entries/' + id, { method: 'DELETE' }).then(function (data) {
      closeConfirm();
      // 需求 3 的「提示」：后端告诉我们这个词条在概念树里有没有子项。
      // 有子项就留了占位节点，必须告诉用户——否则他之后打开概念树看到
      // 一个标着「已删除的词条」的节点，会以为是出了 bug。
      // 没有子项则什么都不用说（后端不留无信息量的空占位）。
      var tomb = data && data.tombstone;
      toast(tomb && tomb.tombstoned
        ? '已删除条目；概念树中该词条下有 ' + tomb.children_kept +
          ' 个子项，已保留占位节点'
        : '已删除条目', 'ok');
    }).then(function () { return loadEntries(); })
      .then(refreshStats)
      // 树那边的占位计数要跟着更新，否则侧栏数字与实际不符。
      .then(function () {
        if (window.EnPracticeNodes && window.EnPracticeNodes.loadTombstoneCount) {
          return window.EnPracticeNodes.loadTombstoneCount();
        }
        return null;
      })
      .catch(function (err) { closeConfirm(); toast('删除失败：' + err.message, 'error'); });
  }

  // ---------- 需求 4：AI 评分建议 ----------

  /**
   * 「修改后接受」的实现方式：用户点「调整」后把只读星级换成 defer 模式的可打分
   * 星级（预填 AI 建议值），此时「接受」读的就是 DOM 上的值。
   * 不用额外的两个数字输入框 —— 编辑弹窗里的星级控件已经验证过这套交互，
   * 再造一种写法只会让用户多记一种操作。
   */
  function showAiError(message) {
    var el = $('ai-error');
    if (!message) { el.hidden = true; el.textContent = ''; return; }
    el.textContent = message;
    el.hidden = false;
  }

  /**
   * 切换确认面板的**语义模式**，必须在每次 openAiQueue 之前调用。
   *
   * 为什么要有两种模式：用户对两个入口的期待是相反的。
   *  - 批量（选中 / 全库未评分）= "补齐没打分的"，所以跳过已有手动评分；
   *  - 详情页单条 = "重新评估"，用户就是想看 AI 怎么重新判断，
   *    跳过的话他永远看不到结果（上一版就是这么坏的）。
   * 同一个面板文案写不出两句都能成立的话，所以按模式切换标题与提示。
   * mode: 'batch' | 'single'
   */
  function setAiMode(mode) {
    state.aiMode = mode;
    var single = mode === 'single';
    $('ai-hint-batch').hidden = single;
    $('ai-hint-single').hidden = !single;
    // 标题里的"评分评估中"也要区分，否则单条重新评估时用户以为在跑批量
    $('ai-modal-title').textContent = single ? 'AI 重新评估建议' : 'AI 评分建议';
  }

  function setAiBusy(busy) {
    if (state.aiMode === 'single') {
      $('ai-modal-title').textContent = busy ? 'AI 重新评估中…' : 'AI 重新评估建议';
    } else {
      $('ai-modal-title').textContent = busy ? 'AI 评分评估中…' : 'AI 评分建议';
    }
    // 「中断」只对分批任务有意义：单条只有一次请求，用户点了也只是等它返回，
    // 显示出按钮等于给一个无效控件（详见 interruptAiBatch 的注释）。
    $('ai-interrupt').hidden = !busy || state.aiMode !== 'batch';
    $('ai-interrupt').disabled = false;
    $('ai-accept-all').hidden = busy;
    $('ai-close').hidden = busy;
  }

  /**
   * 渲染一条 AI 建议。
   *
   * **XSS 红线**：term 与 reason 都来自模型输出（不可信外部文本），
   * 一律 textContent 写入，**绝不 innerHTML 拼接**。模型完全可能返回
   * `<img src=x onerror=...>` 作为"理由"，拼字符串就会变成真元素并立刻执行。
   */
  function renderAiItem(item, index) {
    var card = document.createElement('div');
    card.className = 'ai-card';

    var head = document.createElement('div');
    head.className = 'ai-card-head';
    var term = document.createElement('span');
    term.className = 'ai-card-term';
    term.textContent = item.term || ('ID ' + item.entry_id);
    head.appendChild(term);

    if (item.skipped) {
      var skip = document.createElement('span');
      skip.className = 'muted';
      // skip_reason 也是后端文本，同样走 textContent
      skip.textContent = item.skip_reason || '已跳过（AI 不覆盖已有评分）';
      head.appendChild(skip);
    } else if (item.error) {
      var err = document.createElement('span');
      err.className = 'ai-card-error';
      err.textContent = item.error;
      head.appendChild(err);
    } else if (item.had_rating) {
      // overwrite=true 时才可能出现：明确告知"接受会覆盖你手动打的分"
      var warn = document.createElement('span');
      warn.className = 'ai-card-warn';
      warn.textContent = '已有手动评分，接受将覆盖';
      head.appendChild(warn);
    }
    card.appendChild(head);

    // 失败 / 跳过的条目不渲染星级与理由：没有分值可显示，
    // 硬画一个空控件只会让用户以为"AI 认为它值 0 分"
    if (item.skipped || item.error) return card;

    ['importance', 'usefulness'].forEach(function (field) {
      var row = document.createElement('div');
      row.className = 'ai-row';

      var label = document.createElement('span');
      label.className = 'ai-label';
      label.textContent = field === 'importance' ? '重要性' : '实用性';
      row.appendChild(label);

      var box = document.createElement('div');
      box.className = 'ai-stars';
      box.dataset.field = field;
      // 初始只读：展示 AI 建议值，用户点「调整」后才切成可打分
      box.appendChild(buildStars({
        readOnly: true,
        field: field,
        value: item[field]
      }));
      row.appendChild(box);

      var reason = document.createElement('p');
      reason.className = 'ai-reason';
      var why = item.reason && item.reason[field];
      reason.textContent = why ? why : '（AI 未给出理由）';
      row.appendChild(reason);

      card.appendChild(row);
    });

    var ops = document.createElement('div');
    ops.className = 'ai-ops';

    var accept = document.createElement('button');
    accept.type = 'button';
    accept.className = 'btn btn-sm btn-primary';
    accept.textContent = '接受';
    accept.addEventListener('click', function () { acceptAiItem(index, false); });
    ops.appendChild(accept);

    var adjust = document.createElement('button');
    adjust.type = 'button';
    adjust.className = 'btn btn-sm';
    adjust.textContent = '调整';
    adjust.addEventListener('click', function () { toggleAiAdjust(item, card, adjust); });
    ops.appendChild(adjust);

    var ignore = document.createElement('button');
    ignore.type = 'button';
    ignore.className = 'btn btn-sm';
    ignore.textContent = '忽略';
    ignore.addEventListener('click', function () { ignoreAiItem(index); });
    ops.appendChild(ignore);

    card.appendChild(ops);
    return card;
  }

  /** 在「只读展示 AI 建议」与「可打分」之间切换（修改后接受的入口）。 */
  function toggleAiAdjust(item, card, adjustBtn) {
    var adjusting = adjustBtn.dataset.adjusting === '1';
    ['importance', 'usefulness'].forEach(function (field) {
      var box = card.querySelector('.ai-stars[data-field="' + field + '"]');
      if (!box) return;
      var old = box.querySelector('.stars');
      var shown = old ? (old.dataset.value === '' ? null : Number(old.dataset.value)) : null;
      box.textContent = '';
      if (adjusting) {
        // 回到只读：还原成 AI 建议值，而不是保留用户改过的值——
        // 否则再点一次「接受」会写进一个界面上已经看不到了的分
        box.appendChild(buildStars({
          readOnly: true, field: field, value: item[field]
        }));
      } else {
        // 切成可打分：预填当前显示值，用户点哪颗星就是哪一维
        box.appendChild(buildStars({
          defer: true, field: field, value: shown === null ? item[field] : shown
        }));
      }
    });
    adjustBtn.dataset.adjusting = adjusting ? '0' : '1';
    adjustBtn.textContent = adjusting ? '调整' : '取消调整';
  }

  /** 读卡片上星级控件的实际值（调整态下用户可能改过）。 */
  function readAiCardValues(card) {
    var out = {};
    ['importance', 'usefulness'].forEach(function (field) {
      var el = card ? card.querySelector('.ai-stars[data-field="' + field + '"] .stars') : null;
      if (!el) return;
      out[field] = el.dataset.value === '' ? null : Number(el.dataset.value);
    });
    return out;
  }

  /**
   * 接受一条建议：把分值 PATCH 回该词条。
   *
   * 走的是与手动打分**完全相同**的窄接口 PATCH /api/entries/{id}/ratings，
   * 于是「AI 采纳」与「用户手打」落到库里的东西没有任何区别 ——
   * 也就不会出现"AI 的分走了另一条写入路径、绕过半星校验"这类旁路。
   */
  function acceptAiItem(index, silent) {
    var item = state.aiQueue[index];
    if (!item || item.skipped || item.error) return Promise.resolve();
    var card = $('ai-list').children[index];
    var values = readAiCardValues(card);
    // 两维都没读到值（理论上不会发生）就别发请求，避免把评分误清成 null
    if (values.importance === undefined && values.usefulness === undefined) {
      return Promise.resolve();
    }
    var body = {};
    if (values.importance !== undefined) body.importance = values.importance;
    if (values.usefulness !== undefined) body.usefulness = values.usefulness;
    return api('/api/entries/' + item.entry_id + '/ratings', {
      method: 'PATCH',
      body: JSON.stringify(body)
    }).then(function (entry) {
      item.accepted = true;
      item.importance = entry.importance;
      item.usefulness = entry.usefulness;
      var card2 = $('ai-list').children[index];
      if (card2) card2.classList.add('ai-card-done');
      if (!silent && card2) {
        var badge = document.createElement('span');
        badge.className = 'ai-card-warn';
        badge.textContent = '已写入';
        card2.appendChild(badge);
      }
      if (!silent) toast('已写入「' + (item.term || '') + '」的评分', 'ok');
      return loadEntries();
    }).then(refreshStats).catch(function (err) {
      toast('写入失败：' + err.message, 'error');
    });
  }

  function ignoreAiItem(index) {
    var item = state.aiQueue[index];
    if (item) item.ignored = true;
    var card = $('ai-list').children[index];
    if (card) card.classList.add('ai-card-ignored');
  }

  /**
   * 打开确认面板并渲染整个队列（批量结果逐条在这里过，不自动写）。
   *
   * keepBusy：供**单条重新评估**在发请求前先开面板时使用。
   *   此刻AI 还没返回，面板必须保持"进行中"（标题带"…中…"、隐藏「完成」、
   *   显示可点但无效的「中断」）—— 否则用户能在请求飞行中直接关掉弹窗。
   *   不带该参数时维持原行为：渲染完成即视为空闲，开放「完成」与「全部接受」。
   */
  function openAiQueue(items, meta, keepBusy) {
    state.aiQueue = items || [];
    state.aiIndex = state.aiQueue.length ? 0 : -1;
    showAiError('');
    $('ai-modal-meta').textContent = meta || '';
    var list = $('ai-list');
    list.textContent = '';
    state.aiQueue.forEach(function (item, index) {
      list.appendChild(renderAiItem(item, index));
    });
    if (!state.aiQueue.length) {
      var empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = '没有需要评估的词条。';
      list.appendChild(empty);
    }
    setAiBusy(!!keepBusy);
    $('ai-modal').hidden = false;
  }

  function closeAiQueue() {
    $('ai-modal').hidden = true;
    $('ai-list').textContent = '';
    state.aiQueue = [];
    state.aiIndex = -1;
    setAiProgress(null);
  }

  /** 批量进度：done 为 null 时隐藏整块；total 为 0 表示总量未知（不确定进度条）。 */
  function setAiProgress(done, total) {
    var wrap = $('ai-progress-wrap');
    if (done === null) { wrap.hidden = true; return; }
    wrap.hidden = false;
    var pct = total > 0 ? Math.round((done / total) * 100) : 0;
    $('ai-progress-bar').style.width = pct + '%';
    $('ai-progress-text').textContent = '已评估 ' + done + ' 条' +
      (total ? ' / 共 ' + total + ' 条' : '（数量未知，继续查找未评分词条…）');
  }

  /**
   * 批量评估：前端按批串行调用后端，**每批之间**检查中断标志。
   *
   * 为什么进度条在前端、而不是提交给后台异步任务再轮询：
   * 1) 中断即时 —— 点「中断」后最多等当前这一批返回，不会在"已停止"后还在跑；
   * 2) 无需引入 APScheduler / BackgroundTasks，也就没有"任务状态存哪、
   *    进程重启怎么办"这类新问题（单机单窗口应用里这些纯属净负担）；
   * 3) 后端一次请求内做完 N 次串行 AI 调用，前端只是把 N 拆成几页而已。
   *
   * 每批 PAGE 条，与后端 ai_ratings.BATCH_SIZE 对齐，避免"一页 8 条里
   * 后端又拆成 1 批"这种无谓的分层。
   *
   * **批与批之间必须让出主线程**（setTimeout 0），这不是锦上添花：
   * 连续 .then 串起来全在微任务队列里跑，浏览器一次 paint 都不会做。
   * 本地后端响应很快时，25 批会在几毫秒内跑完 —— 进度条不刷新、按钮点不动，
   * 用户看到的就是"点了没反应然后突然全出来了"，恰好是需求要治的"卡死"。
   * 让出主线程后，进度条每批更新一次，中断按钮也才真的可点。
   */
  function runAiBatches(postUrl, body, onProgress) {
    state.aiBatchRunning = true;
    state.aiBatchAbort = false;
    var collected = [];
    var offset = 0;
    var PAGE = 8;

    function step() {
      if (state.aiBatchAbort) return Promise.resolve(collected);
      var payload = {};
      Object.keys(body).forEach(function (k) { payload[k] = body[k]; });
      payload.limit = PAGE;
      payload.offset = offset;
      return api(postUrl, { method: 'POST', body: JSON.stringify(payload) })
        .then(function (data) {
          var items = data.items || [];
          collected = collected.concat(items);
          offset += items.length;
          if (onProgress) onProgress(collected.length);
          // 返回空 / 少于一页 → 没有更多未评分词条了。
          // 注意 offset 必须在**后端跳过的条目也算一条**的前提下累加，
          // 否则跳过的词会被反复取回、陷入死循环（后端每条都返回占位结果）。
          if (!items.length || items.length < PAGE) return collected;
          // 让出主线程后再发下一批（见上方注释）
          return new Promise(function (r) { setTimeout(r, 0); }).then(step);
        });
    }

    return step().then(function (result) {
      state.aiBatchRunning = false;
      return result;
    }).catch(function (err) {
      state.aiBatchRunning = false;
      throw err;
    });
  }

  /** 选中词条 → 逐个评估（overwrite=false，已评分的会被后端跳过）。 */
  function aiRateSelected() {
    if (!state.selected.length) return;
    var ids = state.selected.slice();
    setAiMode('batch');
    openAiQueue([], '正在评估 ' + ids.length + ' 条…');
    setAiProgress(0, ids.length);
    setAiBusy(true);
    // setTimeout(0)：让进度条先渲染出来。否则本地/极快的响应会在
    // 浏览器还没绘制进度条时就返回，用户看到的是"闪一下就没了"。
    setTimeout(function () {
      api('/api/entries/ratings/suggest', {
        method: 'POST',
        body: JSON.stringify({ entry_ids: ids, overwrite: false })
      }).then(function (data) {
        setAiProgress(ids.length, ids.length);
        openAiQueue(data.items || [], describeAiResult(data));
      }).catch(function (err) {
        setAiBusy(false);
        showAiError(err.message);
      });
    }, 0);
  }

  /** 全库批量评估未评分词条（分批 + 可中断）。 */
  function aiRateUnrated() {
    setAiMode('batch');
    openAiQueue([], '正在评估未评分词条…');
    setAiProgress(0, 0);
    setAiBusy(true);
    runAiBatches('/api/entries/ratings/suggest-unrated', { overwrite: false },
      function (done) { setAiProgress(done, 0); })
      .then(function (items) {
        // 评估结束 → 收起进度条。留着一条 100% 的进度条会让用户以为
        // "评估完了但界面卡住了"，而实际上下面就是待确认的队列了。
        setAiProgress(null);
        openAiQueue(items, items.length ? '共 ' + items.length + ' 条待确认' : '没有未评分的词条');
      })
      .catch(function (err) {
        setAiProgress(null);
        setAiBusy(false);
        showAiError(err.message);
      });
  }

  /**
   * 单条：从详情页点「重新评估」进来。
   *
   * overwrite=true —— 用户口径是「详情页再次点 AI 评估 = 重新评估」，
   * 所以已评分的词条也必须重新给建议，不能被后端 skip 掉
   *（skip 掉的话用户看到的就是一句"已跳过"，等于这个按钮没生效）。
   *
   * 注意：overwrite 只决定"给不给建议"，**不决定"写不写"**——
   * 这里返回的建议仍旧只是建议，落库唯一入口是用户在面板点「接受」
   * 后调 PATCH /api/entries/{id}/ratings。
   */
  function aiRateOne(id) {
    if (!id) return;
    setAiMode('single');
    // keepBusy=true：请求还没发，面板必须先显示"进行中"，
    // 否则 openAiQueue 会顺手 setAiBusy(false)，把用户放回可关闭状态。
    openAiQueue([], '正在重新评估…', true);
    setTimeout(function () {
      api('/api/entries/ratings/suggest', {
        method: 'POST',
        body: JSON.stringify({ entry_ids: [id], overwrite: true })
      }).then(function (data) {
        openAiQueue(data.items || [], describeAiResult(data));
      }).catch(function (err) {
        setAiBusy(false);
        showAiError(err.message);
      });
    }, 0);
  }

  function describeAiResult(data) {
    var parts = [];
    parts.push('建议 ' + (data.evaluated || 0) + ' 条');
    if (data.skipped) parts.push('跳过 ' + data.skipped + ' 条（已有手动评分）');
    if (data.failed) parts.push('失败 ' + data.failed + ' 条');
    return parts.join(' · ');
  }

  function interruptAiBatch() {
    // 只置标志：当前那一次请求让它自然返回，批与批之间才真正停下。
    // 直接 abort 请求虽然更"即时"，但后端已经算完的那次调用就白花钱了，
    // 而且会让"已评估 N 条"的计数与实际调用数对不上。
    state.aiBatchAbort = true;
    $('ai-interrupt').disabled = true;
  }

  function acceptAllAiItems() {
    var pending = state.aiQueue.filter(function (item) {
      return !item.skipped && !item.error && !item.accepted && !item.ignored;
    });
    if (!pending.length) { toast('没有待接受的建议', 'error'); return; }
    // 串行接受：并发 PATCH 会让 loadEntries 的刷新互相覆盖，
    // 且中途失败时前端状态难以与库对齐。
    var chain = Promise.resolve();
    pending.forEach(function (item) {
      chain = chain.then(function () {
        return acceptAiItem(state.aiQueue.indexOf(item), true);
      });
    });
    chain.then(function () {
      toast('已接受 ' + pending.length + ' 条建议', 'ok');
      closeAiQueue();
    });
  }

  // ---------- 导入 / 导出 ----------

  function doExport() {
    api('/api/entries/export?' + queryString().replace(/&page=\d+&page_size=\d+/, ''))
      .then(function (data) {
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'enpractice-entries-' + String(data.exported_at || '').slice(0, 10) + '.json';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        exportToast(link.download, data.count + ' 条条目');
      })
      .catch(function (err) { toast('导出失败：' + err.message, 'error'); });
  }

  function doExportMarkdown() {
    downloadFile('/api/entries/export.md', 'enpractice-entries.md');
  }

  function doImport(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var payload;
      try {
        payload = JSON.parse(String(reader.result));
      } catch (e) {
        toast('导入失败：文件不是合法的 JSON', 'error');
        return;
      }
      setImporting(true);
      api('/api/entries/import?on_duplicate=skip', {
        method: 'POST',
        body: JSON.stringify(payload)
      }).then(function (res) {
        toast('导入完成：新增 ' + res.created + ' 条，跳过重复 ' + res.skipped +
          ' 条，失败 ' + res.failed + ' 条', res.failed ? 'error' : 'ok');
      }).then(function () { state.page = 1; return loadEntries(); })
        .then(refreshStats)
        .catch(function (err) { toast('导入失败：' + err.message, 'error'); })
        // 无论成败都复位，否则一次失败后导入按钮永久不可用
        .then(function () { setImporting(false); });
    };
    reader.onerror = function () { toast('导入失败：文件读取出错', 'error'); };
    reader.readAsText(file, 'utf-8');
  }

  // ---------- 事件绑定 ----------

  var searchTimer = null;
  $('v-search').addEventListener('input', function () {
    if (searchTimer) clearTimeout(searchTimer);
    var value = this.value;
    searchTimer = setTimeout(function () {
      state.q = value.trim();
      state.page = 1;
      loadEntries();
    }, 300);
  });

  $('v-type').addEventListener('change', function () {
    state.type = this.value; state.page = 1; loadEntries();
  });
  $('v-from').addEventListener('change', function () {
    state.dateFrom = this.value; state.page = 1; loadEntries();
  });
  $('v-to').addEventListener('change', function () {
    state.dateTo = this.value; state.page = 1; loadEntries();
  });
  $('v-reset').addEventListener('click', function () {
    state.q = ''; state.type = ''; state.dateFrom = ''; state.dateTo = '';
    state.page = 1;
    $('v-search').value = ''; $('v-type').value = '';
    $('v-from').value = ''; $('v-to').value = '';
    loadEntries();
  });
  $('v-prev').addEventListener('click', function () {
    if (state.page > 1) { state.page -= 1; loadEntries(); }
  });
  $('v-next').addEventListener('click', function () {
    state.page += 1; loadEntries();
  });
  $('v-new').addEventListener('click', function () { openForm(null); });
  $('v-export').addEventListener('click', doExport);
  $('v-export-md').addEventListener('click', doExportMarkdown);
  $('v-import').addEventListener('click', function () { $('v-file').click(); });
  $('v-file').addEventListener('change', function () {
    if (this.files && this.files[0]) doImport(this.files[0]);
    this.value = '';
  });

  $('f-type').addEventListener('change', syncTypeUI);
  $('f-cancel').addEventListener('click', closeForm);
  $('f-save').addEventListener('click', submitForm);
  $('v-modal').addEventListener('click', function (e) {
    if (e.target === this) closeForm();
  });

  // 词条详情页（只读）
  $('v-detail-close').addEventListener('click', closeDetail);
  $('v-detail').addEventListener('click', function (e) {
    if (e.target === this) closeDetail();
  });
  // 详情页 → 编辑：关掉详情再开编辑，两个弹窗不叠在一起
  $('v-detail-edit').addEventListener('click', function () {
    var id = state.detailId;
    if (!id) return;
    closeDetail();
    api('/api/entries/' + id).then(function (item) { openForm(item); })
      .catch(function (err) { toast('读取条目失败：' + err.message, 'error'); });
  });
  // 详情页 → 重新评估。先关详情再开确认面板：两个 modal 叠着时后者会盖住前者，
  // 用户看得到「AI 建议」却看不到自己正在看的是哪个词条。
  $('v-detail-ai').addEventListener('click', function () {
    var id = state.detailId;
    if (!id) return;
    closeDetail();
    aiRateOne(id);
  });

  // ---------- 需求 4：AI 评分 ----------
  $('v-ai-selected').addEventListener('click', aiRateSelected);
  $('v-ai-unrated').addEventListener('click', aiRateUnrated);
  $('ai-close').addEventListener('click', closeAiQueue);
  $('ai-accept-all').addEventListener('click', acceptAllAiItems);
  $('ai-interrupt').addEventListener('click', interruptAiBatch);
  $('ai-modal').addEventListener('click', function (e) {
    // 点遮罩关闭。批量评估中不关：否则用户点遮罩以为中断了，实际请求还在跑
    if (e.target === this && !state.aiBatchRunning) closeAiQueue();
  });

  $('d-cancel').addEventListener('click', closeConfirm);
  $('d-ok').addEventListener('click', doDelete);
  $('d-modal').addEventListener('click', function (e) {
    if (e.target === this) closeConfirm();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      // 详情页优先关：它可能叠在编辑弹窗之上（从详情点「编辑」时会先关详情）
      // AI 确认面板排在最后：它可能叠在以上任意一层之上
      if (!$('ai-modal').hidden) {
        if (!state.aiBatchRunning) closeAiQueue();
      } else if (!$('v-detail').hidden) closeDetail();
      else if (!$('v-modal').hidden) closeForm();
      else if (!$('d-modal').hidden) closeConfirm();
    }
    // 回车提交：避免在例句/释义多行框里误触发
    if (e.key === 'Enter' && !$('v-modal').hidden
        && e.target.tagName !== 'TEXTAREA') {
      submitForm();
    }
  });

  // ---------- 启动 ----------

  // 暴露给关联库复用：释义列里的「近义词」链接要跳回词条详情，
  // 复制一份打开逻辑只会让两处行为慢慢漂移。
  window.EnPracticeVocab = {
    // 语义已从「打开编辑弹窗」改为「打开只读详情页」：
    // 浏览关联词时误进编辑窗正是需求 7 要消除的行为。
    openEntryById: openDetailById,
    openDetailById: openDetailById,
    reload: function () { return loadEntries(); },
    // 需求 4：供其它模块（如练习页「拿这个词的 AI 评分」）复用同一条路径。
    // 只暴露"给 id"，不暴露队列 —— 队列是 UI 状态，让外部改它必然出乱子。
    suggestRatings: function (id) { return aiRateOne(parseInt(id, 10)); }
  };

  loadEntries().then(refreshStats);
  syncSelectionUI();
})();

// ---------------------------------------------------------------------------
// P2：外刊文章库
// 原生 JS，无构建步骤、无外部依赖，离线可用。
// 数据关系：一篇文章 → 多句；句子按句切分，供翻译练习「按词检索选句」。
// ---------------------------------------------------------------------------
(function articlesModule() {
  var state = { page: 1, pageSize: 20, total: 0, q: '', tags: [], editingId: null };
  // 预置标签（仅作建议）；用户自建标签由 /api/articles/tags 动态拉取后并入筛选
  var PRESET_TAGS = ['经济', '科学', '科技', '心理', '人物', '文化', '游戏', '其他'];
  var allTags = [];

  function showError(msg) {
    var el = $('a-f-error');
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  function loadArticles() {
    $('a-loading').hidden = false;
    $('a-empty').hidden = true;
    return api('/api/articles?page=' + state.page + '&page_size=' + state.pageSize
               + (state.q ? '&q=' + encodeURIComponent(state.q) : '')
               + state.tags.map(function (t) { return '&tags=' + encodeURIComponent(t); }).join(''))
      .then(function (data) {
        state.total = data.total;
        renderRows(data.items);
        renderPager();
        $('a-loading').hidden = true;
        $('a-empty').hidden = data.total !== 0;
        $('a-count').textContent = '共 ' + data.total + ' 篇' +
          (state.tags.length ? '（标签：' + state.tags.join('、') + '，同时满足）' : '');
      })
      .catch(function (err) {
        $('a-loading').hidden = true;
        toast('文章加载失败：' + err.message, 'error');
      });
  }

  function renderRows(items) {
    var html = items.map(function (a) {
      // 链接只放行 http/https：safeUrl 过掉 javascript: 之类，
      // esc() 再挡 HTML 元字符 —— 两层都要，缺一层就是注入入口。
      var u = safeUrl(a.url);
      var link = u
        ? '<a href="' + esc(u) + '" target="_blank" rel="noopener">原文</a>'
        : '<span class="muted">—</span>';
      var zhBadge = a.has_zh ? ' <span class="badge badge-zh">有译文</span>' : '';
      var tags = (a.tags && a.tags.length)
        ? a.tags.map(function (t) { return '<span class="topicpill">' + esc(t) + '</span>'; }).join(' ')
        : '<span class="muted">—</span>';
      return '<tr data-id="' + esc(String(a.id)) + '">'
        // ★ 标题列只留空壳，中文标题由 fillTitleCell 用 DOM 补 ★
        + '<td class="cell-title"></td>'
        + '<td>' + esc(a.source_name || '—') + '</td>'
        + '<td>' + (a.sentence_count || 0) + ' 句' + zhBadge + '</td>'
        + '<td>' + tags + '</td>'
        + '<td>' + link + '</td>'
        + '<td>' + esc((a.added_at || '').slice(0, 10)) + '</td>'
        + '<td class="ops">'
        +   '<button class="btn btn-sm" data-act="view" data-id="' + esc(String(a.id)) + '">详情</button> '
        +   '<button class="btn btn-sm" data-act="edit" data-id="' + esc(String(a.id)) + '">编辑</button> '
        +   '<button class="btn btn-sm btn-danger" data-act="del" data-id="' + esc(String(a.id)) + '">删除</button>'
        + '</td></tr>';
    }).join('');
    $('a-body').innerHTML = html;

    // 标题列走 DOM 构建（需求④）：标题与中文标题都是用户自由文本，
    // 这里用 textContent 写，**绝不 innerHTML 拼接** ——
    // 用户在中文标题里打 `<img src=x onerror=...>` 就会变成真元素并立刻执行。
    Array.prototype.forEach.call($('a-body').querySelectorAll('tr[data-id]'), function (tr) {
      var item = null;
      for (var i = 0; i < items.length; i++) {
        if (String(items[i].id) === tr.dataset.id) { item = items[i]; break; }
      }
      if (item) fillTitleCell(tr.querySelector('.cell-title'), item);
    });

    // 方块卡片形态要显示「第一句截取」，而列表接口不返回正文
    // （/api/articles 只给 title/tags/sentence_count 等摘要）。
    // → 逐篇取一次首句，缓存起来，同一篇不再重复请求。
    //   DOM 上是**追加**一个 td（不是改列数）—— colgroup 仍是 7 列，
    //   qa_canvas.js 那三条列数断言不受影响。
    items.forEach(function (a) {
      fillExcerpt(trOf(a.id));
    });
  }

  /** 按 id 找外刊库的行元素。 */
  function trOf(id) {
    return $('a-body').querySelector('tr[data-id="' + id + '"]');
  }

  /**
   * 首句缓存：id → 首句文本（取不到缓存 null，避免反复请求）。
   * 列表每次重渲染都调 fillExcerpt，缓存命中就不发请求。
   */
  var excerptCache = {};

  /**
   * 取文章首句并填进方块卡片。
   * 正文来自 /api/articles/{id} 的 content 字段；
   * 首句 = 第一个非空行（外刊正文按空行分段，但也有整段无换行的，
   * 所以还要按句号切一次，取第一句）。
   */
  function fillExcerpt(tr) {
    if (!tr) { return; }
    var id = tr.dataset.id;
    if (!id) { return; }
    if (excerptCache[id] !== undefined) {
      if (excerptCache[id]) { paintExcerpt(tr, excerptCache[id]); }
      return;
    }
    api('/api/articles/' + id).then(function (d) {
      var first = '';
      var raw = (d && d.content) ? String(d.content) : '';
      if (raw) {
        var line = raw.split(/\n+/).map(function (s) { return s.trim(); })
          .filter(function (s) { return s.length > 0; })[0] || '';
        // 整段没换行 → 按句号切第一句
        if (line.indexOf('。') >= 0) { line = line.split('。')[0] + '。'; }
        else if (line.indexOf('.') > 0) {
          var m = line.match(/^.*?\./);
          if (m) { line = m[0]; }
        }
        first = line;
      }
      excerptCache[id] = first;
      if (first) { paintExcerpt(tr, first); }
    }).catch(function () {
      // 取不到就记空串，不再重试这一篇
      excerptCache[id] = '';
    });
  }

  /** 把首句写进方块卡片（textContent，绝不 innerHTML —— 正文是用户自由文本）。 */
  function paintExcerpt(tr, text) {
    if (!tr) { return; }
    if (tr.querySelector('.art-excerpt')) { return; }
    var td = document.createElement('td');
    td.className = 'art-excerpt';
    td.textContent = text;
    // 插在操作列之前，让「右下角操作」真的落在右下角
    var ops = tr.querySelector('td.ops');
    if (ops && ops.parentNode) { ops.parentNode.insertBefore(td, ops); }
    else { tr.appendChild(td); }
  }

  /**
   * 标题单元格：英文原标题 + 中文标题（需求④）。
   * 中文标题为空时**不追加任何节点** —— 不留空行、不显示「null」、
   * 也不显示占位符，表格行高与改动前完全一致。
   */
  function fillTitleCell(cell, item) {
    cell.textContent = '';
    var en = document.createElement('span');
    en.className = 'cell-title-en';
    en.textContent = item.title || '';
    cell.appendChild(en);
    var zh = (item.title_zh || '').trim();
    if (!zh) return;
    var sub = document.createElement('span');
    sub.className = 'cell-title-zh';
    sub.textContent = zh;
    cell.appendChild(sub);
  }

  function renderPager() {
    var pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    $('a-page').textContent = state.page + ' / ' + pages;
    $('a-prev').disabled = state.page <= 1;
    $('a-next').disabled = state.page >= pages;
  }

  /** 渲染标签多选下拉：折叠态显示已选芯片（可点 × 移除），展开态是复选框列表。 */
  function renderTagSelect() {
    var val = $('a-tagselect-val');
    val.innerHTML = state.tags.length
      ? state.tags.map(function (t) {
          return '<span class="tagchip" data-drop="' + esc(t) + '" title="点击移除">'
            + esc(t) + '<span class="tagchip-x" aria-hidden="true">×</span></span>';
        }).join('')
      : '<span class="muted">全部标签</span>';

    // 展开态列表 = 预置 ∪ 已建标签（自定义标签随保存实时并入，见 submitForm 的 loadTags）
    var extra = allTags.filter(function (t) { return PRESET_TAGS.indexOf(t) < 0; });
    var names = PRESET_TAGS.concat(extra);
    $('a-tagselect-panel').innerHTML = names.length
      ? names.map(function (t) {
          var on = state.tags.indexOf(t) >= 0;
          return '<label class="tagselect-opt"><input type="checkbox" data-tag="' + esc(t) + '"'
            + (on ? ' checked' : '') + '><span>' + esc(t) + '</span></label>';
        }).join('')
      : '<div class="muted tagselect-empty">暂无标签</div>';
  }

  function setTagSelectOpen(open) {
    $('a-tagselect-panel').hidden = !open;
    $('a-tagselect-head').setAttribute('aria-expanded', String(open));
    $('a-tagselect').classList.toggle('open', open);
  }

  function loadTags() {
    return api('/api/articles/tags')
      .then(function (list) { allTags = Array.isArray(list) ? list : []; renderTagSelect(); })
      .catch(function () { renderTagSelect(); });  // 拉取失败也有预置标签可点
  }

  function openForm(article) {
    state.editingId = article ? article.id : null;
    $('a-modal-title').textContent = article ? '编辑文章' : '添加外刊文章';
    $('a-f-title').value = article ? article.title : '';
    $('a-f-title-zh').value = article ? (article.title_zh || '') : '';
    $('a-f-source').value = article ? (article.source_name || '') : '';
    $('a-f-url').value = article ? (article.url || '') : '';
    $('a-f-topic').value = article ? (article.tags || []).join(', ') : '';
    $('a-f-content').value = article ? article.content : '';
    $('a-f-zh').value = article ? (article.content_zh || '') : '';
    $('a-f-extra').value = article ? (article.extra_info || '') : '';
    showError('');
    $('a-modal').hidden = false;
    $('a-f-title').focus();
  }

  function closeForm() { $('a-modal').hidden = true; state.editingId = null; }

  /** 保存成功后的提示：句数 + 译文对齐情况。 */
  function articleSavedToast(res) {
    var msg = (state.editingId ? '已保存修改' : '已添加「' + res.title + '」，切出 ' + res.sentence_count + ' 句');
    if (res.zh_aligned) {
      if (res.align_mode === 'exact') {
        msg += '，' + res.translated_count + ' 句全部配上你的译文';
      } else {
        msg += '；中英文句数不完全一致，已按句长自动对齐：' + res.translated_count
          + ' 句配上译文（合并对应处详情页有标注），未配上的句子练习时用 AI 译文';
      }
    } else if ($('a-f-zh').value.trim()) {
      msg += '；译文未能配到具体句子，已整篇保存在文章详情里可对照查看';
    }
    toast(msg, 'ok');
  }

  function submitForm() {
    // 链接只放行 http/https：不合法（javascript: 之类）拦在表单层，不进库；
    // 万一库里已有脏数据（旧版写入 / 导入 JSON），渲染侧 safeUrl 兜底 —— 两层都放。
    var rawUrl = $('a-f-url').value.trim();
    if (rawUrl && !safeUrl(rawUrl)) {
      showError('文章链接只支持 http:// 或 https:// 网址');
      return;
    }
    var payload = {
      title: $('a-f-title').value.trim(),
      title_zh: $('a-f-title-zh').value.trim(),
      source_name: $('a-f-source').value.trim(),
      url: safeUrl(rawUrl),
      tags: $('a-f-topic').value.trim(),
      content: $('a-f-content').value.trim(),
      content_zh: $('a-f-zh').value.trim(),
      extra_info: $('a-f-extra').value.trim()
    };
    if (!payload.title) { showError('标题不能为空'); return; }
    if (!payload.content) { showError('正文不能为空'); return; }

    var req = state.editingId
      ? api('/api/articles/' + state.editingId, { method: 'PUT', body: JSON.stringify(payload) })
      : api('/api/articles', { method: 'POST', body: JSON.stringify(payload) });

    $('a-f-save').disabled = true;
    req.then(function (res) {
        closeForm();
        articleSavedToast(res);
        // 先刷标签清单再刷列表：新建的标签要立刻出现在筛选下拉里
        // （历史上只在页面启动时拉过一次，导致新标签要刷新页面才看得到）。
        return loadTags().then(loadArticles).then(refreshStats);
      })
      .catch(function (err) { showError(err.message); })
      .then(function () { $('a-f-save').disabled = false; });
  }

  /** 外刊模块 · 文章详情（id = 文章 id）。注意与词汇模块的 openDetail(item) 区分开——同名会互相覆盖。 */
  // 当前打开的外刊详情对应的文章 id：导出 Word 要用。
  // 放在模块级而不是函数内，是因为「导出 Word」按钮在弹窗里，
  // 点击时并不在 openArticleDetail 的调用栈里。
  var currentDetailArticleId = null;

  function openArticleDetail(id) {
    currentDetailArticleId = id;
    api('/api/articles/' + id)
      .then(function (art) {
        return api('/api/articles/' + id + '/sentences').then(function (list) {
          return { art: art, list: list };
        });
      })
      .then(function (d) {
        var art = d.art, list = d.list;
        $('a-detail-title').textContent = '文章句子（共 ' + art.sentence_count + ' 句）';
        // 中文标题（需求④）：有才显示。textContent 写入（用户自由文本），
        // 为空时整块 hidden —— 不占位、不显示「null」。
        var artTitleZh = (art.title_zh || '').trim();
        if (artTitleZh) {
          $('a-detail-title-zh').textContent = artTitleZh;
          $('a-detail-title-zh-wrap').hidden = false;
        } else {
          $('a-detail-title-zh').textContent = '';
          $('a-detail-title-zh-wrap').hidden = true;
        }
        // 其他信息（作者/背景）：有才展示。用 textContent 而非 innerHTML——
        // 用户填的是自由文本，全站约定一律走文本节点防注入。
        if ((art.extra_info || '').trim()) {
          $('a-detail-extra-text').textContent = art.extra_info;
          $('a-detail-extra').hidden = false;
        } else {
          $('a-detail-extra').hidden = true;
          $('a-detail-extra-text').textContent = '';
        }
        $('a-detail-list').innerHTML = list.map(function (s) {
          var zh;
          if (s.text_zh) {
            // 译文把这几句英文并成了一段：相邻几句会显示同一段译文，
            // 不标注的话看着像逐句对照，实际是共用一段（练习时这类句子走 AI 单句译文）
            var note = s.zh_seg > 1
              ? ' <span class="sent-zh-note">由中文 ' + s.zh_seg + ' 句合并对应</span>'
              : (s.zh_seg === -1
                ? ' <span class="sent-zh-note">与相邻句共用这段译文（练习时改按单句翻译对照）</span>'
                : '');
            zh = '<p class="sent-zh">' + esc(s.text_zh) + note + '</p>';
          } else if (art.content_zh) {
            // 有整篇译文但这句没配上：明示，避免误以为漏显示
            zh = '<p class="sent-zh sent-zh-none">（未对齐到该句，练习时将使用 AI 译文）</p>';
          } else {
            zh = '';
          }
          return '<li>' + esc(s.text) + zh + '</li>';
        }).join('') || '<li class="muted">这篇文章没有切出有效句子（可能过短或无句末标点）。</li>';
        // 整篇译文在「有译文但存在未配上译文的句子」时展示，补全对照；
        // 全部配上（exact）时不展示，避免与逐句对照重复
        var anyMissing = list.some(function (s) { return !s.text_zh; });
        if (art.content_zh && anyMissing) {
          $('a-detail-zh-text').textContent = art.content_zh;
          $('a-detail-zh').hidden = false;
        } else {
          $('a-detail-zh').hidden = true;
        }
        $('a-detail').hidden = false;
      })
      .catch(function (err) { toast('加载失败：' + err.message, 'error'); });
  }

  function doExportArticleDocx() {
    if (!currentDetailArticleId) { toast('请先打开一篇文章', 'error'); return; }
    downloadFile('/api/articles/' + currentDetailArticleId + '/export.docx', 'article.docx');
  }

  function removeArticle(id) {
    api('/api/articles/' + id, { method: 'DELETE' })
      .then(function () {
        toast('已删除', 'ok');
        // 删掉当前页最后一条时退回上一页，避免停在空页
        if (state.total % state.pageSize === 1 && state.page > 1) state.page -= 1;
        return loadArticles().then(refreshStats);
      })
      .catch(function (err) { toast('删除失败：' + err.message, 'error'); });
  }

  // ---------- 事件 ----------

  $('a-new').addEventListener('click', function () { openForm(null); });
  $('a-f-cancel').addEventListener('click', closeForm);
  $('a-f-save').addEventListener('click', submitForm);
  $('a-detail-export').addEventListener('click', doExportArticleDocx);
  $('a-detail-close').addEventListener('click', function () { $('a-detail').hidden = true; });

  $('a-body').addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-act]');
    if (!btn) return;
    var id = Number(btn.dataset.id);
    if (btn.dataset.act === 'view') openArticleDetail(id);
    else if (btn.dataset.act === 'edit') {
      api('/api/articles/' + id).then(openForm).catch(function (err) { toast(err.message, 'error'); });
    } else if (btn.dataset.act === 'del') {
      openConfirm('确定要删除这篇文章吗？删除后其句子会一并清除，且无法恢复。', function () {
        removeArticle(id);
      });
    }
  });

  var searchTimer;
  $('a-search').addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      state.q = $('a-search').value.trim();
      state.page = 1;
      loadArticles();
    }, 250);
  });

  $('a-tagselect-head').addEventListener('click', function () {
    setTagSelectOpen($('a-tagselect-panel').hidden);
  });

  // 折叠态芯片上的 ×：移除单个标签（阻止冒泡，免得顺手把面板又打开）
  $('a-tagselect-val').addEventListener('click', function (e) {
    var chip = e.target.closest('.tagchip');
    if (!chip) return;
    e.stopPropagation();
    var tag = chip.dataset.drop;
    state.tags = state.tags.filter(function (t) { return t !== tag; });
    state.page = 1;
    renderTagSelect();
    loadArticles();
  });

  // 展开态复选框：多选、AND 语义（与后端一致）
  $('a-tagselect-panel').addEventListener('change', function (e) {
    var box = e.target.closest('input[type="checkbox"]');
    if (!box) return;
    var tag = box.dataset.tag;
    var i = state.tags.indexOf(tag);
    if (i >= 0) state.tags.splice(i, 1);
    else state.tags.push(tag);
    state.page = 1;
    renderTagSelect();
    loadArticles();
  });

  // 点击面板外部收起
  document.addEventListener('click', function (e) {
    if (!$('a-tagselect-panel').hidden && !e.target.closest('#a-tagselect')) {
      setTagSelectOpen(false);
    }
  });

  $('a-reset').addEventListener('click', function () {
    $('a-search').value = '';
    state.q = '';
    state.tags = [];
    state.page = 1;
    renderTagSelect();
    loadArticles();
  });

  $('a-prev').addEventListener('click', function () {
    if (state.page > 1) { state.page -= 1; loadArticles(); }
  });
  $('a-next').addEventListener('click', function () {
    var pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (state.page < pages) { state.page += 1; loadArticles(); }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!$('a-tagselect-panel').hidden) setTagSelectOpen(false);
      else if (!$('a-detail').hidden) $('a-detail').hidden = true;
      else if (!$('a-modal').hidden) closeForm();
    }
  });

  // ---------- 启动 ----------
  // 外刊库列表随页面加载（词汇库/练习模块同样在启动时拉取数据）。
  // 曾经漏掉这一步：面板会永远停在 HTML 里默认的「加载中…」占位，
  // 直到用户碰搜索/重置/分页才触发首次加载。
  loadTags();
  loadArticles();

  // 供翻译练习模块复用：按目标词从本地外刊库选句
  window.EnPracticeArticles = { pickSentences: function (terms) {
    return api('/api/articles/pick-sentences', {
      method: 'POST', body: JSON.stringify({ terms: terms })
    });
  }};
})();

// ---------------------------------------------------------------------------
// P3：翻译练习（左侧「今日复习」子功能 + 右侧练习主流程）
// 流程：取词（当日到期 + 新学）→ 外刊库选句 → AI 中文译文 → 我翻译 → AI 点评评分
// 外刊库没有命中时才走网络外刊（句子 + 网址 + 译文，内容不入库）
// ---------------------------------------------------------------------------
(function practiceModule() {
  var state = {
    practice: null,      // 当前练习 {id, items:[...], is_mock}
    idx: 0,              // 当前展示第几条素材
    submitting: false,
    aiMock: false,
    reviewQueue: [],     // 今日复习队列
    reviewIdx: 0,
    revealed: false,     // 复习时是否已显示答案
    // 每页 25 条：与后端练习流水保留上限（MAX_PRACTICE_RECORDS = 25）一致，
    // 翻页不会看到「明明还有更多却翻不出来」。
    page: 1, pageSize: 25, total: 0
  };

  // ---------- AI 模式提示 ----------

  function loadAiStatus() {
    return api('/api/ai/status').then(function (d) {
      state.aiMock = d.mock;
      var tip = $('p-aitip');
      if (!tip) return;
      tip.hidden = !d.mock;
      if (!d.mock) { tip.textContent = ''; return; }
      var cloud = !!(window.EnPracticeCloud && window.EnPracticeCloud.isCloud);
      if (cloud) {
        // ★ 云端：正向说明 + 一个能点的入口 ★
        //   访客在公网 demo 上普遍没有 key（平台不提供公共 key），原文案
        //   「未配置 AI 密钥，译文与评分均为占位内容」会让用户第一眼觉得是半成品。
        //   改成「现在就能用 / 配了会怎样」的说法，并把入口摆在提示里。
        //   ★ 译文不再是占位中文 ★：mock 模式原样给英文原句（见 app/ai.py
        //     _mock_translate），所以这里说「原句自行对照」，不说「示例译文」——
        //     说成示例内容就等于承认我们编了一段假中文。
        tip.textContent = '当前可直接使用：练习展示英文原句，请自行对照翻译。'
          + '配置你自己的 AI 密钥后，参考译文与评分将改为真实 AI 生成。';
        var btn = document.createElement('button');
        btn.type = 'button';
        // ★ 用实心主色按钮，不用小号描边按钮 ★
        //   这条提示横幅是琥珀色底的，小号描边按钮压在上面几乎看不见 ——
        //   而「设置自己的 API 密钥」是作品核心卖点的入口（点评是不是真的
        //   AI 给的，全看访客有没有填密钥），入口不显眼 = 卖点打折。
        btn.className = 'btn btn-primary';
        btn.id = 'p-aitip-key-btn';
        btn.textContent = '设置密钥';
        btn.addEventListener('click', function () {
          if (window.EnPracticeSettings && window.EnPracticeSettings.openSettings) {
            window.EnPracticeSettings.openSettings();
          }
        });
        tip.appendChild(document.createTextNode(' '));
        tip.appendChild(btn);
      } else {
        // ★ 桌面：文案逐字不变 ★
        //   ★ 译文不再是「占位内容」★：mock 模式原样返回英文原句
        //     （app/ai.py 的 _mock_translate），所以只说评分是演示数据。
        tip.textContent = '演示模式：未配置 AI 密钥，练习展示英文原句供自行对照，'
          + '评分为演示数据。点右上角「设置」填入密钥后可获得真实点评。';
      }
    }).catch(function () {});
  }

  // ---------- 今日复习（翻译练习内的子功能） ----------

  function refreshDue() {
    return api('/api/review/due-count').then(function (d) {
      $('pr-due-summary').textContent = d.due > 0
        ? '有 ' + d.due + ' 个词条待复习'
        : '暂无到期词条（新词会在加入后按间隔出现）';
      return d.due;
    }).catch(function () {
      $('pr-due-summary').textContent = '无法获取复习状态';
    });
  }

  function loadReviewSettings() {
    return api('/api/settings').then(function (s) {
      var rv = s.review || {};
      if (rv.time) $('pr-time').value = rv.time;
      if (rv.mode) $('pr-mode').value = rv.mode;
    }).catch(function () {});
  }

  function saveReviewSettings() {
    return api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ review: { time: $('pr-time').value, mode: $('pr-mode').value } })
    }).catch(function (err) { toast('设置保存失败：' + err.message, 'error'); });
  }

  function startReview() {
    var mode = $('pr-mode').value;
    saveReviewSettings();
    return api('/api/review/due?mode=' + encodeURIComponent(mode) + '&limit=50')
      .then(function (list) {
        if (!list.length) { toast('当前没有待复习的词条', 'ok'); return; }
        state.reviewQueue = list;
        state.reviewIdx = 0;
        state.revealed = false;
        renderReviewCard();
        $('rv-modal').hidden = false;
      })
      .catch(function (err) { toast('加载复习失败：' + err.message, 'error'); });
  }

  function renderReviewCard() {
    var item = state.reviewQueue[state.reviewIdx];
    if (!item) { closeReview(); return; }
    state.revealed = false;
    $('rv-progress').textContent = (state.reviewIdx + 1) + ' / ' + state.reviewQueue.length;
    $('rv-term').textContent = item.term;
    $('rv-pos').textContent = item.pos ? '（' + item.pos + '）' : '';
    $('rv-answer').hidden = true;
    $('rv-reveal').hidden = false;
    $('rv-actions').hidden = true;
  }

  function revealAnswer() {
    var item = state.reviewQueue[state.reviewIdx];
    if (!item) return;
    state.revealed = true;
    $('rv-answer').hidden = false;
    $('rv-answer').innerHTML =
      '<p><strong>释义：</strong>' + esc(item.meaning_zh) + '</p>'
      + (item.usage ? '<p><strong>例句：</strong>' + esc(item.usage) + '</p>' : '')
      + (item.source ? '<p class="muted"><strong>来源：</strong>' + esc(item.source) + '</p>' : '');
    $('rv-reveal').hidden = true;
    $('rv-actions').hidden = false;
  }

  function markReviewed(remembered) {
    var item = state.reviewQueue[state.reviewIdx];
    if (!item) return;
    api('/api/review/mark', {
      method: 'POST',
      body: JSON.stringify({ entry_id: item.id, remembered: remembered })
    }).then(function (res) {
        toast(remembered ? '已标记记住，' + res.interval_days + ' 天后再出现' : '已标记没记住，稍后还会出现', 'ok');
        state.reviewIdx += 1;
        if (state.reviewIdx >= state.reviewQueue.length) {
          closeReview();
          toast('本轮复习完成', 'ok');
          return refreshDue();
        }
        renderReviewCard();
      })
      .catch(function (err) { toast('提交失败：' + err.message, 'error'); });
  }

  function closeReview() {
    $('rv-modal').hidden = true;
    state.reviewQueue = [];
    state.reviewIdx = 0;
    refreshDue();
  }

  // ---------- 翻译练习主流程 ----------

  /** 评分下限输入 -> 请求参数：空串=不限(null)，其余为 0~5 的数字。 */
  function parseRatingFloor(raw) {
    if (raw === '' || raw === null || raw === undefined) return null;
    var n = Number(raw);
    return isNaN(n) ? null : n;
  }

  function newPractice() {
    toast('正在准备练习…', 'ok');
    // 空串 -> null（不限），不能转成 0：0 是有效下限，与「不过滤」是两回事
    var minImp = parseRatingFloor($('pr-min-imp').value);
    var minUse = parseRatingFloor($('pr-min-use').value);
    // ★ 不再收「外刊链接」：外刊库没命中的目标词由**后端自动去网络搜**，
    //   搜到的内容不入外刊库。原先让使用者手填网址的设计已按用户要求移除。
    var payload = { terms: [] };
    if (minImp !== null) payload.min_importance = minImp;
    if (minUse !== null) payload.min_usefulness = minUse;
    // ★ 每轮词数：这轮最多练几个词（用户自选，默认 8）。
    //   parseInt 失败/空值一律兜底成 8 —— 不能把 NaN 或 null 发给后端。
    var maxTerms = parseInt($('pr-max-terms').value, 10);
    if (!isFinite(maxTerms) || maxTerms < 1) maxTerms = 8;
    payload.max_terms = maxTerms;
    return api('/api/practices', {
      method: 'POST',
      body: JSON.stringify(payload)
    })
      .then(function (prac) {
        state.practice = prac;
        state.idx = 0;
        renderPractice();
        $('p-setup').hidden = true;
        $('p-stage').hidden = false;
      })
      .catch(function (err) { toast('创建练习失败：' + err.message, 'error'); });
  }

  function renderPractice() {
    var prac = state.practice;
    if (!prac) return;
    var item = prac.items[state.idx];
    if (!item) { finishPractice(); return; }

    $('p-stage-progress').textContent = (state.idx + 1) + ' / ' + prac.items.length;
    $('p-src-term').textContent = item.term;
    // ★ 来源文案按实际情况说，别一律说「网络外刊」：
    //   source_kind = 'dictionary' 是词典站例句（iciba 的 GRE/IELTS 语料等），
    //   说成「外刊」会让用户以为那是外刊原句。
    //   'article'（或老数据没有该键）才是搜索抓到的真实文章。
    var srcKind = item.source_kind === 'dictionary' ? 'dictionary' : 'article';
    $('p-src-meta').textContent = item.from_web
      ? ('来源：' + (srcKind === 'dictionary' ? '网络例句' : '网络外刊')
         + ' · ' + (item.source_name || '') + (item.url ? ' · ' + item.url : ''))
      : ('来源：外刊库 · ' + (item.article_title || '本地文章'));
    // ★ 参考译文的来源要说实话 ★
    //   zh_from === 'user' = 用户自己粘进文章里的逐句译文（可信）；
    //   其余情况走 AI。但**没配密钥时**后端给的是英文原句而不是中文译文
    //   （app/ai.py 的 _mock_translate 不再伪造中文）——
    //   这时写「AI 参考译文」是不实描述，用户会以为那就是翻译结果。
    $('p-src-meta').textContent += item.zh_from === 'user'
      ? ' · 参考译文来自你添加的文章'
      : (prac.is_mock
        ? ' · 未配置 AI 密钥：此处为英文原句，请自行对照翻译'
        : ' · AI 参考译文');
    $('p-zh').textContent = item.text_zh;
    $('p-input').value = '';
    $('p-input').disabled = false;
    setReviewWaiting(false);
    $('p-form').hidden = false;
    $('p-result').hidden = true;
    $('p-input').focus();

    // 外刊库没命中的词给出提示。
    // ★ 说明文字优先用后端给的 prac.web_search_note ★
    //   后端知道自己到底做了什么（搜了但没搜到 / 出网已关 / 搜索链路挂了），
    //   前端自己编一句「已自动去网络搜索」在云端就是**不实描述**——
    //   云端根本没发出任何搜索请求，用户读到会以为功能坏了或网速有问题。
    //   不按云端/桌面临床分支：后端给不给说明本身就是判据。
    var miss = prac.missing_terms || [];
    $('p-missing').hidden = !miss.length;
    if (miss.length) {
      var note = (prac.web_search_note || '').trim();
      $('p-missing').textContent = '外刊库中没有找到含 ' + miss.join('、')
        + ' 的句子，本次未覆盖这些词。'
        + (note ? note : '可以把这些原文粘贴进外刊库，下次就会被覆盖。');
    }

    // ★ 「每轮词数」的实际结果说明 ★
    //   用户设了 20 个词而库里只有 5 个时，后端只给 5 个（不报错）。
    //   这里如实说出来 —— 不说的话，用户会以为是自己数错了。
    var want = prac.requested_terms, got = prac.actual_terms;
    if (want && got && got < want) {
      $('p-termcount').hidden = false;
      $('p-termcount').textContent = '你设的是每轮 ' + want + ' 个词，'
        + '库里目前有 ' + got + ' 个可以练的词，本轮就练这 ' + got + ' 个。';
    } else {
      $('p-termcount').hidden = true;
    }
  }

  /** 点评等待提示：给出预期时长，避免用户以为卡死。全程textContent，不拼 HTML。 */
  var REVIEW_WAIT_HINT = '点评中，请稍候…通常 5-20 秒，深度思考模型会更久，请不要重复提交。';

  function setReviewWaiting(on) {
    var btn = $('p-submit');
    var tip = $('p-waiting');
    btn.disabled = on;
    btn.textContent = on ? '点评中…' : '提交';
    if (tip) {
      tip.textContent = on ? REVIEW_WAIT_HINT : '';
      tip.hidden = !on;
    }
  }

  function submitTranslation() {
    if (state.submitting) return;
    var text = $('p-input').value.trim();
    if (!text) { toast('请先输入你的翻译', 'error'); return; }
    state.submitting = true;
    setReviewWaiting(true);
    var startedAt = Date.now();

    // 带上当前题号与本句命中词条：练习是逐句作答的，点评与排期都只针对这一句。
    // 少了 seq 后端会拿整篇 passage 去比对，保真度会因「漏翻其余几句」乱扣分。
    var item = state.practice.items[state.idx] || {};
    api('/api/practices/' + state.practice.id + '/submit', {
      method: 'POST',
      body: JSON.stringify({
        user_text: text,
        seq: state.idx + 1,
        terms: item.terms || []
      })
    })
      .then(function (res) {
        var secs = ((Date.now() - startedAt) / 1000).toFixed(1);
        renderResult(res);
        // 把真实耗时显示出来：用户自己就能判断优化是否生效，不用靠猜
        var tip = $('p-waiting');
        if (tip) {
          tip.textContent = '本次点评耗时 ' + secs + ' 秒'
            + (res.is_mock ? '（演示模式，未真实调用）' : '');
          tip.hidden = false;
        }
      })
      .catch(function (err) {
        // 失败时恢复可提交状态，否则按钮永久停在「点评中…」，用户只能刷新页面
        setReviewWaiting(false);
        toast('点评失败：' + err.message, 'error');
      })
      .then(function () { state.submitting = false; });
  }

  function renderResult(res) {
    var sc = res.scores;
    $('p-form').hidden = true;
    $('p-result').hidden = false;
    $('p-input').disabled = true;

    var dims = [['grammar', '语法正确'], ['fluency', '表达地道'], ['fidelity', '语义保真']];
    $('p-scores').innerHTML = dims.map(function (d) {
      var s = sc[d[0]];
      var pct = Math.round(s.score / s.max * 100);
      return '<div class="score-dim">'
        + '<div class="score-dim-head"><span>' + d[1] + '</span>'
        + '<strong>' + s.score + ' / ' + s.max + '</strong></div>'
        + '<div class="score-bar"><i style="width:' + pct + '%"></i></div>'
        + (s.issues && s.issues.length
            ? '<ul class="score-issues">' + s.issues.map(function (i) {
                return '<li>' + esc(i) + '</li>';
              }).join('') + '</ul>'
            : '<p class="muted score-ok">' + esc(s.comment || '无明显问题') + '</p>')
        + '</div>';
    }).join('');
    $('p-total').textContent = res.scores.total;
    $('p-suggestion').innerHTML = '<strong>更优表达：</strong>' + esc(res.suggestion || '（无）');

    var mockNote = res.is_mock ? '<p class="mock-note">演示模式：以上为占位评分，配置 AI 密钥后为真实点评。</p>' : '';
    $('p-result-extra').innerHTML = mockNote
      + '<p class="muted"><strong>英文原文：</strong>' + esc(res.source_en) + '</p>'
      + '<p class="muted"><strong>参考译文：</strong>' + esc(res.reference_zh) + '</p>'
      + renderReviewUpdates(res);

    $('p-next').textContent = (state.idx + 1 >= state.practice.items.length) ? '完成本轮' : '下一条';
  }

  /** 翻译即复习：把本次翻译造成的排期变化讲清楚，让用户知道词条何时再见。 */
  function renderReviewUpdates(res) {
    var ups = res.review_updates;
    if (!ups || !ups.length) {
      // 演示模式或本轮没带词汇库词条时，不给"排期已更新"的错觉
      return res.is_mock
        ? '<p class="muted">演示模式不更新复习排期——配置 AI 密钥后才按评分自动排期。</p>'
        : '';
    }
    var items = ups.map(function (u) {
      var when = u.remembered
        ? (u.interval_days > 0 ? u.interval_days + ' 天后再练' : '今天内会再出现')
        : '明天再练（本次没掌握，已重排）';
      return '<li><strong>' + esc(u.term) + '</strong>：'
        + (u.remembered ? '记住了，' : '没记住，') + esc(when) + '</li>';
    }).join('');
    return '<div class="review-updates"><p><strong>复习排期已更新</strong>'
      + '（总分 ≥ ' + esc(res.pass_score) + ' 分记为记住）</p><ul>' + items + '</ul></div>';
  }

  function nextItem() {
    state.idx += 1;
    if (state.idx >= state.practice.items.length) { finishPractice(); return; }
    renderPractice();
  }

  function finishPractice() {
    state.practice = null;
    $('p-stage').hidden = true;
    $('p-setup').hidden = false;
    toast('本轮练习完成', 'ok');
    return loadHistory().then(refreshDue).then(refreshStats);
  }

  function loadHistory() {
    return api('/api/practices?page=' + state.page + '&page_size=' + state.pageSize)
      .then(function (d) {
        state.total = d.total;
        renderHistory(d.items);
        // ★ 把最近一次列表挂到模块导出上，供骨架轴侧栏复用 ★
        //   侧栏（漫威 Recent Issues）要显示最近 4 次练习得分。
        //   它**不能自己再发一次** /api/practices —— 在 jsdom 测试里那次请求会
        //   抢在练习模块之前进 mock 的 calls 队列，把「列表请求带 page_size=25」
        //   的断言取成 page_size=4；包装 fetch 去监听更脆（响应流被读空、
        //   历史列表渲染成 0 条，见qa_practice_detail）。
        //   这里只暴露一个只读引用：谁渲染谁负责，零额外请求、零副作用。
        if (window.EnPracticePractice) { window.EnPracticePractice.recentItems = d.items || []; }
        // 侧栏（漫威 Recent Issues）用的是这份数据，渲染完得让它重画（强制丢弃缓存）
        if (window.EnPracticeRails) { window.EnPracticeRails.refresh(true); }
        $('p-count').textContent = d.total ? '共 ' + d.total + ' 次练习' : '';
        $('p-empty').hidden = d.total !== 0;
        return api('/api/practices/stats');
      })
      .then(function (st) {
        if (!st.count) { $('p-trend').textContent = ''; return; }
        var parts = ['练习 ' + st.count + ' 次'];
        if (st.avg_total != null) parts.push('平均 ' + st.avg_total + ' 分');
        if (st.best_total != null) parts.push('最高 ' + st.best_total + ' 分');
        if (st.first_total != null && st.last_total != null && st.count > 1) {
          var delta = st.last_total - st.first_total;
          parts.push('首次 ' + st.first_total + ' → 最近 ' + st.last_total
                     + '（' + (delta >= 0 ? '+' : '') + delta + '）');
        }
        $('p-trend').textContent = parts.join(' · ');
      })
      .catch(function (err) { toast('历史加载失败：' + err.message, 'error'); });
  }

  // ---------- 练习记录详情（点历史记录里的任意一条打开） ----------

  // 三个评分维度的展示口径：顺序、名称、满分都与量表一致（语法30/地道30/保真40）。
  var PRACTICE_DIMS = [
    { key: 'grammar', label: '语法正确', max: 30, scoreField: 'score_grammar' },
    { key: 'fluency', label: '表达地道', max: 30, scoreField: 'score_fluency' },
    { key: 'fidelity', label: '语义保真', max: 40, scoreField: 'score_fidelity' }
  ];

  /**
   * 解析存库的 ai_feedback（模型输出的 JSON 文本）。
   * 坏数据（不是合法 JSON / 结构不对）一律降级成空对象：
   * 一条脏记录不该让整个详情页打不开。
   */
  function parseFeedback(raw) {
    if (!raw) return {};
    try {
      var obj = JSON.parse(raw);
      return (obj && typeof obj === 'object') ? obj : {};
    } catch (e) {
      return {};
    }
  }

  /** 把一个维度评分的 issues 展开成 <li>。issues 是模型原话，一律 textContent 写入。 */
  function buildIssueList(host, dim) {
    var issues = dim && Array.isArray(dim.issues) ? dim.issues : [];
    if (!issues.length) return false;
    var ul = document.createElement('ul');
    ul.className = 'score-issues';
    issues.forEach(function (text) {
      var li = document.createElement('li');
      li.textContent = String(text == null ? '' : text);
      ul.appendChild(li);
    });
    host.appendChild(ul);
    return true;
  }

  function renderPracticeDetail(d) {
    $('p-detail-meta').textContent = (d.created_at || '').replace('T', ' ').slice(0, 16);

    // 原句 / 参考译文 / 当时的翻译：都是自由文本，textContent 写入。
    $('p-detail-en').textContent = d.text_en || '（没有原句记录）';
    $('p-detail-zh').textContent = d.text_zh || '（没有参考译文）';
    $('p-detail-user').textContent = d.user_text || '（没有翻译记录）';

    var fb = parseFeedback(d.ai_feedback);
    var host = $('p-detail-scores');
    host.textContent = '';
    PRACTICE_DIMS.forEach(function (spec) {
      // null/undefined 是「这一维度没评分」，不是 0 分 —— 必须先判空再转数字。
      // Number(null) === 0，直接 Number(...) 会把「未评分」显示成「0 / 30」，
      // 让人误以为自己考了 0 分。isFinite 兜住空串与非法字符串。
      var raw = d[spec.scoreField];
      var num = (raw === null || raw === undefined || raw === '') ? null : Number(raw);
      if (num !== null && !isFinite(num)) num = null;
      var pct = num === null ? 0 : Math.max(0, Math.min(100, num / spec.max * 100));

      var box = document.createElement('div');
      box.className = 'score-dim';

      var head = document.createElement('div');
      head.className = 'score-dim-head';
      var name = document.createElement('span');
      name.textContent = spec.label;
      var val = document.createElement('strong');
      val.textContent = (num === null ? '—' : num) + ' / ' + spec.max;
      head.appendChild(name);
      head.appendChild(val);
      box.appendChild(head);

      var bar = document.createElement('div');
      bar.className = 'score-bar';
      var fill = document.createElement('i');
      fill.style.width = pct.toFixed(1) + '%';
      bar.appendChild(fill);
      box.appendChild(bar);

      var dim = fb[spec.key];
      if (!buildIssueList(box, dim)) {
        // 没有具体 issues 时退回该维度的总评 comment（同样是模型原话）
        var ok = document.createElement('p');
        ok.className = 'muted score-ok';
        ok.textContent = (dim && dim.comment) ? String(dim.comment) : '无明显问题';
        box.appendChild(ok);
      }
      host.appendChild(box);
    });

    // 更优表达建议：模型原话，textContent 写入。
    $('p-detail-suggestion').textContent = fb.suggestion ? String(fb.suggestion) : '（本次没有给出建议）';

    renderRevisions(d.revisions);
    resetDiscuss();
    // 记录这次打开的是哪一条、以及用户看到的旧分：讨论与采纳都以此为准，
    // 采纳时把它原样回传，后端据此判定并发冲突。
    discussState.recordId = d.id;
    discussState.old = {
      score_grammar: d.score_grammar,
      score_fluency: d.score_fluency,
      score_fidelity: d.score_fidelity,
      score_total: d.score_total
    };
  }

  // ---------- 和 AI 讨论本题 ----------

  /**
   * 讨论状态。
   *
   * 为什么要独立于 DOM：采纳要拿 expected_old 做并发比对，必须记住「用户当时看到的
   * 那一组分值」；而这个值在 renderPracticeDetail 后仍然有效，不该每次渲染都重置。
   * discussRecordId 同时用来挡住「切到另一条记录后，飞行中的旧响应回来把新记录
   * 的弹窗内容覆盖掉」——与 detailToken 是同一个道理，但这里防的是"内容串台"。
   */
  var discussState = {
    recordId: null, reply: '', scores: null, total: null,
    busy: false,
    // 用户看到的那组分值。采纳时原样回传，后端据此做并发比对。
    // 存在 discussState 而不是 renderRevisedScores 的局部变量里：采纳按钮的
    // 点击回调拿不到渲染时的局部作用域，而它必须拿到**同一组**旧分。
    old: null
  };

  /** 打开/切换记录时清空讨论区：上一条记录的提问与修正分不能带到下一条。 */
  function resetDiscuss() {
    discussState.recordId = null;
    discussState.reply = '';
    discussState.scores = null;
    discussState.total = null;
    discussState.busy = false;
    discussState.old = null;
    $('pd-question').value = '';
    setDiscussStatus('');
    $('pd-discuss-result').hidden = true;
    $('pd-revised').hidden = true;
    $('pd-adopt-status').textContent = '';
    $('pd-discuss-send').disabled = false;
    $('pd-discuss-send').textContent = '让 AI 回答';
  }

  function setDiscussStatus(msg) {
    var el = $('pd-discuss-status');
    if (el) el.textContent = msg || '';
  }

  /** 渲染修正评分：旧分 → 新分的对照。数字与标签都走 textContent / DOM。 */
  function renderRevisedScores(scores, total, before) {
    var host = $('pd-revised-scores');
    host.textContent = '';
    var old = before || {};

    var rows = [
      { key: 'grammar', label: '语法正确', max: 30 },
      { key: 'fluency', label: '表达地道', max: 30 },
      { key: 'fidelity', label: '语义保真', max: 40 }
    ];

    rows.forEach(function (spec) {
      var next = Number(scores[spec.key]);
      var prevRaw = old['score_' + spec.key];
      var prev = (prevRaw === null || prevRaw === undefined || prevRaw === '')
        ? null : Number(prevRaw);

      var box = document.createElement('div');
      box.className = 'pd-rev-row';

      var name = document.createElement('span');
      name.textContent = spec.label;
      box.appendChild(name);

      var val = document.createElement('strong');
      val.textContent = next + ' / ' + spec.max;
      box.appendChild(val);

      // 对照只在有旧分时才有意义：原来就没分的记录，"从 — 到 20"是噪音
      if (prev !== null && isFinite(prev)) {
        var diff = next - prev;
        var delta = document.createElement('span');
        delta.className = 'pd-rev-delta '
          + (diff > 0 ? 'pd-rev-delta-up' : (diff < 0 ? 'pd-rev-delta-down' : 'pd-rev-delta-same'));
        delta.textContent = '（原 ' + prev + '，'
          + (diff > 0 ? '+' + diff : (diff < 0 ? String(diff) : '±0')) + '）';
        box.appendChild(delta);
      }
      host.appendChild(box);
    });

    var totalBox = document.createElement('div');
    totalBox.className = 'pd-rev-row';
    var totalName = document.createElement('span');
    totalName.textContent = '总分';
    var totalVal = document.createElement('strong');
    totalVal.textContent = total + ' / 100';
    totalBox.appendChild(totalName);
    totalBox.appendChild(totalVal);

    var prevTotal = (old.score_total === null || old.score_total === undefined
      || old.score_total === '') ? null : Number(old.score_total);
    if (prevTotal !== null && isFinite(prevTotal)) {
      var dt = Number(total) - prevTotal;
      var tdelta = document.createElement('span');
      tdelta.className = 'pd-rev-delta '
        + (dt > 0 ? 'pd-rev-delta-up' : (dt < 0 ? 'pd-rev-delta-down' : 'pd-rev-delta-same'));
      tdelta.textContent = '（原 ' + prevTotal + '，'
        + (dt > 0 ? '+' + dt : (dt < 0 ? String(dt) : '±0')) + '）';
      totalBox.appendChild(tdelta);
    }
    host.appendChild(totalBox);
  }

  /** 渲染采纳修正的留痕（后端 ai_feedback.revisions 解析出来的数组）。 */
  function renderRevisions(revisions) {
    var block = $('p-detail-revisions-block');
    var host = $('p-detail-revisions');
    host.textContent = '';

    if (!Array.isArray(revisions) || !revisions.length) {
      block.hidden = true;
      return;
    }
    block.hidden = false;

    revisions.slice().reverse().forEach(function (rev) {
      var item = document.createElement('div');
      item.className = 'pd-rev-item';

      var when = document.createElement('div');
      when.className = 'pd-rev-when';
      when.textContent = String(rev.at || '').replace('T', ' ').slice(0, 16);
      item.appendChild(when);

      // 用户的质疑原文 —— 自由文本，textContent 写入
      if (rev.question) {
        var q = document.createElement('div');
        q.className = 'pd-rev-q';
        q.textContent = '你的质疑：' + String(rev.question);
        item.appendChild(q);
      }

      // AI 回复 —— 不可信外部文本，同样只能 textContent
      if (rev.reply) {
        var r = document.createElement('div');
        r.className = 'pd-text';
        r.textContent = String(rev.reply);
        item.appendChild(r);
      }

      var before = rev.before || {};
      var after = rev.after || {};
      var line = document.createElement('div');
      line.className = 'pd-rev-line';
      line.textContent = '评分由 语法 ' + (before.score_grammar === undefined ? '—' : before.score_grammar)
        + ' / 地道 ' + (before.score_fluency === undefined ? '—' : before.score_fluency)
        + ' / 保真 ' + (before.score_fidelity === undefined ? '—' : before.score_fidelity)
        + '（总分 ' + (before.score_total === undefined ? '—' : before.score_total)
        + '）改为 语法 ' + (after.grammar === undefined ? '—' : after.grammar)
        + ' / 地道 ' + (after.fluency === undefined ? '—' : after.fluency)
        + ' / 保真 ' + (after.fidelity === undefined ? '—' : after.fidelity)
        + '（总分 ' + (after.total === undefined ? '—' : after.total) + '）';
      item.appendChild(line);

      host.appendChild(item);
    });
  }

  function sendDiscuss() {
    if (discussState.busy) return;
    var input = $('pd-question');
    var question = input.value.trim();
    if (!question) { toast('请先写下你的疑问', 'error'); return; }

    var id = discussState.recordId;
    if (!id) return;

    discussState.busy = true;
    $('pd-discuss-send').disabled = true;
    $('pd-discuss-send').textContent = 'AI 回答中…';
    setDiscussStatus('AI 正在作答，请稍候…通常 5-20 秒。');

    var asked = question;
    api('/api/practices/' + id + '/discuss', {
      method: 'POST',
      body: JSON.stringify({ question: asked })
    })
      .then(function (res) {
        // 记录已经切到别条 → 这次响应作废，别把上一条的答案贴到新记录上
        if (discussState.recordId !== id) return;

        discussState.busy = false;
        $('pd-discuss-send').disabled = false;
        $('pd-discuss-send').textContent = '让 AI 回答';
        setDiscussStatus('');

        // reply 是模型原话（不可信文本）→ 只用 textContent，绝不 innerHTML
        $('pd-reply').textContent = res && res.reply
          ? String(res.reply) : '（AI 没有给出文字回答）';
        $('pd-discuss-result').hidden = false;

        if (res && res.revised && res.scores) {
          discussState.reply = String(res.reply || '');
          discussState.scores = res.scores;
          discussState.total = res.total;
          renderRevisedScores(res.scores, res.total, discussState.old || {});
          $('pd-revised').hidden = false;
          $('pd-adopt').disabled = false;
          $('pd-adopt-status').textContent = '';
        } else {
          // revised=false 或模型漏给 scores：**不提供采纳入口**。
          // 纯讨论到此为止，原评分保持不变。
          discussState.scores = null;
          discussState.total = null;
          $('pd-revised').hidden = true;
          $('pd-adopt-status').textContent = '';
        }
      })
      .catch(function (err) {
        if (discussState.recordId !== id) return;
        discussState.busy = false;
        $('pd-discuss-send').disabled = false;
        $('pd-discuss-send').textContent = '让 AI 回答';
        setDiscussStatus('');
        // 降级而不是白屏：把后端的错误原文（未配密钥 / 返回格式不对等）显示出来，
        // 并保留提问内容让用户改一改再试。
        $('pd-discuss-result').hidden = false;
        $('pd-reply').textContent = '讨论失败：' + err.message;
        $('pd-revised').hidden = true;
      });
  }

  function adoptRevised() {
    var id = discussState.recordId;
    if (!id || !discussState.scores || discussState.busy) return;

    var btn = $('pd-adopt');
    var status = $('pd-adopt-status');
    discussState.busy = true;
    btn.disabled = true;
    btn.textContent = '写入中…';
    status.textContent = '';

    var payload = {
      scores: discussState.scores,
      total: discussState.total,
      question: $('pd-question').value.trim(),
      reply: discussState.reply,
      // 用户看到的那组旧分：后端据此判定并发冲突，不覆盖别人的写入
      expected_old: discussState.old || null
    };

    api('/api/practices/' + id + '/revise-scores', {
      method: 'PATCH',
      body: JSON.stringify(payload)
    })
      .then(function (res) {
        discussState.busy = false;
        btn.disabled = false;
        btn.textContent = '采纳修正评分';

        if (!res || !res.scores) {
          status.textContent = '写入失败：后端没有返回新评分';
          return;
        }
        var s = res.scores;
        toast('已采纳修正评分（总分 ' + s.total + '）', 'ok');
        status.textContent = '已写入：语法 ' + s.grammar + ' / 地道 ' + s.fluency
          + ' / 保真 ' + s.fidelity + '，总分 ' + s.total;
        // 已经采纳过了：把采纳入口收起来，避免同一份修正分被重复写入
        $('pd-revised').hidden = true;
        // 重新拉详情：分数、留痕都从服务端拿，不在前端自己改 DOM 数值
        openPracticeDetail(id);
        loadHistory();
      })
      .catch(function (err) {
        discussState.busy = false;
        btn.disabled = false;
        btn.textContent = '采纳修正评分';
        // 非法分数 / 并发冲突都会落到这里：明确报错，**不静默夹取**
        if (err.status === 409) {
          // 冲突：详情已过期，重新拉一次让用户看到最新分数。
          // 注意顺序 —— openPracticeDetail 会重置整个讨论区（含状态行），
          // 所以必须等它跑完再写提示，否则这句"未改动"会被它清掉，用户一脸茫然。
          return openPracticeDetail(id).then(function () {
            $('pd-adopt-status').textContent = '未写入：' + err.message;
          });
        }
        status.textContent = '未写入：' + err.message;
      });
  }

  $('pd-discuss-send').addEventListener('click', sendDiscuss);
  $('pd-adopt').addEventListener('click', adoptRevised);

  // 提问框内 Ctrl/Cmd + Enter 直接发送，与翻译框的手感一致
  $('pd-question').addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') sendDiscuss();
  });

  // 详情请求的令牌。用来作废「过期」的响应：连点两条记录时，先发的请求可能后到，
  // 若不挡一下就会把后点的那条覆盖掉；关掉弹窗后仍在飞的响应也会把弹窗重新弹出来。
  var detailToken = 0;

  function openPracticeDetail(id) {
    detailToken += 1;
    var token = detailToken;
    return api('/api/practices/' + id)
      .then(function (d) {
        if (token !== detailToken) return;   // 已有更新的请求，丢弃这次响应
        renderPracticeDetail(d);
        $('p-detail').hidden = false;
      })
      .catch(function (err) {
        if (token !== detailToken) return;
        toast('读取练习详情失败：' + err.message, 'error');
      });
  }

  function closePracticeDetail() {
    // 令牌 +1：让还在飞的请求回来时发现自己已过期，不会把弹窗重新弹出来
    detailToken += 1;
    $('p-detail').hidden = true;
  }

  function renderHistory(items) {
    var host = $('p-history');
    host.textContent = '';
    items.forEach(function (p) {
      var when = (p.created_at || '').replace('T', ' ').slice(0, 16);
      var total = p.score_total == null ? '—' : p.score_total;

      // 整条记录可点击 → 打开详情弹窗。
      // role/tabindex 让它也能用键盘触发，不只是鼠标。
      var card = document.createElement('div');
      card.className = 'prac-item prac-item-open';
      card.setAttribute('role', 'button');
      card.setAttribute('tabindex', '0');
      card.setAttribute('data-id', String(p.id));

      var head = document.createElement('div');
      head.className = 'prac-item-head';
      var whenEl = document.createElement('span');
      whenEl.textContent = when;
      var scoreEl = document.createElement('span');
      scoreEl.className = 'prac-item-score';
      scoreEl.textContent = total + ' 分';
      head.appendChild(whenEl);
      head.appendChild(scoreEl);
      card.appendChild(head);

      // 我的翻译：用户自己写的自由文本 → textContent，绝不拼 innerHTML。
      var text = document.createElement('p');
      text.className = 'prac-item-text';
      var em = document.createElement('em');
      em.textContent = '我的翻译：';
      text.appendChild(em);
      text.appendChild(document.createTextNode((p.user_text || '').slice(0, 160)));
      card.appendChild(text);

      var terms = document.createElement('p');
      terms.className = 'prac-item-terms';
      terms.textContent = '语法 ' + (p.score_grammar == null ? '—' : p.score_grammar) + '/30 · '
        + '地道 ' + (p.score_fluency == null ? '—' : p.score_fluency) + '/30 · '
        + '保真 ' + (p.score_fidelity == null ? '—' : p.score_fidelity) + '/40';
      card.appendChild(terms);

      var more = document.createElement('span');
      more.className = 'prac-item-more';
      more.textContent = '查看完整点评 ›';
      card.appendChild(more);

      host.appendChild(card);
    });
  }

  // 点历史记录里的任意一条 → 打开详情。用事件委托：整块列表只挂一个监听器，
  // 列表重绘后也不必重新绑定。
  $('p-history').addEventListener('click', function (e) {
    var card = e.target.closest ? e.target.closest('.prac-item') : null;
    if (!card) return;
    var id = parseInt(card.getAttribute('data-id'), 10);
    if (id) openPracticeDetail(id);
  });

  // 键盘可达：焦点在记录上时按回车/空格同样打开详情。
  $('p-history').addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var card = e.target.closest ? e.target.closest('.prac-item') : null;
    if (!card) return;
    e.preventDefault();
    var id = parseInt(card.getAttribute('data-id'), 10);
    if (id) openPracticeDetail(id);
  });

  // ---------- 每日定时提醒 ----------

  var reminderTick = null;
  var lastReminderKey = '';
  function startReminderWatch() {
    if (reminderTick) return;
    loadReviewSettings();
    reminderTick = setInterval(function () {
      var want = $('pr-time').value || '20:00';
      var now = new Date();
      var hhmm = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
      var key = now.toDateString() + '|' + want;
      if (hhmm === want && key !== lastReminderKey) {
        lastReminderKey = key;
        refreshDue().then(function (n) {
          if (n > 0) {
            toast('该复习了：有 ' + n + ' 个词条到期', 'ok');
            if ('Notification' in window && Notification.permission === 'granted') {
              new Notification('我的英文库 MyEnglishVault', { body: '有 ' + n + ' 个词条待复习' });
            }
          }
        });
      }
    }, 30000);  // 30 秒粒度，足够"到点提醒"且不扰频
  }

  // ---------- 事件绑定 ----------

  $('pr-start').addEventListener('click', startReview);
  $('pr-time').addEventListener('change', saveReviewSettings);
  $('pr-mode').addEventListener('change', saveReviewSettings);
  $('rv-reveal').addEventListener('click', revealAnswer);
  $('rv-remember').addEventListener('click', function () { markReviewed(true); });
  $('rv-forgot').addEventListener('click', function () { markReviewed(false); });
  $('rv-close').addEventListener('click', closeReview);

  $('p-new').addEventListener('click', newPractice);
  $('p-submit').addEventListener('click', submitTranslation);
  $('p-next').addEventListener('click', nextItem);
  $('p-giveup').addEventListener('click', function () {
    state.practice = null;
    $('p-stage').hidden = true;
    $('p-setup').hidden = false;
  });

  // 练习记录详情弹窗的关闭：按钮 / 点遮罩 / Escape，三种方式与全站其他弹窗一致
  $('p-detail-close').addEventListener('click', closePracticeDetail);
  $('p-detail').addEventListener('click', function (e) {
    if (e.target === this) closePracticeDetail();
  });

  // 翻译框内 Ctrl/Cmd + Enter 直接提交
  $('p-input').addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitTranslation();
  });

  // Escape 关闭复习卡片（与词汇/外刊/设置弹窗行为一致）
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (!$('p-detail').hidden) { closePracticeDetail(); return; }
    if (!$('rv-modal').hidden) closeReview();
  });

  // ---------- 启动 ----------
  // 等部署形态到手再渲染首屏提示：形态决定提示怎么写、密钥从哪读。
  var cloudReady = (window.EnPracticeCloud && window.EnPracticeCloud.ready) || null;
  var boot = cloudReady ? cloudReady : Promise.resolve();
  boot.then(function () { return loadAiStatus(); }).then(refreshDue).then(loadHistory);
  startReminderWatch();

  // 供设置模块保存后刷新演示模式提示
  window.EnPracticePractice = { reloadAiStatus: loadAiStatus, recentItems: [] };
})();

// ---------------------------------------------------------------------------
// 界面主题：7 个方向共用一套结构，切换只改 <html data-theme> 上的语义变量。
//
// 三条硬约束：
// 1) 主题 id 来自 settings.json（用户手可改）—— 一律走 setAttribute /
//    textContent，绝不拼 innerHTML，否则 settings.json 就是 XSS 入口。
// 2) 写盘必须复用 settingsModule 的 PUT /api/settings 通道，不另起一套。
// 3) --accent-on 由各主题在 CSS 里写死（按实测对比度），运行时绝不按亮度猜。
// ---------------------------------------------------------------------------
(function themeModule() {
  var DEFAULT_THEME = 'reading';

  var THEMES = [
    { id: 'reading', name: '朴素', desc: '纸灰底 + 单一松绿 · 项目本来的样子',
      sw: ['#f4f4f0', '#1d6b51', '#ffffff', '#1f211d'] },
    { id: 'starry', name: '星月夜', desc: '普鲁士蓝深底 + 铬黄一点高亮',
      sw: ['#1b3a5c', '#e8b23a', '#2d5c86', '#f2e6c8'] },
    { id: 'sunflower', name: '向日葵', desc: '铬黄主色 + 深墨文字 · 控件全圆角',
      sw: ['#e3a712', '#a8660a', '#f6e3b0', '#2a2118'] },
    { id: 'vesper', name: '克制', desc: '暖白纸面 + 全站只一个彩色',
      sw: ['#faf9f6', '#14161a', '#ffffff', '#c1443c'] },
    { id: 'mona', name: '蒙娜丽莎', desc: '深褐 + 旧金 · 直角边框',
      sw: ['#1a1410', '#b08d4f', '#2a2016', '#efe4d0'] },
    { id: 'vampire', name: '吸血鬼日记', desc: '近黑紫 + 血红 · 小圆角',
      sw: ['#0d0a0f', '#c0392f', '#221b2c', '#e8e0d4'] },
    { id: 'marvel', name: '漫威', desc: '米白纸面 + 漫威红 + 亮黄',
      sw: ['#f2efe6', '#e62429', '#ffffff', '#0d0d10'] },
    { id: 'silver', name: '银塑', desc: '高级感银灰 + 银色雕像 · 流金浅粉点缀',
      sw: ['#b9bec6', '#56430a', '#fafbfc', '#7a2f40'] }
  ];

  function isValid(id) {
    return THEMES.some(function (t) { return t.id === id; });
  }

  // ------------------------------------------------------------------
  // 骨架轴：主题 → 布局的映射。
  // 六个设计方向的骨架差异实测归纳为两个正交轴：
  //   ① 导航位置（顶栏横排 / 左侧竖排）
  //   ② 主区列结构（1 / 2 / 3 列及其列宽）
  // 列宽逐字取自 design-explore 里对应候选的 grid-template-columns：
  //   星月夜   216px 1fr                （左侧导航，无顶栏）
  //   克制     232px 1fr+246px 指标栏   （左侧导航，无顶栏）
  //   向日葵   顶栏 66px+1fr 320px 花盘
  //   蒙娜丽莎 236px minmax(0,1fr) 264px（肖像 / 主区 / 画廊说明）
  //   吸血鬼   250px minmax(0,1fr)     （左状态栏）
  //   漫威     1fr 340px               （右数字栏）
  // 朴素是「应用原本的样子」，data-layout="top"，不挂任何布局规则。
  // 布局不落库：它由主题唯一决定，落一份就多一个能和主题打架的状态。
  // ------------------------------------------------------------------
  var DEFAULT_LAYOUT = 'top';
  var LAYOUTS = {
    reading: 'top',
    starry: 'sidebar',
    sunflower: 'hero-disc',
    vesper: 'sidebar-metrics',
    mona: 'gallery',
    vampire: 'status-left',
    marvel: 'comic',
    silver: 'sidebar'      /* 与星月夜同骨架：左侧竖排导航 216px */
  };

  function layoutFor(themeId) {
    var id = normalize(themeId);
    return Object.prototype.hasOwnProperty.call(LAYOUTS, id) ? LAYOUTS[id] : DEFAULT_LAYOUT;
  }

  /** 独立入口：非法布局值一律回落 top（朴素骨架）。 */
  function normalizeLayout(v) {
    var all = Object.keys(LAYOUTS).map(function (k) { return LAYOUTS[k]; });
    return typeof v === 'string' && all.indexOf(v) >= 0 ? v : DEFAULT_LAYOUT;
  }

  /** 只接受白名单里的 id；其余（含空串/带空格/超长/非字符串）一律回落默认。 */
  function normalize(id) {
    return typeof id === 'string' && isValid(id) ? id : DEFAULT_THEME;
  }

  function current() {
    return normalize(document.documentElement.getAttribute('data-theme'));
  }

  function applyLayout(name) {
    var safe = normalizeLayout(name);
    document.documentElement.setAttribute('data-layout', safe);
    if (window.EnPracticeRails) { window.EnPracticeRails.refresh(); }
    return safe;
  }

  function applyTheme(id) {
    var safe = normalize(id);
    document.documentElement.setAttribute('data-theme', safe);
    applyLayout(layoutFor(safe));
    // ★ 主题一换，大标题的有无就变了（朴素/克制不显示）★
    //   切主题不走 switchModule，所以这里必须显式刷一遍标题，
    //   否则会出现「主题换了但大标题还在/该没」。
    if (typeof applyAllPageTitles === 'function') { applyAllPageTitles(); }
    document.querySelectorAll('#s-theme-grid [data-theme-id]').forEach(function (b) {
      b.setAttribute('aria-checked', String(b.getAttribute('data-theme-id') === safe));
    });
    return safe;
  }

  function themeById(id) {
    return THEMES.filter(function (t) { return t.id === id; })[0] || THEMES[0];
  }

  /** 建色板按钮。全部用 createElement + textContent，不碰 innerHTML。 */
  function renderPicker() {
    var host = $('s-theme-grid');
    if (!host) { return; }
    host.textContent = '';
    THEMES.forEach(function (t) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'theme-opt';
      btn.setAttribute('role', 'radio');
      btn.setAttribute('data-theme-id', t.id);
      btn.title = t.desc;

      var sw = document.createElement('span');
      sw.className = 'theme-opt-sw';
      sw.setAttribute('aria-hidden', 'true');
      t.sw.forEach(function (c) {
        var cell = document.createElement('i');
        cell.style.background = c;
        sw.appendChild(cell);
      });

      var box = document.createElement('span');
      box.className = 'theme-opt-text';
      var name = document.createElement('span');
      name.className = 'theme-opt-name';
      name.textContent = t.name;
      var desc = document.createElement('span');
      desc.className = 'theme-opt-desc';
      desc.textContent = t.desc;
      box.appendChild(name);
      box.appendChild(desc);

      btn.appendChild(sw);
      btn.appendChild(box);
      btn.addEventListener('click', function () { pick(t.id); });
      host.appendChild(btn);
    });
    applyTheme(current());
  }

  /** 切换：先本地生效（不等网络），再走 settingsModule 的保存通道写回。 */
  function pick(id) {
    var safe = applyTheme(id);
    if (window.EnPracticeSettings) { window.EnPracticeSettings.saveTheme(safe); }
    else {
      // 保存通道尚未就绪（极早期的点击）：退回裸 api，仍是同一个后端接口。
      api('/api/settings', { method: 'PUT', body: JSON.stringify({ ui: { ui_theme: safe } }) })
        .catch(function () { /* 主题已生效，写盘失败不该回滚观感 */ });
    }
  }

  /** 启动回放：先按默认上色避免闪白，再读 settings.json 里的 ui_theme 覆盖。 */
  function bootstrap() {
    applyTheme(DEFAULT_THEME);
    renderPicker();
    return fetch('/api/settings')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) {
        var ui = s && s.ui;
        return applyTheme(normalize(ui && ui.ui_theme));
      })
      .catch(function () { /* 读不到设置就用默认主题，不打断启动 */ });
  }

  window.EnPracticeTheme = {
    applyTheme: applyTheme, applyLayout: applyLayout, bootstrap: bootstrap,
    THEMES: THEMES, LAYOUTS: LAYOUTS, layoutFor: layoutFor, DEFAULT_LAYOUT: DEFAULT_LAYOUT
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();

// ---------------------------------------------------------------------------
// 骨架轴侧栏的真实数据。
//
// 六个方向的侧栏语义各不相同（花盘 / 指标栏 / 肖像 / 画廊说明 / 状态栏 /
// 数字栏），但它们**引用的统计口径是同一批** —— 所以只取一次数、一次填所有槽。
//
// 三条纪律：
// 1) 一个字面量都不写死。设计稿上的 51 / 7 / 82.9 / 57 是当时那台机器的快照，
//    写进代码就变成假数据；这里全部来自接口。
// 2) 「近 30 天练习天数」必须真的按 trend 里 practice_count>0 数，
//    不能拿 active_days 顶替（那是别的口径）。
// 3) 某一项取不到就显示「—」，绝不显示 0。「没练过」和「考了 0 分」是两件事。
// ---------------------------------------------------------------------------
(function railsModule() {
  var last = null;          // 最近一次成功取到的统计，供切布局时免重取
  var inflight = null;

  function $(id) { return document.getElementById(id); }

  function fmt(v, suffix) {
    if (v === null || v === undefined || v === '') { return '—'; }
    var n = Number(v);
    var s = isNaN(n) ? String(v) : (Math.round(n * 10) / 10).toString();
    return suffix ? s + suffix : s;
  }

  /** 把 {due,count,avg,best,entries,days,recent} 铺到所有 data-stat 槽位上。 */
  function paint(d) {
    var vals = {
      due: fmt(d.due),
      count: fmt(d.count),
      avg: d.count ? fmt(d.avg) : '—',
      best: d.count ? fmt(d.best) : '—',
      entries: fmt(d.entries),
      days: fmt(d.days),
      // 今日平均：今天没练就是没有值，显示「—」是对的（不能拿历史平均冒充）
      todayAvg: (d.todayAvg === null || d.todayAvg === undefined) ? '—' : fmt(d.todayAvg)
    };
    document.querySelectorAll('[data-stat]').forEach(function (el) {
      var k = el.getAttribute('data-stat');
      if (Object.prototype.hasOwnProperty.call(vals, k)) {
        el.textContent = vals[k];
      }
    });
    // 侧栏导航底部那个平均分带自己的 ::after 说明，数字本体不带单位
    var nav = $('nav-score');
    if (nav) { nav.textContent = vals.avg; }

    // 漫威的 Recent Issues：最近 4 次练习的真实得分
    var list = $('mv-issues');
    if (list) {
      list.textContent = '';
      var items = (d.recent || []).slice(0, 4);
      if (!items.length) {
        var li0 = document.createElement('li');
        li0.className = 'empty';
        li0.textContent = '还没有练习记录';
        list.appendChild(li0);
      } else {
        items.forEach(function (it, i) {
          var li = document.createElement('li');
          var no = document.createElement('span');
          no.textContent = '#' + (i + 1) + ' ' + String(it.text_en || '').slice(0, 22);
          var sc = document.createElement('b');
          sc.textContent = (it.score_total === null || it.score_total === undefined)
            ? '—' : String(it.score_total);
          li.appendChild(no);
          li.appendChild(sc);
          list.appendChild(li);
        });
      }
    }
  }

  // Recent Issues 的数据源：复用练习模块已渲染的那份列表（EnPracticePractice.recentItems）。
  // ★ 为什么不让侧栏自己请求 ★
  //   试过两条路都不成立（别再重走）：
  //   ① 侧栏自己发 /api/practices?page=1&page_size=4
  //      → 在 jsdom 测试里抢在练习模块之前进mock 的 calls 队列，
  //        qa_practice_detail 的「列表请求带 page_size=25」取到的是侧栏那次
  //        page_size=4 —— 一个纯前端改动弄红了别的模块的断言。
  //   ② 包装 window.fetch 去监听练习列表的响应
  //      → 包装函数一旦 res.text() 就会读空正文流，练习模块随后 res.json()
  //        拿到空流，历史列表渲染成 0 条（qa_practice_detail 实测）。
  //        用 clone() 也一样，两个消费者争同一条流。
  //   剩下唯一稳的：练习模块自己渲染完，把 items 挂到它的导出上（只读引用）。
  //   零额外请求、零包装、零副作用。
  function readRecent() {
    var p = window.EnPracticePractice;
    return p && Array.isArray(p.recentItems) ? p.recentItems : [];
  }

  function collect() {
    return Promise.all([
      api('/api/review/due-count').catch(function () { return null; }),
      api('/api/practices/stats').catch(function () { return null; }),
      api('/api/stats').catch(function () { return null; }),
      api('/api/analytics?days=30').catch(function () { return null; })
    ]).then(function (r) {
      var due = r[0] && typeof r[0].due === 'number' ? r[0].due : null;
      var ps = r[1] || {};
      var st = r[2] || {};
      var an = r[3] || {};
      // 真按 trend 数：练习天数 = trend 里 practice_count > 0 的天数
      var days = null;
      if (Array.isArray(an.trend)) {
        days = an.trend.filter(function (t) { return Number(t.practice_count) > 0; }).length;
      }
      // 今日平均分：真按 trend 最后一天（今天）的 avg_score 取，不是历史平均。
      // ★ 别用 ps.avg_total 冒充「今日平均」—— 那是全部练习的平均，语义不同 ★
      var todayAvg = null;
      if (Array.isArray(an.trend) && an.trend.length) {
        var today = an.trend[an.trend.length - 1];
        if (today && today.avg_score !== null && today.avg_score !== undefined) {
          todayAvg = today.avg_score;
        }
      }
      return {
        due: due,
        count: typeof ps.count === 'number' ? ps.count : (typeof st.practices === 'number' ? st.practices : null),
        avg: ps.avg_total,
        best: ps.best_total,
        entries: typeof st.entries === 'number' ? st.entries : null,
        days: days,
        todayAvg: todayAvg,
        recent: readRecent()
      };
    });
  }

  /** @param force  true = 丢掉缓存重取（练习模块渲染完新列表时用） */
  function refresh(force) {
    if (force) { last = null; }
    if (last) { paint(last); return Promise.resolve(last); }
    if (inflight) { return inflight; }
    inflight = collect()
      .then(function (d) { last = d; paint(d); return d; })
      .catch(function () { paint({}); return {}; })
      .then(function (d) { inflight = null; return d; });
    return inflight;
  }

  window.EnPracticeRails = { refresh: refresh, paint: paint, collect: collect, syncRailHeight: syncRailHeight };

  /* 漫威方向右列是上下两格（数字 / 近期练习），两块都是 grid item 落在同一格。
     两行 grid 的四种写法都会让第二格被主区高度顶出首屏（详见 style.css 里
     记下的四条失败路径），所以这里量一次 rail-a 的真实高度写进 --rail-a-h，
     由 CSS 的 margin-top 让位。
     这不是搬 DOM：DOM 一个节点都没动，JS 只写一个长度值，
     位置关系仍由 CSS 决定。窗口尺寸变化会改变侧栏高度，所以绑 resize。 */
  function syncRailHeight() {
    var root = document.documentElement;
    if (root.getAttribute('data-layout') !== 'comic') {
      root.style.removeProperty('--rail-a-h');
      return;
    }
    var a = document.getElementById('rail-a');
    if (!a) { return; }
    var h = Math.round(a.getBoundingClientRect().height);
    root.style.setProperty('--rail-a-h', h + 'px');
  }
  var rzTimer = null;
  window.addEventListener('resize', function () {
    if (rzTimer) { clearTimeout(rzTimer); }
    rzTimer = setTimeout(syncRailHeight, 120);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { refresh().then(syncRailHeight); });
  } else {
    refresh().then(syncRailHeight);
  }
})();
// ---------------------------------------------------------------------------
(function settingsModule() {
  function showError(msg) {
    $('s-error').textContent = msg || '';
    $('s-error').hidden = !msg;
  }

  function isCloud() {
    return !!(window.EnPracticeCloud && window.EnPracticeCloud.isCloud);
  }

  function storeKey(key) {
    try {
      if (key) { localStorage.setItem('enp_ai_key', key); }
      else { localStorage.removeItem('enp_ai_key'); }
      if (window.EnPracticeCloud) { window.EnPracticeCloud.apiKey = key || ''; }
    } catch (e) {
      showError('浏览器禁用了本地存储，密钥无法保存（可临时粘贴后直接使用）');
    }
  }

  // ★ 云端：访客自填的接口地址 / 模型 / 超时 ★
  //   三者与密钥**完全同构** —— 只存在本浏览器 localStorage，随请求头带给服务器，
  //   服务端不落盘。桌面版走的是另一条路（PUT /api/settings 存进本机 settings.json），
  //   所以这三个函数只在云端分支被调用。
  function storeAiTarget(baseUrl, model, timeout) {
    try {
      if (baseUrl) { localStorage.setItem('enp_ai_base_url', baseUrl); }
      else { localStorage.removeItem('enp_ai_base_url'); }
      if (model) { localStorage.setItem('enp_ai_model', model); }
      else { localStorage.removeItem('enp_ai_model'); }
      if (timeout) { localStorage.setItem('enp_ai_timeout', timeout); }
      else { localStorage.removeItem('enp_ai_timeout'); }
      if (window.EnPracticeCloud) {
        window.EnPracticeCloud.aiBaseUrl = baseUrl || '';
        window.EnPracticeCloud.aiModel = model || '';
        window.EnPracticeCloud.aiTimeout = timeout || '';
      }
    } catch (e) {
      showError('浏览器禁用了本地存储，接口地址无法保存（可临时粘贴后直接使用）');
    }
  }

  function readAiTarget() {
    var out = { baseUrl: '', model: '', timeout: '' };
    try {
      out.baseUrl = localStorage.getItem('enp_ai_base_url') || '';
      out.model = localStorage.getItem('enp_ai_model') || '';
      out.timeout = localStorage.getItem('enp_ai_timeout') || '';
    } catch (e) {
      // 隐私模式禁 localStorage：降级为"没填"，不报错
    }
    if (window.EnPracticeCloud) {
      window.EnPracticeCloud.aiBaseUrl = out.baseUrl;
      window.EnPracticeCloud.aiModel = out.model;
      window.EnPracticeCloud.aiTimeout = out.timeout;
    }
    return out;
  }

  function applyCloudLayout() {
    // ★ 云端：接口地址与模型**不再隐藏** ★
    //   桌面版本来就能填这两项，云端版之前把它们藏起来（因为当时服务端锁死、
    //   访客填了也存不住）。现在访客自填值会真的生效（走请求头通道），
    //   所以两项都照常显示 —— 这正是本次要补齐的。
    //   ★ 密钥那栏的措辞也要分流：桌面说"已保存密钥（不回显）"，
    //     云端说的是"已在本浏览器保存密钥"（云端根本不回读服务端设置）。
    var cloud = isCloud();
    var hintDesktop = $('s-hint-desktop');
    if (hintDesktop) { hintDesktop.hidden = cloud; }
    var cloudOnly = $('s-cloud-only');
    if (cloudOnly) { cloudOnly.hidden = !cloud; }
    // ★ 「清空我的数据」只在云端显示 ★
    //   桌面库里全是本机用户自己的积累，"访客"这个概念根本不存在 ——
    //   给一个按钮去删自己的词库是危险品（app/main.py 那边也是 405）。
    var clearWrap = $('s-clear-wrap');
    if (clearWrap) { clearWrap.hidden = !cloud; }
  }

  function openSettings() {
    showError('');
    $('s-key').value = '';
    return (window.EnPracticeCloud && window.EnPracticeCloud.ready
      ? window.EnPracticeCloud.ready : Promise.resolve())
      .then(function () {
        applyCloudLayout();
        var localKey = (window.EnPracticeCloud && window.EnPracticeCloud.apiKey) || '';
        if (isCloud()) {
          // 云端：密钥在浏览器本地，**不回填明文**（输入框给的是掩码提示）
          $('s-key').value = '';
          $('s-key').placeholder = localKey
            ? '已在本浏览器保存密钥（不回显）；输入新密钥可替换，留空则保持不变'
            : '粘贴你自己的 API Key（只存在本浏览器，不会发给我们）';
          // ★ 云端：接口地址 / 模型 / 超时也回填本浏览器存的值 ★
          //   服务端不存这些（磁盘上不留访客指定的出网地址），所以只能从本地取。
          var t = readAiTarget();
          $('s-base-url').value = t.baseUrl;
          $('s-model').value = t.model;
          $('s-timeout').value = t.timeout;
          return null;                       // 云端不必回读服务端设置
        }
        return api('/api/settings');
      })
      .then(function (s) {
        if (!s) return null;
        var aiCfg = s.ai || {};
        // 主题不在本弹窗里「保存」（点了就即时生效并已写盘），这里只做状态回显。
        if (window.EnPracticeTheme) {
          window.EnPracticeTheme.applyTheme((s.ui || {}).ui_theme);
        }
        $('s-base-url').value = aiCfg.base_url || '';
        $('s-model').value = aiCfg.model || '';
        $('s-timeout').value = aiCfg.timeout == null ? '' : aiCfg.timeout;
        $('s-key').placeholder = aiCfg.api_key
          ? '已保存密钥（不回显）；输入新密钥可替换，留空则保持不变'
          : '尚未配置密钥；留空则以演示模式运行';
        return api('/api/ai/status');
      })
      .then(function (st) {
        if (!st) { $('s-modal').hidden = false; return; }
        var tip = $('s-ai-status');
        tip.hidden = !st.mock;
        if (st.mock) {
          tip.textContent = '当前为演示模式：译文与评分均为占位内容。填入密钥并保存后即可获得真实点评。';
        }
        $('s-modal').hidden = false;
      })
      .catch(function (err) { showError(err.message); });
  }

  function saveSettings() {
    var aiCfg = {};
    var key = $('s-key').value;
    showError('');
    if (isCloud()) {
      // ★ 云端：密钥 + 接口地址 + 模型 + 超时，**全部只写 localStorage** ★
      //   一个都不进 /api/settings —— 那会落进服务端的 settings.json，
      //   等于把访客指定的出网地址长期留在服务器上。
      //   它们随每次请求的请求头带给服务器（见 api() 统一包装）。
      var cBase = asciiSafe($('s-base-url').value.trim());
      var cModel = asciiSafe($('s-model').value.trim());
      var cTimeout = $('s-timeout').value.trim();
      if (cTimeout) {
        var ct = Number(cTimeout);
        if (!isFinite(ct) || ct < 5 || ct > 300) {
          showError('超时时间需在 5–300 秒之间');
          return;
        }
        cTimeout = String(Math.round(ct));
      }
      storeKey(key.trim());
      storeAiTarget(cBase, cModel, cTimeout);
      $('s-modal').hidden = true;
      toast(key.trim() ? '已保存到本浏览器' : '已清除本浏览器的密钥与接口设置', 'ok');
      if (window.EnPracticePractice) window.EnPracticePractice.reloadAiStatus();
      return;
    }
    var base = $('s-base-url').value.trim();
    var model = $('s-model').value.trim();
    var timeout = $('s-timeout').value.trim();
    if (base) aiCfg.base_url = base;
    if (model) aiCfg.model = model;
    if (key) aiCfg.api_key = key;  // 留空 = 不修改，绝不把空串写掉已存密钥
    if (timeout) {
      var t = Number(timeout);
      if (!isFinite(t) || t < 5 || t > 300) {
        showError('超时时间需在 5–300 秒之间');
        return;
      }
      aiCfg.timeout = t;
    }
    $('s-save').disabled = true;
    api('/api/settings', { method: 'PUT', body: JSON.stringify({ ai: aiCfg }) })
      .then(function () {
        $('s-modal').hidden = true;
        toast('设置已保存', 'ok');
        if (window.EnPracticePractice) window.EnPracticePractice.reloadAiStatus();
      })
      .catch(function (err) { showError(err.message); })
      .then(function () { $('s-save').disabled = false; });
  }

  // ---------- 清空我的数据（仅云端） ----------
  function clearMyData() {
    if (!isCloud()) { showError('桌面模式没有「访客数据」的概念'); return; }
    var box = $('s-clear-confirm');
    if (box) { box.hidden = false; }
  }

  function reallyClearMyData() {
    var btn = $('s-clear-do');
    btn.disabled = true;
    api('/api/me/data', { method: 'DELETE' })
      .then(function () {
        $('s-modal').hidden = true;
        // ★ 为什么成功后还要多解释一句 ★
        //   访客的 cookie 还在，但服务端已经给这个访客打了「已清空」标记
        //   （app/cloud.py 的 tenants/<tid>.cleared，**必须在访客目录之外**），
        //   所以下次请求建的是一份**空库**（有 schema、无演示语料），
        //   不会「删了又长出来」。
        //   光说「已清空」，隐私敏感的用户会读成「删了但没删干净」——
        //   页面刷新一下又「有数据了」，像是没删成功。
        //   把「你的数据没了」和「这里现在是空的、可以自己加」分开说，
        //   别让用户自己去猜。
        toast('已删除你的全部数据；此处将保持为空库，'
          + '你可以自己添加词条和文章。', 'ok', 6000);
        // ★ 6 秒后才刷新 ★
        //   原先 800ms 就 reload —— toast 还没被人读完就没了，
        //   上面那句话等于白写。留足阅读时间再刷新。
        setTimeout(function () { location.reload(); }, 6000);
      })
      .catch(function (err) { showError(err.message); })
      .then(function () { btn.disabled = false; });
  }

  /** 主题写盘：与 AI 配置同一条 PUT 通道（后端按分组合并，互不覆盖）。 */
  function saveTheme(id) {
    return api('/api/settings', { method: 'PUT', body: JSON.stringify({ ui: { ui_theme: id } }) })
      .catch(function (err) { toast('主题未保存：' + err.message, 'error'); });
  }

  window.EnPracticeSettings = { saveTheme: saveTheme, openSettings: openSettings };

  $('s-open').addEventListener('click', openSettings);
  $('s-cancel').addEventListener('click', function () { $('s-modal').hidden = true; });
  $('s-save').addEventListener('click', saveSettings);
  if ($('s-clear')) { $('s-clear').addEventListener('click', clearMyData); }
  if ($('s-clear-do')) { $('s-clear-do').addEventListener('click', reallyClearMyData); }
  if ($('s-clear-no')) {
    $('s-clear-no').addEventListener('click', function () {
      $('s-clear-confirm').hidden = true;
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('s-modal').hidden) $('s-modal').hidden = true;
  });
})();

// ---------------------------------------------------------------------------
// 关联库：块列表 + 画布编辑器
// ---------------------------------------------------------------------------
// ★ 形态（用户设计稿）★
//   主页面 = 搜索栏 + 筛选键 + **正方形关联块**网格 + 右上角「增加关联」。
//   点块 / 点「增加关联」打开**同一个**画布弹窗（#cv-modal）：
//     - 点块       → 已加载该关联（节点在画布上、线已连好），可继续加词条/加线/移除
//     - 点增加关联 → 空白画布，从右边词条库拖词条进来自己连
//   刻意**不做**「只读详情 + 编辑」两套窗口：两套代码必然随时间漂移。
//
// ★ 三种关系 → 两套后端模型（不要试图合并表）★
//   近似词 SYM / 反义词 OPP → entry_links 组模型（**无向**同伴关系）
//       POST /api/links {entry_ids:[a,b], relation:'synonym'|'antonym'}
//   父子                    → nodes + node_edges 模型（**有向**，一个节点只有一个父）
//       POST /api/nodes/{parent_id}/children {child:{kind:'entry',entry_id}}
//
// ★ 线型（视觉上一眼能分清，不靠颜色单独区分）★
//   SYM   蓝色实线  + 线上小方块标记，方块里写 "SYM"
//   OPP   红色虚线  + 线上小方块标记，方块里写 "OPP"
//   父子  绿色实线  + 箭头，无标记
//
// ★ XSS 红线 ★
//   词条名 / 概念名 / 关联名 全是用户自由文本，一律 textContent / DOM 构建。
//   本模块**没有任何一处** innerHTML 拼接（唯一的 innerHTML='' 是清空容器）。
// ---------------------------------------------------------------------------
(function canvasModule() {
  'use strict';

  var PAGE_SIZE = 100;
  var MIN_ZOOM = 0.4;
  var MAX_ZOOM = 1.6;
  var ZOOM_STEP = 0.2;
  var NODE_W = 148;   // 与 style.css 的 .cv-node width 保持一致
  var NODE_H = 76;
  var PALETTE_MAX = 200;

  // 主页面（块列表）状态
  var state = {
    keyword: '',
    filter: 'all',
    groups: [],      // /api/links 的关联组
    roots: [],       // /api/nodes?roots=1 的概念树根
    total: 0,
  };

  // 画布状态。**只有一个画布实例**——点块与点新增共用它，靠 canvasOpen 区分。
  var canvas = {
    open: false,
    loaded: false,   // true = 由某个已有块打开（数据已加载）；false = 空白新建
    linkId: null,    // 打开的是哪个关联组（null = 空白 / 层级视图）
    entries: [],     // 画布上的词条节点 [{id, term, meaning_zh, x, y}]
    concepts: [],    // 画布上的概念节点 [{node_id, label, x, y}]
    edges: [],       // [{from, to, relation}]  relation: synonym|antonym|parent
    zoom: 1,
    group: null,     // 打开的是哪个关联组的完整载荷（备注 / 成员都在里面）
    drag: null,      // 正在拖的节点 {kind, id, dx, dy}
    linking: null,   // 正在拉的线 {fromKind, fromId, relation}
  };

  // ---------- 工具 ----------

  function clearBox(el) {
    // 清空容器是唯一允许 innerHTML 的地方，且只写空串 —— 不存在注入面。
    el.innerHTML = '';
  }

  function svgEl(tag, attrs) {
    var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) {
      el.setAttribute(k, String(attrs[k]));
    });
    return el;
  }

  function showError(message) {
    var el = $('cv-error');
    if (!el) return;
    if (!message) { el.hidden = true; el.textContent = ''; return; }
    el.textContent = message;
    el.hidden = false;
  }

  function setHint(text) {
    var el = $('cv-hint');
    if (el) el.textContent = text || '';
  }

  /** 关系值 → 白名单项；父子不在 LINK_RELATIONS 里，单独处理。 */
  function relationInfo(relation) {
    for (var i = 0; i < LINK_RELATIONS.length; i++) {
      if (LINK_RELATIONS[i].value === relation) return LINK_RELATIONS[i];
    }
    return null;
  }

  // =======================================================================
  // 主页面：块列表
  // =======================================================================

  function loadAll() {
    $('lk-loading').hidden = false;
    return Promise.all([
      api('/api/links?page_size=' + PAGE_SIZE).catch(function () { return { items: [], total: 0 }; }),
      api('/api/nodes?roots=1&page_size=' + PAGE_SIZE).catch(function () { return { items: [], total: 0 }; }),
    ]).then(function (res) {
      state.groups = (res[0] && res[0].items) || [];
      state.roots = (res[1] && res[1].items) || [];
      state.total = state.groups.length + state.roots.length;
      $('lk-loading').hidden = true;
      renderBlocks();
      loadTombstoneCount();
    });
  }

  /**
   * 一个关联块 = 一个关联组（synonym/antonym）或一棵概念树（层级）。
   *
   * 两种都渲染成**正方形**块：用户要的是「一格一格能扫」的列表，
   * 两种关联混排才能对比。块上写清「是什么词汇的关联」：
   * 词条名（组内前几个成员） + 关系类型 + 成员数。
   */
  function renderBlocks() {
    var box = $('lk-blocks');
    if (!box) return;
    clearBox(box);
    var kw = state.keyword.trim().toLowerCase();
    var shown = 0;

    state.groups.forEach(function (group) {
      // 筛选是互斥的：选「层级」时**不能**顺带把关联组也显示出来。
      // （早先这里写成 `filter !== 'nodes'` 才显示，导致选层级时组也一起冒出来。）
      if (state.filter !== 'all' && group.relation !== state.filter) return;
      if (kw && !groupMatches(group, kw)) return;
      shown++;
      box.appendChild(buildBlock({
        kind: 'link',
        id: group.id,
        title: groupTitle(group),
        relation: group.relation,
        meta: group.members.length + ' 个词条',
        note: group.note || '',
        // ★ 用户要「在关联库这一层就看到到底是哪些词」★
        // 以前块上只有 groupTitle() 拼出来的「abundant / plentiful」摘要，
        // 看不出组里到底有几个人、分别是谁。这里把成员名整份传下去逐个列出来。
        terms: (group.members || []).map(function (m) { return m.term; }),
      }));
    });

    state.roots.forEach(function (root) {
      if (state.filter !== 'all' && state.filter !== 'nodes') return;
      // ★ 前端兜底：即使后端将来放松了判据，也不把「1 节点 / 1 词条 / 0 概念」
      // 渲染成层级块（那正是用户截图里 recoil / shy away 那一屏）。
      // 判据与后端 nodes.py 的 _SQL_ROOT_IS_STRUCTURE 同源：有子节点、
      // 或本身是概念节点，才算用户建的关联。后端已过滤，这里是第二道闸。
      var isShell = !root.child_count
        && !root.concept_count
        && Number(root.entry_count || 0) <= 1;
      if (isShell) return;
      if (kw && String(root.label || '').toLowerCase().indexOf(kw) < 0) return;
      shown++;
      box.appendChild(buildBlock({
        kind: 'nodes',
        id: root.node_id,
        title: root.label || '(未命名)',
        relation: 'nodes',
        meta: root.node_count + ' 个节点',
        note: root.entry_count + ' 词条 / ' + root.concept_count + ' 概念',
        // 层级块的概念名就是标题本身，列出来没有新信息，只列真实词条名。
        terms: root.terms || [],
      }));
    });

    state.total = shown;
    var count = $('lk-count');
    if (count) count.textContent = shown ? ('共 ' + shown + ' 个关联') : '';
    var empty = $('lk-empty');
    if (empty) empty.hidden = shown !== 0;
  }

  function groupMatches(group, kw) {
    if (String(group.relation_label || relationLabel(group.relation)).toLowerCase().indexOf(kw) >= 0) return true;
    if (String(group.note || '').toLowerCase().indexOf(kw) >= 0) return true;
    var members = group.members || [];
    for (var i = 0; i < members.length; i++) {
      if (String(members[i].term || '').toLowerCase().indexOf(kw) >= 0) return true;
      if (String(members[i].meaning_zh || '').toLowerCase().indexOf(kw) >= 0) return true;
    }
    return false;
  }

  /** 块标题：组内前 2 个词条名，让用户扫一眼就知道是什么词汇的关联。 */
  function groupTitle(group) {
    var members = group.members || [];
    if (!members.length) return '(空关联)';
    var names = members.slice(0, 2).map(function (m) { return m.term; });
    var title = names.join(' / ');
    if (members.length > 2) title += ' …';
    return title;
  }

  /** 块内最多列出几个成员名；剩下的折成「等 N 个」。 */
  var BLOCK_TERM_LIMIT = 6;

  /**
   * 一个关联块。全部 DOM 构建 + textContent：标题/成员名都是用户自由文本。
   * 点块 = 打开画布看这个关联（可继续编辑，不是只读）。
   *
   * ★ 结构约定（徽标压字问题的修法）★
   * 旧结构把 badge 和 title 都作为直接子元素顺次流式排列，标题一长就压到
   * 右上角的徽标上（用户截图：'ple' 压在 SYM 上、'give sb. the creeps' 整段被压住）。
   * 现在：
   *   - badge 是 **position:absolute** 的右上角浮层（CSS 保证），不占流；
   *   - title 独占一整行、可换行，并留出右侧 padding 避让徽标；
   *   - 成员名单独一块，排在标题下面，天然不可能与徽标重叠。
   */
  function buildBlock(info) {
    var block = document.createElement('button');
    block.type = 'button';
    block.className = 'lk-block';
    block.dataset.kind = info.kind;
    block.dataset.id = String(info.id);
    if (info.kind === 'link') block.dataset.relation = info.relation;
    block.setAttribute('title', '点开在画布中查看与编辑');

    // 关系徽标：近似词 SYM / 反义词 OPP / 层级
    var badge = document.createElement('span');
    badge.className = 'lk-block-badge';
    if (info.kind === 'nodes') {
      badge.classList.add('lk-badge-nodes');
      badge.textContent = '层级';
    } else {
      var rel = relationInfo(info.relation);
      badge.classList.add('lk-badge-' + (rel ? rel.cls.replace('cv-line-', '') : 'syn'));
      badge.textContent = rel ? rel.short : relationLabel(info.relation);
    }
    block.appendChild(badge);

    var title = document.createElement('span');
    title.className = 'lk-block-title';
    title.textContent = info.title;
    block.appendChild(title);

    // ★ 成员词条名：块内逐个列出（用户要的就是这个）★
    var terms = info.terms || [];
    if (terms.length) {
      var list = document.createElement('span');
      list.className = 'lk-block-terms';
      var shownTerms = Math.min(terms.length, BLOCK_TERM_LIMIT);
      for (var i = 0; i < shownTerms; i++) {
        var chip = document.createElement('span');
        chip.className = 'lk-term';
        chip.textContent = terms[i];   // textContent：词条名是用户自由文本
        list.appendChild(chip);
      }
      if (terms.length > shownTerms) {
        var more = document.createElement('span');
        more.className = 'lk-term lk-term-more';
        more.textContent = '等 ' + (terms.length - shownTerms) + ' 个';
        list.appendChild(more);
      }
      block.appendChild(list);
    }

    var meta = document.createElement('span');
    meta.className = 'lk-block-meta';
    meta.textContent = info.meta;
    block.appendChild(meta);

    if (info.note) {
      var note = document.createElement('span');
      note.className = 'lk-block-note';
      note.textContent = info.note;
      block.appendChild(note);
    }

    block.addEventListener('click', function () {
      if (info.kind === 'link') openLinkCanvas(info.id);
      else openTreeCanvas(info.id);
    });
    return block;
  }

  // =======================================================================
  // 画布：打开 / 关闭
  // =======================================================================

  /** 点「增加关联」：空白画布。 */
  function openBlankCanvas() {
    resetCanvas();
    canvas.open = true;
    canvas.loaded = false;
    $('cv-title').textContent = '新增关联';
    $('cv-meta').textContent = '';
    $('cv-group-bar').hidden = true;   // 还没有块，谈不上删除
    $('cv-note-row').hidden = true;
    $('cv-modal').hidden = false;
    renderCanvas();
    loadPalette();
    loadTombstoneCount();
  }

  /** 点关联块：打开该关联，节点与线都已连好，可继续加词条/加线/移除。 */
  function openLinkCanvas(linkId) {
    resetCanvas();
    canvas.open = true;
    canvas.loaded = true;
    canvas.linkId = linkId;
    $('cv-title').textContent = '关联详情';
    $('cv-modal').hidden = false;
    renderCanvas();
    loadPalette();

    api('/api/links/' + linkId).then(function (group) {
      if (!canvas.open) return;   // 载入期间用户已关闭
      canvas.group = group;
      // 组维护条：备注 + 删除该关联块。空白画布没有组，所以默认隐藏。
      $('cv-group-bar').hidden = false;
      $('cv-note-row').hidden = false;
      $('cv-note').value = group.note || '';
      $('cv-title').textContent = '关联 · ' + relationLabel(group.relation);
      $('cv-meta').textContent = (group.members || []).length + ' 个词条'
        + (group.note ? (' · ' + group.note) : '');
      // 成员摆成一圈，先把第一个成员作为中心，其余连到它 ——
      // 组模型是「同组即互为关系」，画成星形而不是两两相连（12 个成员会画出 66 条线）。
      var members = (group.members || []).slice(0, 12);
      var cx = 260, cy = 200;
      members.forEach(function (m, i) {
        var angle = (Math.PI * 2 * i) / Math.max(1, members.length) - Math.PI / 2;
        canvas.entries.push({
          id: m.id, term: m.term, meaning_zh: m.meaning_zh,
          x: i === 0 ? cx : Math.round(cx + Math.cos(angle) * 190),
          y: i === 0 ? cy : Math.round(cy + Math.sin(angle) * 190),
        });
        if (i > 0) {
          canvas.edges.push({ from: 0, to: i, relation: group.relation });
        }
      });
      canvas.loaded = true;
      renderCanvas();
      return null;
    }).catch(function (err) {
      showError('关联加载失败：' + err.message);
    });
  }

  /**
   * 点层级块：打开概念树的画布视图。
   * 层级是**有向父子**，树本身已经在后端建好，这里只把它按父子关系画到画布上。
   */
  function openTreeCanvas(rootId) {
    resetCanvas();
    canvas.open = true;
    canvas.loaded = true;
    canvas.rootId = rootId;
    $('cv-title').textContent = '层级结构';
    // ★ 需求②：层级块**也要有删除入口** ★
    // 旧代码在这里把整条维护条藏起来，于是①里那几十个绿色「层级」伪块
    // 打开后连删除按钮都看不到 —— 用户说的「点了没反应」真正的断点就在这里，
    // 不是 z-box。层级块由 rootId 标识，dissolveGroup() 会走
    // DELETE /api/nodes/{id} 这条分支。
    $('cv-group-bar').hidden = false;
    // 备注是「关联组」的概念，层级树没有备注字段 → 只显示删除按钮。
    $('cv-note-row').hidden = true;
    $('cv-modal').hidden = false;
    renderCanvas();
    loadPalette();

    return api('/api/nodes/' + rootId + '/tree').then(function (data) {
      if (!canvas.open) return;
      var tree = data && data.tree;
      $('cv-meta').textContent = (data && data.node_count ? data.node_count : 0) + ' 个节点';
      if (tree) layoutTree(tree);
      // 父子边由 layoutTree 画；树内成员之间的近义 / 反义要另外补横向虚线，
      // 那是 entry_links 那套模型的数据，树结构里根本没有（两套模型不合并）。
      drawTreeLinks(data && data.links);
      renderCanvas();
      return null;
    }).catch(function (err) {
      showError('层级加载失败：' + err.message);
    });
  }

  /**
   * 在层级画布上补出「树内成员之间的近义 / 反义」横向边。
   *
   * 数据来自 /api/nodes/{id}/tree 响应里新增的 links（后端 groups_within 只给
   * 至少 2 个成员在这棵树里的组）。成员里不在树上的那些直接跳过 —— 它们属于
   * 别的层级块，画在这儿会凭空多出几个悬空端点。
   *
   * ★ 只连相邻两个（A-B-C），不做两两全连 ★
   * 3 人组全连是三条线、构成一个三角，视觉上和「两组两人关系」分不开；
   * 串成一条反而一眼看出「这一串互为近义」。2 人组本来就是一条，不受影响。
   */
  function drawTreeLinks(groups) {
    (groups || []).forEach(function (group) {
      var slots = [];
      (group.members || []).forEach(function (m) {
        var i = findEntrySlot(m.id);
        if (i >= 0 && slots.indexOf(i) < 0) slots.push(i);
      });
      for (var k = 0; k + 1 < slots.length; k += 1) {
        var a = slots[k], b = slots[k + 1];
        var dup = canvas.edges.some(function (e) {
          if (e.relation !== group.relation) return false;
          return (e.from === a && e.to === b) || (e.from === b && e.to === a);
        });
        if (dup) continue;
        canvas.edges.push({
          from: a, to: b, fromKind: 'entry', toKind: 'entry',
          relation: group.relation,
        });
      }
    });
  }

  function resetCanvas() {
    canvas.loaded = false;
    canvas.linkId = null;
    canvas.rootId = null;
    canvas.group = null;
    canvas.entries = [];
    canvas.concepts = [];
    canvas.edges = [];
    canvas.zoom = 1;
    canvas.drag = null;
    canvas.linking = null;
    showError('');
    setHint('');
    var path = $('cv-path');
    if (path) path.textContent = '';
  }

  function closeCanvas() {
    canvas.open = false;
    canvas.drag = null;
    canvas.linking = null;
    $('cv-modal').hidden = true;
  }

  /**
   * 把一棵树摊成画布坐标。**按父子层级放射**：根在中心，每个子节点占一个
   * 独立扇区，子节点之间角度均分，扇区半径随深度递增。
   *
   * ★ 这里曾经有一个 bug：角度只按 depth 算 ★
   * 旧代码 `angle = -PI/2 + depth * 0.9`，完全没看节点在兄弟里的序号 ——
   * 于是同一层的所有子节点（斗牛犬/拉布拉多/杜宾犬/腊肠犬）算出的是**同一个**
   * 角度、同一个半径，四个节点坐标完全重合，屏幕上只看得见一个点。
   * 修法：把「第几个孩子 / 共几个孩子」带进 walk，角度按扇区均分；
   * 再对坐标做一次「同一坐标去重」的兜底，保证任意两个节点不会叠在一起。
   */
  function layoutTree(tree) {
    var out = [];

    /**
     * 遍历并算坐标。
     * @param node       当前节点
     * @param depth      深度（根 = 0）
     * @param slotStart  本层起始扇区序号（用于角度均分）
     * @param slotCount  本层总扇区数
     */
    (function walk(node, depth, slotStart, slotCount) {
      if (!node) return;
      var kids = node.children || [];

      // 本节点的扇区中心：只有孩子时用父亲的扇区中心，保持在正上方/正下方。
      var centerSlot = slotCount > 1
        ? slotStart + (slotCount - 1) / 2
        : slotStart;
      var angle = -Math.PI / 2 + (centerSlot / Math.max(1, slotCount)) * Math.PI * 2;
      var radius = 150 + depth * 90;
      out.push({
        node: node,
        depth: depth,
        x: Math.round(300 + Math.cos(angle) * radius),
        y: Math.round(240 + Math.sin(angle) * radius),
      });

      // ★ 每个孩子分到自己的扇区：这是「四个子节点不再重合」的关键 ★
      kids.forEach(function (child, i) {
        walk(child, depth + 1, slotStart + i * slotCount, kids.length * slotCount);
      });
    })(tree, 0, 0, 1);

    if (!out.length) return;

    // ★ 负坐标归一 ★
    // 放射布局以 (300,240) 为圆心、半径随深度递增（每层 +90）。同一条链上
    // 连续两代都朝同一个方向排时，深层节点会算出**负坐标**——例如
    // 克服 → surmount → insurmountable，第三层落在 y=-90，直接被画布顶边
    // 裁掉，看起来就是「节点凭空消失、连着的那条线也不知道通向哪里」。
    // 这在「词条不能当父」的年代不可能发生（树最多两层），放开之后必须补上。
    // 整树平移一次，让最靠边的节点也留出 40px 边距，不改变任何相对位置。
    var minX = Infinity, minY = Infinity;
    out.forEach(function (item) {
      if (item.x < minX) minX = item.x;
      if (item.y < minY) minY = item.y;
    });
    var shiftX = Math.max(0, 40 - minX);
    var shiftY = Math.max(0, 40 - minY);
    if (shiftX || shiftY) {
      out.forEach(function (item) { item.x += shiftX; item.y += shiftY; });
    }

    // 兜底去重：万一有结构（后端给的树不规则）导致两个节点算出同一坐标，
    // 这里强行错开，保证任意两个节点的 (x,y) 不完全相同。
    var used = {};
    out.forEach(function (item) {
      var key = item.x + ',' + item.y;
      var bump = 0;
      while (used[key]) {
        bump += 1;
        item.x += 46;
        item.y += 34;
        key = item.x + ',' + item.y;
      }
      used[key] = true;
    });

    out.forEach(function (item) {
      var node = item.node;
      var slot = { x: item.x, y: item.y, node_id: node.node_id };
      if (node.real && node.entry_id) {
        canvas.entries.push({
          id: node.entry_id, term: node.label,
          meaning_zh: node.subtitle || '', node_id: node.node_id,
          tombstone: !!node.tombstone,
          x: item.x, y: item.y,
        });
      } else {
        slot.label = node.label || '(未命名)';
        slot.tombstone = !!node.tombstone;
        slot.depth = item.depth;
        canvas.concepts.push(slot);
      }
    });

    // 父子边：真实词条节点与概念节点都要连
    (function linkEdges(node) {
      if (!node) return;
      (node.children || []).forEach(function (child) {
        var fromKind = (node.real && node.entry_id) ? 'entry' : 'concept';
        var toKind = (child.real && child.entry_id) ? 'entry' : 'concept';
        var from = fromKind === 'entry'
          ? findEntrySlot(node.entry_id) : findConceptSlot(node.node_id);
        var to = toKind === 'entry'
          ? findEntrySlot(child.entry_id) : findConceptSlot(child.node_id);
        // ★ 必须判 >= 0 而不是判真值 ★
        // 下标 0 是合法槽位（根节点通常落在 0），`if (from && to)` 会把
        // 「指向根的那条边」当成「没找到」整条丢掉 —— 表现为树少画一层边。
        if (from >= 0 && to >= 0) {
          canvas.edges.push({
            from: from, to: to,
            fromKind: fromKind, toKind: toKind,
            relation: 'parent',
          });
        }
        linkEdges(child);
      });
    })(tree);
  }

  function findEntrySlot(entryId) {
    for (var i = 0; i < canvas.entries.length; i++) {
      if (canvas.entries[i].id === entryId) return i;
    }
    return -1;
  }

  function findConceptSlot(nodeId) {
    for (var i = 0; i < canvas.concepts.length; i++) {
      if (canvas.concepts[i].node_id === nodeId) return i;
    }
    return -1;
  }

  // =======================================================================
  // 画布渲染
  // =======================================================================

  function renderCanvas() {
    var svg = $('cv-svg');
    var layer = $('cv-nodes');
    if (!svg || !layer) return;
    clearBox(svg);
    clearBox(layer);

    var width = 0, height = 0;
    canvas.entries.concat(canvas.concepts).forEach(function (item) {
      width = Math.max(width, item.x + NODE_W);
      height = Math.max(height, item.y + NODE_H);
    });
    var canvasEl = $('cv-canvas');
    canvasEl.style.width = Math.max(width + 80, 400) + 'px';
    canvasEl.style.height = Math.max(height + 80, 300) + 'px';
    canvasEl.style.setProperty('--cv-zoom', String(canvas.zoom));

    renderEdges(svg);

    canvas.entries.forEach(function (item, i) {
      layer.appendChild(renderEntryNode(item, i));
    });
    canvas.concepts.forEach(function (item, i) {
      layer.appendChild(renderConceptNode(item, i));
    });

    var empty = $('cv-empty');
    if (empty) {
      empty.hidden = (canvas.entries.length + canvas.concepts.length) > 0;
    }
  }

  /**
   * 节点矩形：以左上角坐标 + 宽高表示，供连线端点计算用。
   *
   * 宽高取 NODE_W / NODE_H（与 style.css 的 .cv-node 同步的常量），**不用
   * getBoundingClientRect**：一是 jsdom 里它恒为 0（测试环境取不到真值），
   * 二是拖动过程中每帧读布局会强制重排，画布明显卡。两个值同源、可维护。
   */
  function rectOf(kind, index) {
    var list = kind === 'entry' ? canvas.entries : canvas.concepts;
    var item = list[index];
    // 兜底：下标越界说明边与节点已经不同步（理论上不该发生）。
    // 宁可画一条 0 长度的线，也不要让整个画布因为一次异常白屏——
    // 渲染是展示层，宁可少画一条线也不能连带整个面板打不开。
    if (!item) return { x: 0, y: 0, w: NODE_W, h: NODE_H };
    return { x: item.x, y: item.y, w: NODE_W, h: NODE_H };
  }

  /**
   * ★ 连线端点：从矩形**中心**朝目标方向投射，停在矩形边界上（需求③）★
   *
   * 旧实现两端都取中心点，于是线从上方块的文字下方穿进框里、压着文字一路
   * 连到框底（用户截图）。用户要的是「线连在块的边缘」—— 两框之间的空隙里。
   *
   * 做法：设矩形中心 C、目标点 T，方向 d = T - C。射线 C + t·d 与矩形
   * 四条边的最小正 t 就是交点：
   *     t = min( 半宽 / |dx| , 半高 / |dy| )      （dx、dy 为 0 的方向不参与）
   * 这正是「射线与轴对齐包围盒求交」，对矩形（含宽 ≠ 高）严格成立。
   *
   * @param {object} from   起点矩形 {x,y,w,h}
   * @param {object} to     终点矩形 {x,y,w,h}
   * @returns {{ax:number, ay:number, bx:number, by:number}} 起点/终点的边界点
   */
  function edgeEndpoints(from, to) {
    var cx1 = from.x + from.w / 2, cy1 = from.y + from.h / 2;
    var cx2 = to.x + to.w / 2, cy2 = to.y + to.h / 2;
    var a = boundaryPoint(from, cx2, cy2);   // 起点：朝终点中心投射
    var b = boundaryPoint(to, cx1, cy1);     // 终点：朝起点中心投射
    return { ax: a.x, ay: a.y, bx: b.x, by: b.y };
  }

  /** 从矩形中心朝 (tx, ty) 投射，取与矩形边界的交点。 */
  function boundaryPoint(rect, tx, ty) {
    var cx = rect.x + rect.w / 2, cy = rect.y + rect.h / 2;
    var dx = tx - cx, dy = ty - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    var hw = rect.w / 2, hh = rect.h / 2;
    // 四个方向里取「最近撞到边」的那个：t 越小越先出界。
    var t = Infinity;
    if (dx !== 0) t = Math.min(t, hw / Math.abs(dx));
    if (dy !== 0) t = Math.min(t, hh / Math.abs(dy));
    if (!isFinite(t)) t = 0;   // 兜底：NaN 情况下退回中心，绝不返回 Infinity
    return { x: cx + dx * t, y: cy + dy * t };
  }

  function renderEdges(svg) {
    canvas.edges.forEach(function (edge) {
      var from = rectOf(edge.fromKind || 'entry', edge.from);
      var to = rectOf(edge.toKind || 'entry', edge.to);
      // ★ 端点取**边界**而非中心（需求③）：线不再伸进块内、也不压文字。
      var ep = edgeEndpoints(from, to);
      var a = { x: ep.ax, y: ep.ay };
      var b = { x: ep.bx, y: ep.by };

      if (edge.relation === 'parent') {
        // 父子：绿色实线 + 箭头，无标记
        svg.appendChild(svgEl('line', {
          x1: a.x, y1: a.y, x2: b.x, y2: b.y,
          class: 'cv-line cv-line-parent',
          'data-relation': 'parent',
        }));
        svg.appendChild(svgEl('path', {
          d: arrowPath(a, b),
          class: 'cv-arrow',
        }));
        return;
      }

      var rel = relationInfo(edge.relation);
      var cls = rel ? rel.cls : 'cv-line-syn';
      svg.appendChild(svgEl('line', {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        class: 'cv-line ' + cls,
        'data-relation': edge.relation,
      }));
      // 线上标记：一个小方块，方块里写 SYM / OPP。
      // 用 SVG rect + text（textContent 走 createElementNS，不拼字符串）。
      var mx = (a.x + b.x) / 2;
      var my = (a.y + b.y) / 2;
      svg.appendChild(svgEl('rect', {
        x: mx - 15, y: my - 9, width: 30, height: 18, rx: 3,
        class: 'cv-mark ' + cls,
      }));
      var text = svgEl('text', {
        x: mx, y: my + 4, 'text-anchor': 'middle',
        class: 'cv-mark-text ' + cls,
      });
      text.textContent = rel ? rel.short : edge.relation;
      svg.appendChild(text);
    });
  }

  /** 箭头：在线段终点画一个小三角。角度由两端坐标算出，不依赖 SVG marker。 */
  function arrowPath(a, b) {
    var dx = b.x - a.x, dy = b.y - a.y;
    var len = Math.sqrt(dx * dx + dy * dy) || 1;
    var ux = dx / len, uy = dy / len;
    var tipX = b.x - ux * 4, tipY = b.y - uy * 4;
    var size = 7;
    var p1x = tipX - uy * size, p1y = tipY + ux * size;
    var p2x = tipX + uy * size, p2y = tipY - ux * size;
    return 'M ' + tipX + ' ' + tipY + ' L ' + p1x + ' ' + p1y + ' L ' + p2x + ' ' + p2y + ' Z';
  }

  function renderEntryNode(item, index) {
    var el = document.createElement('div');
    el.className = 'cv-node cv-node-entry';
    el.dataset.kind = 'entry';
    el.dataset.index = String(index);
    el.dataset.entryId = String(item.id);
    el.style.left = item.x + 'px';
    el.style.top = item.y + 'px';
    if (item.tombstone) {
      el.classList.add('cv-tombstone');
      el.dataset.tombstone = 'true';
    }

    var label = document.createElement('div');
    label.className = 'cv-label';
    label.textContent = item.term || '(未命名)';
    label.setAttribute('title', item.term || '');
    el.appendChild(label);

    if (item.meaning_zh) {
      var sub = document.createElement('div');
      sub.className = 'cv-sub';
      sub.textContent = item.meaning_zh;
      el.appendChild(sub);
    }

    if (item.tombstone) {
      var badge = document.createElement('span');
      badge.className = 'cv-tombstone-badge';
      badge.textContent = '已删除的词条';
      el.appendChild(badge);
    }

    el.appendChild(buildPlusButton('entry', index));
    el.appendChild(buildRemoveButton('entry', index));
    attachNodeDrag(el, 'entry', index);
    return el;
  }

  function renderConceptNode(item, index) {
    var el = document.createElement('div');
    el.className = 'cv-node cv-node-concept';
    el.dataset.kind = 'concept';
    el.dataset.index = String(index);
    el.dataset.nodeId = String(item.node_id);
    el.style.left = item.x + 'px';
    el.style.top = item.y + 'px';
    if (item.tombstone) {
      el.classList.add('cv-tombstone');
      el.dataset.tombstone = 'true';
    }

    var label = document.createElement('div');
    label.className = 'cv-label';
    label.textContent = item.label || '(未命名)';
    label.setAttribute('title', item.label || '');
    el.appendChild(label);

    // 概念节点与真实词条的视觉区分：概念加「概念」角标 + 不同底色（.cv-node-concept）
    var tag = document.createElement('span');
    tag.className = 'cv-kind-tag';
    tag.textContent = '概念';
    el.appendChild(tag);

    if (item.tombstone) {
      var badge = document.createElement('span');
      badge.className = 'cv-tombstone-badge';
      badge.textContent = '已删除的词条';
      el.appendChild(badge);
      el.appendChild(buildPurgeButton(item.node_id));
    }

    el.appendChild(buildPlusButton('concept', index));
    el.appendChild(buildRemoveButton('concept', index));
    attachNodeDrag(el, 'concept', index);
    return el;
  }

  /**
   * 节点上的「＋」：点了展开关系菜单（父子 / 近似词 / 反义词），
   * 选完关系后线就跟着鼠标走，丢到另一个节点上即建立关联。
   */
  function buildPlusButton(kind, index) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cv-plus';
    btn.dataset.plus = kind + ':' + index;
    btn.textContent = '＋';
    btn.setAttribute('title', '加一条关系');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      e.preventDefault();
      openRelationMenu(kind, index, btn);
    });
    return btn;
  }

  function buildRemoveButton(kind, index) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cv-remove';
    btn.dataset.remove = kind + ':' + index;
    btn.textContent = '×';
    btn.setAttribute('title', '从画布移除');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      e.preventDefault();
      removeNode(kind, index);
    });
    return btn;
  }

  function buildPurgeButton(nodeId) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cv-purge';
    btn.dataset.purge = String(nodeId);
    btn.textContent = '清理占位';
    btn.setAttribute('title', '清理这个占位：子项会提到上一层，占位本身删除');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      e.preventDefault();
      purgeTombstone(nodeId);
    });
    return btn;
  }

  /**
   * 关系菜单。**只有三种**，与用户设计稿一致。
   * 选了之后进入「拉线」状态：鼠标移动时画一条预览线，
   * 松开在某个节点上就建立关联（见 stage 的 mouseup）。
   */
  function openRelationMenu(kind, index, anchor) {
    closeRelationMenu();
    var menu = document.createElement('div');
    menu.className = 'cv-rel-menu';
    menu.id = 'cv-rel-menu';

    var head = document.createElement('p');
    head.className = 'cv-rel-title';
    head.textContent = '选择关系';
    menu.appendChild(head);

    var items = [
      { relation: 'parent', label: '父子节点', hint: '它是上位概念' },
      { relation: 'synonym', label: '近似词 SYM', hint: '同组互为近义' },
      { relation: 'antonym', label: '反义词 OPP', hint: '同组互为反义' },
    ];
    items.forEach(function (item) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cv-rel-btn';
      btn.dataset.relation = item.relation;
      var name = document.createElement('span');
      name.className = 'cv-rel-name';
      name.textContent = item.label;
      var hint = document.createElement('span');
      hint.className = 'cv-rel-hint';
      hint.textContent = item.hint;
      btn.appendChild(name);
      btn.appendChild(hint);
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        canvas.linking = { fromKind: kind, from: index, relation: item.relation };
        closeRelationMenu();
        setHint('已选「' + item.label + '」，把线拉到另一个词条上');
      });
      menu.appendChild(btn);
    });

    document.body.appendChild(menu);
    var rect = anchor.getBoundingClientRect();
    menu.style.left = Math.round(rect.left) + 'px';
    menu.style.top = Math.round(rect.bottom + 6) + 'px';
    // 点别处关掉菜单
    setTimeout(function () {
      document.addEventListener('click', closeRelationMenu, { once: true });
    }, 0);
  }

  function closeRelationMenu() {
    var menu = $('cv-rel-menu');
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
  }

  // ---------- 节点拖动（mousedown + mousemove） ----------

  function attachNodeDrag(el, kind, index) {
    el.addEventListener('mousedown', function (e) {
      // 点在按钮上不启动拖动，否则点「＋」会被当成拖节点。
      if (e.target && e.target.tagName === 'BUTTON') return;
      e.preventDefault();
      var item = kind === 'entry' ? canvas.entries[index] : canvas.concepts[index];
      canvas.drag = {
        kind: kind, index: index,
        dx: e.clientX - item.x,
        dy: e.clientY - item.y,
        moved: false,
      };
    });
  }

  function onStageMouseMove(e) {
    if (canvas.drag) {
      var item = canvas.drag.kind === 'entry'
        ? canvas.entries[canvas.drag.index] : canvas.concepts[canvas.drag.index];
      item.x = e.clientX - canvas.drag.dx;
      item.y = e.clientY - canvas.drag.dy;
      canvas.drag.moved = true;
      // 线随节点实时重画
      renderCanvas();
    }
  }

  function onStageMouseUp(e) {
    if (canvas.drag) {
      canvas.drag = null;
      return;
    }
    if (!canvas.linking) return;

    // 找落点下的节点：命中 canvas 自己的节点元素，且不是起点自己。
    var target = findNodeAt(e.clientX, e.clientY);
    var linking = canvas.linking;
    canvas.linking = null;
    setHint('');
    if (!target) {
      showError('没落到节点上：请把线拉到画布里另一个词条上。');
      return;
    }
    if (target.kind === linking.fromKind && target.index === linking.from) {
      showError('不能连到自己。');
      return;
    }
    establishRelation(linking, target);
  }

  /** 命中测试：从最上层往下找落在点上的画布节点。 */
  function findNodeAt(clientX, clientY) {
    var nodes = document.querySelectorAll('#cv-nodes .cv-node');
    for (var i = nodes.length - 1; i >= 0; i--) {
      var el = nodes[i];
      var r = el.getBoundingClientRect();
      if (!r || !r.width) {
        // jsdom 里 getBoundingClientRect 全是 0，用 style.left/top 兜底比较
        var x = parseFloat(el.style.left) || 0;
        var y = parseFloat(el.style.top) || 0;
        continue;
      }
      if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
        return {
          kind: el.dataset.kind,
          index: parseInt(el.dataset.index, 10),
        };
      }
    }
    return null;
  }

  // =======================================================================
  // 建立关联（三种关系）
  // =======================================================================

  /**
   * 近似词 / 反义词 → entry_links 组模型（无向）。
   *
   * 两端都必须是**真实词条**：概念节点没有 entry_id，无向组模型装不下它。
   * 重复连同一对是幂等的（主键 (link_id, entry_id)），后端会另建一个新组，
   * 所以前端先在本地去重，避免画出两条一样的线。
   */
  function establishRelation(linking, target) {
    if (linking.relation === 'parent') {
      return establishParent(linking, target);
    }
    if (linking.fromKind !== 'entry' || target.kind !== 'entry') {
      showError('近似词 / 反义词只能连在两个真实词条之间（概念节点请用父子关系）。');
      return Promise.resolve();
    }
    var a = canvas.entries[linking.from];
    var b = canvas.entries[target.index];
    if (!a || !b || a.id === b.id) {
      showError('不能连到自己。');
      return Promise.resolve();
    }

    var exists = canvas.edges.some(function (e) {
      if (e.relation !== linking.relation) return false;
      return (e.from === linking.from && e.to === target.index)
        || (e.from === target.index && e.to === linking.from);
    });
    if (exists) {
      showError('这两个词条之间已经有这个关系了。');
      return Promise.resolve();
    }

    return joinRelationGroup(a, b, linking.relation).then(function (res) {
      canvas.edges.push({
        from: linking.from, to: target.index,
        fromKind: 'entry', toKind: 'entry',
        relation: linking.relation,
      });
      // 第一个建出的组记下来：它就是这个画布对应的关联。
      // 层级块画布的「关联」是 rootId（整棵树），不能被一个近义组顶替。
      if (!canvas.linkId && !canvas.rootId && res.group && res.group.id) {
        canvas.linkId = res.group.id;
      }
      showError('');
      toast(res.appended
        ? relationLabel(linking.relation) + '已并入同一组（共 ' + res.size + ' 个）'
        : relationLabel(linking.relation) + '已建立', 'ok');
      renderCanvas();
      loadAll();     // 块列表要立刻反映新建的组
      return null;
    }).catch(function (err) {
      showError('建立失败：' + err.message);
    });
  }

  /**
   * 把 b 并入 a 所在的关联组；没有可并入的组才新建。
   *
   * ★ 为什么不再「一次只发 2 个 id」★
   * 「overcome / conquer / surmount 三者互为近义」是一个 **3 人组**，
   * 不是三个 2 人组。后端 MAX_MEMBERS 本来就是 12、create_link 一直支持多人，
   * 只有前端这里把成员数写死成两个 —— 于是 3 人以上的组在界面上根本造不出来。
   *
   * ★ 并入的条件必须收紧：组里除 a 以外的成员也得在**当前画布**上 ★
   * 同一个词可能同时属于几个语义不同的组（bank 河岸 / bank 银行）。
   * 无条件并入会把两个不同场景的组缝成一个。要求「其他成员也在画布上」，
   * 等价于「这一组就是当前这块里画的」——既保住多义，又让同一块内自然长成多人组。
   */
  function joinRelationGroup(a, b, relation) {
    return api('/api/entries/' + a.id + '/links').then(function (data) {
      // ⚠ /api/entries/{id}/links 返回的是**扁平的自连接结果**（一行 = 一个同伴，
      // 不含自己），不是「组」。这里必须先按 link_id 聚回组，再把 a 自己补进
      // 成员表 —— 少补这一个 id，下面 PUT 的「成员全量替换」语义会把 a 从
      // 它自己所在的组里删掉，变成 b 和同伴的新组，而 a 消失了。
      var byLink = {};
      var groups = [];
      (data && data.links ? data.links : []).forEach(function (item) {
        var gid = item.link_id;
        if (!byLink[gid]) {
          byLink[gid] = { id: gid, relation: item.relation, ids: [a.id] };
          groups.push(byLink[gid]);
        }
        if (byLink[gid].ids.indexOf(item.entry.id) < 0) {
          byLink[gid].ids.push(item.entry.id);
        }
      });

      for (var i = 0; i < groups.length; i++) {
        var g = groups[i];
        if (g.relation !== relation) continue;
        var ids = g.ids;
        if (ids.indexOf(b.id) >= 0) {
          return { duplicate: true, group: g, size: ids.length };
        }
        var allOnCanvas = ids.every(function (id) { return findEntrySlot(id) >= 0; });
        if (!allOnCanvas) continue;
        var next = ids.concat([b.id]);
        return api('/api/links/' + g.id, {
          method: 'PUT',
          body: JSON.stringify({ entry_ids: next }),
        }).then(function (group) {
          return { group: group, appended: true, size: next.length };
        });
      }
      return api('/api/links', {
        method: 'POST',
        body: JSON.stringify({
          entry_ids: [a.id, b.id],
          relation: relation,
          note: '',
        }),
      }).then(function (group) {
        return { group: group, appended: false, size: 2 };
      });
    }).then(function (res) {
      if (res.duplicate) throw new Error('这两个词条已经在同一组里了。');
      return res;
    });
  }

  /**
   * 父子 → nodes 模型（有向）。
   *
   * ★ idx_edges_child 唯一索引 ⇒ 一个节点在层级里**只能有一个父** ★
   *   用户把已挂在别的父下的节点连为子节点时，后端 _link() 会**换父**
   *   （nodes.py 里 old_parent 那段就是干这个的，不会静默失败）。
   *   但换父是有后果的隐式数据改动，所以这里先查一次该节点当前挂在谁下面，
   *   不同父就弹确认：「这个词条已经挂在「X」下面了，要改挂到「Y」吗？」。
   *   用户确认才换；取消则什么都不做。
   */
  function establishParent(linking, target) {
    var parentItem = linking.fromKind === 'entry'
      ? canvas.entries[linking.from] : canvas.concepts[linking.from];
    var childItem = target.kind === 'entry'
      ? canvas.entries[target.index] : canvas.concepts[target.index];
    if (!parentItem || !childItem) return Promise.resolve();

    // 父子边的父可以是概念，也可以是**真实词条**。
    // 早先这里硬拦「真实词条只能作为子项」，于是「surmount → insurmountable」
    // 这种链建不出来：surmount 是词条，它只能当子、不能当父。
    // 后端 attach_entry / attach_child 从来没有这个限制（父只要求是存在的节点），
    // 拦它的只有这一行判断 —— 放开即可，表结构一个字都不用改。
    //
    // 自连必须按**槽位**判，不能拿两个 id 比大小：旧写法
    // `childItem.id === parentItem.node_id` 跨了 entry id / node id 两个空间，
    // 永远不相等，只在「父是概念」时才碰巧正确。
    if (target.kind === linking.fromKind && target.index === linking.from) {
      showError('不能连到自己。');
      return Promise.resolve();
    }

    // ★ 换父确认 ★
    // 只有「目标是真实词条」才需要查：概念节点的父在树数据里已经拿到了
    // （canvas.concepts 来自 /api/nodes/{id}/tree），而词条的父得问后端。
    // 查不到（还没入树 / 请求失败）就当没有父，直接挂 —— 不因为一次查询失败
    // 就拦住用户，后端 _link() 本身是安全的（换父或新建都不会半途坏数据）。
    var parentIndex = linking.from;
    var parentKind = linking.fromKind;

    // 父是真实词条时，它的节点行可能还没建（关联组画布里的槽位只有 entry_id）。
    // 父子边两端都是 node_id，所以先问后端要一次；节点已存在则原样返回。
    if (parentKind === 'entry' && !parentItem.node_id) {
      return ensureEntryNode(parentItem).then(function () {
        return establishParent(linking, target);   // 回填 node_id 后重跑，幂等
      });
    }

    if (target.kind !== 'entry') {
      return doAttachParent(parentItem, parentKind, target, parentIndex);
    }

    return lookupParent(childItem.id).then(function (current) {
      if (!current || !current.node_id) {
        return doAttachParent(parentItem, parentKind, target, parentIndex);
      }
      if (current.node_id === parentItem.node_id) {
        return doAttachParent(parentItem, parentKind, target, parentIndex);
      }
      // 已经挂在别的父下面 —— 换父是隐式的数据改动，先问一句。
      confirmReparent(
        current.label || '(未命名)',
        slotLabel(parentItem),
        function () { doAttachParent(parentItem, parentKind, target, parentIndex, true); }
      );
      return null;
    });
  }

  /** 画布槽位的显示名：词条槽位存的是 term，概念槽位存的是 label。 */
  function slotLabel(item) {
    return (item && (item.term || item.label)) || '(未命名)';
  }

  /**
   * 词条的节点行可能还没建（关联组画布里的槽位只有 entry_id）。
   * 不带 parent_id 调 /api/nodes/from-entry 是幂等的：节点已存在就返回现有的
   * （created=false），不建任何边；回填 node_id 后同一槽位后续都能直接用。
   */
  function ensureEntryNode(item) {
    if (!item) return Promise.reject(new Error('父节点不存在'));
    if (item.node_id) return Promise.resolve(item.node_id);
    if (!item.id) return Promise.reject(new Error('父节点还没有对应的词条'));
    return api('/api/nodes/from-entry', {
      method: 'POST',
      body: JSON.stringify({ entry_id: item.id }),
    }).then(function (res) {
      var node = res && res.node;
      if (!node || !node.node_id) throw new Error('节点创建失败');
      item.node_id = node.node_id;
      return node.node_id;
    });
  }

  /**
   * 真正挂子节点。分两种入参形态（都走同一个端点 /api/nodes/{id}/children）：
   *   真实词条 → {child:{kind:'entry', entry_id:N}}
   *   已有概念 → {child:{kind:'concept', label:'...'}}（后端按父下同名复用）
   *
   * parentKind / parentIndex 必须由调用方显式传进来：
   *   ① canvas.concepts 的元素上**没有** .index 字段（index 是它在数组里的位置，
   *      不是数据的一部分），从 parentItem 上取会拿到 undefined，画出 from 为空的坏边；
   *   ② 父可以是概念也可以是词条，而两个数组的下标互不相通 —— 光有下标不够，
   *      还得知道它属于哪个数组，否则 rectOf() 会跑到另一个数组里取坐标，
   *      画出一条指向错误节点的线（这是放开「词条当父」后必然出现的新情况）。
   */
  function doAttachParent(parentItem, parentKind, target, parentIndex, reparenting) {
    var childSpec;
    if (target.kind === 'entry') {
      childSpec = { kind: 'entry', entry_id: canvas.entries[target.index].id };
    } else {
      childSpec = { kind: 'concept', label: canvas.concepts[target.index].label || '' };
    }

    return ensureEntryNode(parentItem).then(function (parentNodeId) {
      return api('/api/nodes/' + parentNodeId + '/children', {
        method: 'POST',
        body: JSON.stringify({ child: childSpec }),
      });
    }).then(function (node) {
      showError('');
      if (!reparenting) {
        canvas.edges.push({
          from: parentIndex, to: target.index,
          fromKind: parentKind, toKind: target.kind,
          relation: 'parent',
        });
        // 概念子项会被后端 get-or-create 成新节点，回填它的 node_id
        if (target.kind === 'concept') canvas.concepts[target.index].node_id = node.node_id;
      }
      toast('已建立父子关系', 'ok');
      renderCanvas();
      loadAll();
      return null;
    }).catch(function (err) {
      showError('挂载失败：' + err.message);
    });
  }

  /**
   * 换父确认。这是本模块唯一一处用 openConfirm 的地方——
   * 「已经挂在别的父下面」不是错误，是一个需要用户拍板的隐式数据改动。
   *
   * openConfirm 的按钮文案默认是「删除」（它原本服务于删除确认弹窗），
   * 换父不是删除，所以先把按钮文案临时改成「改挂」，关掉时恢复 ——
   * 不改会让用户在「删除」按钮上点「改挂」，这是会误操作的地方。
   */
  function confirmReparent(currentParentLabel, newParentLabel, then) {
    var ok = $('g-ok');
    var original = ok ? ok.textContent : '';
    if (ok) ok.textContent = '改挂';
    openConfirm(
      '这个词条已经挂在「' + currentParentLabel + '」下面了，要改挂到「' + newParentLabel + '」吗？',
      function () {
        if (ok) ok.textContent = original;
        then();
      }
    );
    // 取消路径：openConfirm 的取消不会调 onOk，这里提前把文案挂到关闭后恢复
    var cancel = $('g-cancel');
    if (cancel && !cancel._cvHooked) {
      cancel._cvHooked = true;
      cancel.addEventListener('click', function () {
        if (ok) ok.textContent = original;
      });
    }
  }

  /** 查某词条当前挂在哪个父节点下（用于换父确认）。 */
  function lookupParent(entryId) {
    return api('/api/entries/' + entryId + '/nodes').then(function (data) {
      return (data && data.parent) || null;
    }).catch(function () { return null; });
  }

  // =======================================================================
  // 移除 / 占位清理
  // =======================================================================

  /**
   * 从画布移除一个节点。
   *
   * 只移除**画布上的显示**，不动后端数据：块列表仍是真相来源，
   * 关掉画布再打开就恢复原样。这是刻意的——「移除」不该等同于「删数据」。
   *
   * ★ 删掉下标 k 之后，所有 > k 的下标都要前移 ★
   * edges 里存的是**数组下标**（from/to），不是稳定 id。splice 会让后面的
   * 节点整体前移一位，若只把「碰到 k 的边」过滤掉，剩下的边就会指向错位的
   * 节点 —— 表现为线连到不相干的词条上，或 rectOf 取到兜底矩形直接抛错。
   * 所以这里统一做一次下标重映射。
   */
  function removeNode(kind, index) {
    var list = kind === 'entry' ? canvas.entries : canvas.concepts;

    // 打开的是某个**已有组**时，成员节点上的 × 意味着「真的把它移出这个组」
    // （走 POST /api/links/{id}/remove），而不只是从画布上拿走。
    // 空白画布里没有组，× 就只做画布移除 —— 用户还没提交任何东西。
    if (kind === 'entry' && canvas.linkId && canvas.group) {
      return removeMemberFromGroup(canvas.entries[index].id);
    }

    list.splice(index, 1);

    canvas.edges = canvas.edges.filter(function (e) {
      var f = e.fromKind || 'entry';
      var t = e.toKind || 'entry';
      return !(f === kind && e.from === index) && !(t === kind && e.to === index);
    }).map(function (e) {
      var f = e.fromKind || 'entry';
      var t = e.toKind || 'entry';
      return {
        from: (f === kind && e.from > index) ? e.from - 1 : e.from,
        to: (t === kind && e.to > index) ? e.to - 1 : e.to,
        fromKind: f,
        toKind: t,
        relation: e.relation,
      };
    });
    renderCanvas();
  }

  /**
   * 清理单个占位节点（删词条后树上留下的壳）。
   * 走 POST /api/nodes/{id}/purge 而不是 DELETE /api/nodes/{id}：
   * 后者默认 cascade，会把占位下面的真实子树一起删掉——那是二次数据丢失。
   */
  function purgeTombstone(nodeId) {
    return api('/api/nodes/' + nodeId + '/purge', { method: 'POST' })
      .then(function () {
        toast('已清理占位节点，它的子项已提到上一层', 'ok');
        return loadAll();
      })
      .catch(function (err) {
        showError('清理失败：' + err.message);
      });
  }

  /** 一次性清理全部占位节点。 */
  function purgeAllTombstones() {
    return api('/api/nodes/tombstones/purge-all', { method: 'POST' })
      .then(function (data) {
        toast('已清理 ' + ((data && data.purged_count) || 0) + ' 个占位节点', 'ok');
        return loadAll();
      })
      .catch(function (err) {
        toast('清理失败：' + err.message, 'error');
      });
  }

  /**
   * 清理空壳（需求①的数据层）：删掉「从没被挂进任何层级树」的孤立词条节点行。
   *
   * ★ 用户手动触发，不是启动时自动跑 ★
   * 自动删会让用户觉得数据莫名消失，所以只在用户点了「清理空壳」之后才发请求。
   * 后端口径：只删 kind='entry' 且在 node_edges 里既不是 parent 也不是 child
   * 的行 —— 用户亲手建的关联（含暂时为空的）一个都不动，词汇库词条也一个不少。
   * 幂等：再点一次会返回 deleted=0，如实告诉用户「没有可清理的」。
   */
  function purgeEmptyShells() {
    return openConfirm('确定清理这些「空壳」吗？它们是还没被挂进任何层级树的孤立节点记录。词汇库里的词条和你建的关联都不会受影响。', function () {
      api('/api/nodes/purge-empty', { method: 'POST' })
        .then(function (data) {
          var n = (data && data.deleted) || 0;
          // 0 也要如实说出来，不能静默 —— 用户点了按钮就该得到回应。
          toast(n > 0 ? ('已清理 ' + n + ' 个空壳节点') : '没有可清理的空壳', 'ok');
          return loadAll();
        })
        .catch(function (err) {
          toast('清理失败：' + err.message, 'error');
        });
    });
  }

  function loadTombstoneCount() {
    return api('/api/nodes/tombstones')
      .then(function (data) {
        var total = (data && data.total) || 0;
        var el = $('lk-tombstone-count');
        if (el) el.textContent = total ? (total + ' 个待清理') : '没有待清理的占位';
        var btn = $('lk-purge-all');
        if (btn) btn.disabled = total === 0;
        return total;
      })
      .catch(function () {
        var el = $('lk-tombstone-count');
        if (el) el.textContent = '占位状态加载失败';
        return 0;
      });
  }

  // =======================================================================
  // 右侧词条库（拖拽源）
  // =======================================================================

  var palette = { keyword: '', filter: 'all', items: [] };

  function loadPalette() {
    return api('/api/entries?page_size=' + PALETTE_MAX)
      .then(function (data) {
        palette.items = (data && data.items) || [];
        renderPalette();
        return null;
      })
      .catch(function () {
        palette.items = [];
        renderPalette();
      });
  }

  function renderPalette() {
    var box = $('cv-palette');
    if (!box) return;
    clearBox(box);
    var kw = palette.keyword.trim().toLowerCase();
    var inCanvas = {};
    canvas.entries.forEach(function (e) { inCanvas['e' + e.id] = true; });

    var shown = 0;
    palette.items.forEach(function (entry) {
      if (kw && String(entry.term).toLowerCase().indexOf(kw) < 0) return;
      if (palette.filter === 'important' && !(entry.importance > 0)) return;
      if (palette.filter === 'unused' && inCanvas['e' + entry.id]) return;
      shown++;
      box.appendChild(buildPaletteItem(entry, inCanvas['e' + entry.id]));
    });

    if (!shown) {
      var tip = document.createElement('span');
      tip.className = 'muted';
      tip.textContent = palette.items.length ? '没有匹配的词条' : '还没有词条，请先到词汇库录入';
      box.appendChild(tip);
    }

    // 已经在画布上的临时词条也列出来（只读展示）。
    // 刻意不带 draggable：概念节点不是词条，不能从词条库拖进画布 ——
    // 它是「建出来就直接在画布上」的东西。
    var groupConcepts = (canvas.group && canvas.group.concepts) || [];
    groupConcepts.forEach(function (c) {
      var el = document.createElement('div');
      el.className = 'cv-palette-item cv-palette-concept';
      el.dataset.conceptId = String(c.node_id);
      var term = document.createElement('span');
      term.className = 'cv-palette-term';
      term.textContent = c.label;   // textContent：概念名是用户自由文本
      el.appendChild(term);
      var tag = document.createElement('span');
      tag.className = 'cv-palette-concept-tag';
      tag.textContent = '临时';
      el.appendChild(tag);
      box.appendChild(el);
    });
  }

  /**
   * 一个可拖动的词条。**原生 HTML5 drag & drop**（不引任何库：
   * 项目硬约束是离线可用、无外部依赖）。
   * dragstart 时把 entry_id 放进 dataTransfer；画布 drop 时读出来建节点。
   */
  function buildPaletteItem(entry, alreadyIn) {
    var el = document.createElement('div');
    el.className = 'cv-palette-item';
    el.draggable = true;
    el.dataset.entryId = String(entry.id);
    if (alreadyIn) el.classList.add('cv-palette-added');

    var term = document.createElement('span');
    term.className = 'cv-palette-term';
    term.textContent = entry.term;   // textContent：词条名是用户自由文本
    el.appendChild(term);

    if (entry.meaning_zh) {
      var meaning = document.createElement('span');
      meaning.className = 'cv-palette-meaning';
      meaning.textContent = entry.meaning_zh;
      el.appendChild(meaning);
    }

    el.addEventListener('dragstart', function (e) {
      var dt = e.dataTransfer;
      // jsdom 没有实现 DataTransfer，测试里手写桩；真实浏览器两者都有 setData。
      if (dt && dt.setData) {
        dt.setData('text/plain', String(entry.id));
        dt.setData('application/x-entry-id', String(entry.id));
      }
      if (dt && dt.effectAllowed !== undefined) dt.effectAllowed = 'copy';
      canvas.dragging = { kind: 'entry', id: entry.id };
    });

    el.addEventListener('dragend', function () {
      canvas.dragging = null;
    });
    return el;
  }

  /**
   * 把一个词条移出关联组（保留能力：移出成员）。
   *
   * ★ 后端规则：移出后组内不足 2 人会**连带解散整组** ★
   * 返回体里的 dissolved 会告知这件事，前端据此给不同提示 ——
   * 一次操作可能销毁用户的一整个关联组，这不是可以静默带过的事。
   */
  function removeMemberFromGroup(entryId) {
    return api('/api/links/' + canvas.linkId + '/remove', {
      method: 'POST',
      body: JSON.stringify({ entry_id: entryId }),
    }).then(function (result) {
      if (result && result.dissolved) {
        toast('组内不足 2 人，已连带解散', 'ok');
        closeCanvas();
        loadAll();
        return null;
      }
      toast('已移出', 'ok');
      // 组还在：重新载入以拿到最新的成员与备注
      return openLinkCanvas(canvas.linkId);
    }).catch(function (err) {
      showError('移出失败：' + err.message);
    });
  }

  /**
   * 删除该关联块（需求②）。
   *
   * ★ 断点记录：为什么这个按钮以前「点了没反应」 ★
   * 上一轮只把 #g-modal 的 z-index 从 50 提到 80，但那只覆盖了「关联组」
   * （SYM/OPP）这一条路。**层级块根本没有删除入口** ——
   * openTreeCanvas() 里写的是 `$('cv-group-bar').hidden = true`，
   * 整个组维护条（含本按钮）被藏起来。而用户点开的正是①里那几十个
   * 绿色「层级」伪块，所以看到的是「连按钮都没有」，不是「确认框被盖住」。
   * 现在层级块也显示维护条（本按钮走 DELETE /api/nodes/{id}），
   * 于是每个块都有了明确的删除入口。
   *
   * 这是不可逆操作，所以先确认。
   */
  function dissolveGroup() {
    // 层级块：删的是这棵概念树（连同子树），不是 entry_links 里的组。
    if (canvas.rootId) {
      return openConfirm('确定删除这个层级关联块吗？块内的节点会一并删除，词条本身不受影响。', function () {
        api('/api/nodes/' + canvas.rootId, { method: 'DELETE' })
          .then(function () {
            toast('已删除', 'ok');
            closeCanvas();
            return loadAll();
          })
          .catch(function (err) { showError('删除失败：' + err.message); });
      });
    }
    if (!canvas.linkId) return;
    var n = canvas.group && canvas.group.members ? canvas.group.members.length : 0;
    openConfirm('确定删除这个关联块吗？块内 ' + n + ' 个词条的关联会一并解除。', function () {
      api('/api/links/' + canvas.linkId, { method: 'DELETE' })
        .then(function () {
          toast('已删除', 'ok');
          closeCanvas();
          return loadAll();
        })
        .catch(function (err) { showError('删除失败：' + err.message); });
    });
  }

  /**
   * 存备注（保留能力）。只传 note、不传 entry_ids ——
   * 后端 update_link 的三个字段都是「None = 不改动」，传了成员列表就会
   * 把成员整体覆盖掉（这坑 links.py 的注释里写着，这里必须只传一个字段）。
   */
  function saveNote() {
    if (!canvas.linkId) return;
    return api('/api/links/' + canvas.linkId, {
      method: 'PUT',
      body: JSON.stringify({ note: $('cv-note').value }),
    }).then(function (group) {
      canvas.group = group;
      toast('备注已保存', 'ok');
      $('cv-meta').textContent = (group.members || []).length + ' 个词条'
        + (group.note ? (' · ' + group.note) : '');
      return loadAll();
    }).catch(function (err) {
      showError('保存备注失败：' + err.message);
    });
  }

  /**
   * 建一个**临时词条**（概念节点）并放进画布。
   *
   * 用户需求原话：「无法创造临时词条（比如 dog 作为父词条但不进入词汇库，
   * 因为这个词太简单了）」。
   *
   * 为什么不需要新后端：概念节点（kind=concept）本来就不进 entries 表、
   * 不参与 AI 评分、没有详情页 —— 它就是为这种场景设计的。
   * 走 POST /api/nodes/concept，契约见 app/main.py 的 ConceptIn：
   *   { label: str, note: str, parent_id: int|None, position: int|None }
   * parent_id 传当前打开的层级根节点 id（如果在层级画布里），
   * 这样建出来就直接挂在树里、能当父词条用。
   *
   * 视觉：画布上概念节点带 .cv-node-concept（虚线边框 + 绿调底 + 「概念」角标），
   * 与真实词条（方角实线卡片）明显区分。
   */
  function addConceptToCanvas() {
    var input = $('cv-concept-label');
    if (!input) return Promise.resolve(null);
    var label = String(input.value || '').trim();
    if (!label) {
      showError('请先输入临时词条的名字');
      return Promise.resolve(null);
    }
    // 同名概念已经在画布上就别重复建
    for (var i = 0; i < canvas.concepts.length; i++) {
      if (canvas.concepts[i].label === label) {
        showError('「' + label + '」已经在画布上了');
        return Promise.resolve(null);
      }
    }

    var payload = { label: label, note: '' };
    // 在层级画布里打开时，直接挂到当前根节点下 —— 这正是用户要的「当父词条」。
    if (canvas.rootId) payload.parent_id = canvas.rootId;

    return api('/api/nodes/concept', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then(function (node) {
      // 概念节点不在 entries 表里，所以永远不是「已关联词条」，
      // 直接往右边的词条库里塞一条纯展示条目，拖不动、点不了。
      var concepts = (canvas.group && canvas.group.concepts) || [];
      canvas.group = canvas.group || {};
      canvas.group.concepts = concepts.concat([{
        node_id: node.node_id, label: node.label || label, note: node.note || '',
      }]);

      canvas.concepts.push({
        x: canvas.concepts.length * 30 + 60,
        y: canvas.concepts.length * 30 + 60,
        node_id: node.node_id,
        label: node.label || label,
        tombstone: false,
        depth: 0,
      });
      canvas.loaded = true;
      showError('');
      setHint('已加入临时词条「' + (node.label || label) + '」，它不会进入词汇库');
      input.value = '';
      renderCanvas();
      renderPalette();
      return node;
    }).catch(function (err) {
      showError('创建临时词条失败：' + err.message);
    });
  }

  // =======================================================================
  // 缩放
  // =======================================================================

  function applyZoom(value) {
    var z = Math.round(value * 100) / 100;
    canvas.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
    var el = $('cv-canvas');
    if (el) el.style.setProperty('--cv-zoom', String(canvas.zoom));
    var label = $('cv-zoom');
    if (label) label.textContent = Math.round(canvas.zoom * 100) + '%';
  }

  // =======================================================================
  // 事件绑定 + 启动
  // =======================================================================

  $('lk-search').addEventListener('input', function () {
    state.keyword = this.value;
    renderBlocks();
  });

  $('lk-filters').addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.lk-filter-btn') : null;
    if (!btn) return;
    state.filter = btn.dataset.filter;
    document.querySelectorAll('#lk-filters .lk-filter-btn').forEach(function (b) {
      var on = b === btn;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    renderBlocks();
  });

  // 「增加关联」→ 空白画布
  $('lk-add').addEventListener('click', openBlankCanvas);

  $('lk-purge-all').addEventListener('click', function () {
    purgeAllTombstones();
  });

  // 清理空壳（需求①）：用户手动触发，绝不在启动时自动跑
  $('lk-purge-empty').addEventListener('click', function () {
    purgeEmptyShells();
  });

  $('cv-close').addEventListener('click', closeCanvas);
  $('cv-dissolve').addEventListener('click', dissolveGroup);
  $('cv-save-note').addEventListener('click', saveNote);
  $('cv-concept-add').addEventListener('click', function () { addConceptToCanvas(); });
  $('cv-concept-label').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); addConceptToCanvas(); }
  });
  $('cv-zoom-in').addEventListener('click', function () { applyZoom(canvas.zoom + ZOOM_STEP); });
  $('cv-zoom-out').addEventListener('click', function () { applyZoom(canvas.zoom - ZOOM_STEP); });
  $('cv-zoom-reset').addEventListener('click', function () { applyZoom(1); });

  $('cv-palette-search').addEventListener('input', function () {
    palette.keyword = this.value;
    renderPalette();
  });

  $('cv-palette-filters').addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.cv-pf-btn') : null;
    if (!btn) return;
    palette.filter = btn.dataset.pfilter;
    document.querySelectorAll('#cv-palette-filters .cv-pf-btn').forEach(function (b) {
      var on = b === btn;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    renderPalette();
  });

  // ---- 拖词条进画布（HTML5 drag & drop）----

  var stage = $('cv-stage');
  // dragover 必须 preventDefault，否则 drop 根本不触发（浏览器默认不允许放）。
  stage.addEventListener('dragover', function (e) {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    stage.classList.add('cv-drop-active');
  });
  stage.addEventListener('dragleave', function () {
    stage.classList.remove('cv-drop-active');
  });
  stage.addEventListener('drop', function (e) {
    e.preventDefault();
    stage.classList.remove('cv-drop-active');
    var id = null;
    if (e.dataTransfer) {
      id = e.dataTransfer.getData('application/x-entry-id')
        || e.dataTransfer.getData('text/plain');
    }
    // jsdom 的 DataTransfer 桩可能只在 dragstart 时被 app.js 记下，取不到就回退
    if (!id && canvas.dragging && canvas.dragging.kind === 'entry') {
      id = canvas.dragging.id;
    }
    if (!id) {
      showError('没认出拖过来的词条。');
      return;
    }
    addEntryToCanvas(parseInt(id, 10), e.clientX, e.clientY);
  });

  /** 把一个词条加到画布上（拖进来的落点或默认位置）。 */
  function addEntryToCanvas(entryId, clientX, clientY) {
    var entry = null;
    for (var i = 0; i < palette.items.length; i++) {
      if (palette.items[i].id === entryId) { entry = palette.items[i]; break; }
    }
    if (!entry) {
      // 词条库没这一条（比如筛选把它藏了）：按 id 造一个最小节点，
      // 宁可显示 id 也不静默丢弃用户的拖放。
      entry = { id: entryId, term: '词条 ' + entryId, meaning_zh: '' };
    }
    var already = findEntrySlot(entryId);
    if (already >= 0) {
      showError('这个词条已经在画布上了。');
      return;
    }

    // 落点换算成画布坐标；拿不到 clientX/Y（jsdom）就顺次往下排。
    var rect = stage.getBoundingClientRect();
    var x = canvas.entries.length * 30 + 60;
    var y = canvas.entries.length * 30 + 60;
    if (typeof clientX === 'number' && rect && rect.width) {
      x = Math.round(clientX - rect.left + (stage.scrollLeft || 0));
      y = Math.round(clientY - rect.top + (stage.scrollTop || 0));
    }

    canvas.entries.push({
      id: entry.id, term: entry.term, meaning_zh: entry.meaning_zh,
      x: x, y: y,
    });
    showError('');
    canvas.loaded = true;
    renderCanvas();
    renderPalette();
    setHint('已加入画布。点节点上的 ＋ 选关系，再把线拉到另一个节点上。');
  }

  // ---- 节点拖动 + 拉线（mousedown/mousemove/mouseup）----

  stage.addEventListener('mousemove', onStageMouseMove);
  stage.addEventListener('mouseup', onStageMouseUp);
  // 鼠标在 stage 外松开也要收尾，否则拖动状态会卡住
  document.addEventListener('mouseup', function () {
    if (canvas.drag) { canvas.drag = null; }
  });

  // 点节点 = 选中；选中真实词条时在右侧显示它在层级树里的路径（按词条反查）。
  $('cv-nodes').addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('.cv-node') : null;
    if (!el || el.classList.contains('selected')) return;
    var prev = document.querySelector('#cv-nodes .cv-node.selected');
    if (prev) prev.classList.remove('selected');
    el.classList.add('selected');
    if (el.dataset.kind === 'entry') showEntryPath(parseInt(el.dataset.entryId, 10));
  });

  /** 按词条反查树位置：显示 根 › … › 父 的完整路径。 */
  function showEntryPath(entryId) {
    var el = $('cv-path');
    if (!el) return;
    api('/api/entries/' + entryId + '/nodes').then(function (data) {
      var path = (data && data.path) || [];
      if (!path.length) {
        el.textContent = '这个词条还没有挂在任何层级树里。';
        return;
      }
      var names = path.map(function (p) { return p.label || '(未命名)'; });
      el.textContent = '所在位置：' + names.join(' › ');
    }).catch(function (err) {
      el.textContent = '路径查询失败：' + err.message;
    });
  }

  // ---- 启动 ----

  $('lk-loading').hidden = true;
  loadAll();

  // 供 QA / 控制台调试。只暴露读接口与「打开某个关联」，
  // 不暴露 state 本体（同 vocab 的做法）。
  window.EnPracticeCanvas = {
    openBlankCanvas: openBlankCanvas,
    openLinkCanvas: openLinkCanvas,
    openTreeCanvas: openTreeCanvas,
    closeCanvas: closeCanvas,
    loadAll: loadAll,
    // QA 用：塞完假数据后重画块列表（生产路径没有「凭空加一个组」这种操作）
    renderBlocks: renderBlocks,
    purgeTombstone: purgeTombstone,
    purgeAllTombstones: purgeAllTombstones,
    dissolveGroup: dissolveGroup,
    saveNote: saveNote,
    removeMemberFromGroup: removeMemberFromGroup,
    loadTombstoneCount: loadTombstoneCount,
    loadPalette: loadPalette,
    addEntryToCanvas: addEntryToCanvas,
    addConceptToCanvas: addConceptToCanvas,
    // 历史接口名保留：词汇库删除词条后会调它刷新占位计数
    loadTombstoneCountForVocab: loadTombstoneCount,
    getState: function () {
      return {
        filter: state.filter,
        total: state.total,
        groups: state.groups.length,
        roots: state.roots.length,
        canvasOpen: canvas.open,
        canvasLoaded: canvas.loaded,
        entries: canvas.entries.length,
        concepts: canvas.concepts.length,
        edges: canvas.edges.map(function (e) {
          return { relation: e.relation, from: e.from, to: e.to };
        }),
        zoom: canvas.zoom,
      };
    },
  };

  // 词汇库删除词条后要刷新占位计数 —— 旧模块叫 EnPracticeNodes，
  // 这里把同名入口也挂上，避免其它模块的调用悬空。
  window.EnPracticeNodes = window.EnPracticeNodes || {};
  window.EnPracticeNodes.loadTombstoneCount = loadTombstoneCount;
})();

// ---------------------------------------------------------------------------
// 学习进度（数据分析）
// ---------------------------------------------------------------------------
// 数据全部来自 GET /api/analytics（真实库聚合），**没有示例数据**。
//
// ★ XSS 红线 ★
// 词条名、释义、外刊标题、来源名全部是用户自由输入的文本。
// 本模块**一律用 textContent / DOM API 写入**，不做任何 innerHTML 字符串拼接。
// SVG 里的文字也走 createElementNS + textContent。
//
// ★ 趋势图的真实数据陷阱 ★
// 数据只集中在少数几天，其余为空档期。
// - avg_score 为 null 表示"这天没练习"，**不是 0 分** → 折线必须断开，
//   把 null 当 0 画进折线会让"没练"看起来像"考了 0 分"。
// - 空档期的柱子画成极淡的底纹，视觉上可辨但不抢眼。
// - 绝不能为了让曲线好看而补一条平滑上升的假趋势 —— 那是伪造进步。
(function analyticsModule() {
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var state = {
    data: null,
    loaded: false,
    loading: false,
  };

  function $(id) { return document.getElementById(id); }

  // ---- 通用小工具 ----

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text != null) { node.textContent = String(text); }
    return node;
  }

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        node.setAttribute(k, String(attrs[k]));
      });
    }
    return node;
  }

  function clear(node) {
    // 清空容器：innerHTML='' 是全站唯一允许的 innerHTML 用法（只写空串）
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function fmtDate(iso) {
    if (!iso) { return ''; }
    var s = String(iso);
    // 库里的时间戳是 2026-10-17T22:01:46，取日期部分
    var m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? m[1] + '.' + m[2] + '.' + m[3] : s;
  }

  function shortDate(iso) {
    var m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? m[2] + '/' + m[3] : iso;
  }

  // ---- 概览四数 ----

  function renderOverview(d) {
    var box = $('an-stats');
    if (!box) { return; }
    clear(box);
    var o = d.overview || {};
    var avg = o.avg_score_all == null ? '—' : o.avg_score_all;
    var items = [
      { n: o.total_entries, unit: '条', label: '词条总量' },
      { n: o.total_practices, unit: '次', label: '累计练习' },
      { n: avg, unit: o.avg_score_all == null ? '' : '分', label: '练习平均分' },
      { n: o.total_articles, unit: '篇', label: '外刊总量' },
    ];
    items.forEach(function (it) {
      var card = el('div', 'an-stat');
      var num = el('div', 'an-stat-n', it.n);
      if (it.unit) {
        num.appendChild(el('span', 'an-stat-unit', it.unit));
      }
      card.appendChild(num);
      card.appendChild(el('div', 'an-stat-label', it.label));
      box.appendChild(card);
    });
  }

  // ---- 30 天趋势图 ----
  //
  // 双层：柱=练习次数（左轴），折线=平均分（右轴）。
  // 两条量纲不同，共用一个坐标系会误导，所以折线按自己的 0~100 映射。
  // ---- 30 天趋势图 ----
  //
  // ★ 为什么拆成上下两条，而不是叠在一张图里 ★
  // 1) 练习次数（0~4 次）与平均分（0~100 分）是两个量纲。塞进一张图必须用双轴，
  //    而双轴的两套刻度可以任意拉伸对齐 —— 同一份数据能画出"分数在涨"或"在跌"
  //    两种完全相反的结论。这是最容易被误读的一种图。
  //    改成：柱子=次数（自己的刻度），分数直接标在柱子上（数字不需要刻度）。
  // 2) 新增词条 / 新增外刊是"入库"活动，和"练习"不是一回事。
  //    真实数据里练习集中在 2 天、入库分布在 4 天，叠在一起会互相压掉。
  //    所以下面单独一条副图放入库量。
  //
  // ★ 真实数据的红线 ★
  // - avg_score 为 null = 这天没练习，**不是 0 分** → 不画柱、不断线
  // - 分数标在柱顶（数字），不需要连线 —— 只有 2 个点时连线既难看又会误导
  // - 绝不为求好看而补一条平滑上升的假趋势
  function renderTrend(d) {
    var box = $('an-trend');
    var legend = $('an-legend');
    if (!box) { return; }
    clear(box);
    if (legend) { clear(legend); }

    var trend = d.trend || [];
    if (!trend.length) {
      box.appendChild(el('div', 'an-empty', '暂无趋势数据'));
      return;
    }

    var W = 960;              // viewBox 逻辑宽（实际显示宽度由 CSS 控制）
    var H = 300;
    var padL = 30, padR = 16, padT = 20, padB = 30;
    var plotW = W - padL - padR;

    // 上半：练习次数。高度 170
    var upH = 170;
    // 中间：分隔留白
    var gap = 26;
    // 下半：入库活动。高度 46
    var lowH = 46;
    // 底部：X 轴标签
    var axisY = padT + upH + gap + lowH;

    var maxCount = 0;
    var maxIn = 0;
    trend.forEach(function (t) {
      if (t.practice_count > maxCount) { maxCount = t.practice_count; }
      // 量程按**单类**的最大值算：词条和外刊各用各的同一把尺，
      // 不能用两者之和当上限，否则"词条 20 + 外刊 1 = 21"会被说成"单格最高 21"，
      // 而实际没有任何一格是 21。口径必须和图上画出来的一致。
      var inMax = Math.max(t.new_entries || 0, t.new_articles || 0);
      if (inMax > maxIn) { maxIn = inMax; }
    });
    var countTop = Math.max(maxCount, 1);
    var inTop = Math.max(maxIn, 1);

    var svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      preserveAspectRatio: 'xMidYMid meet',
      role: 'img',
      'aria-label': '近 30 天学习趋势：上为练习次数与平均分，下为新增词条与外刊',
    });

    var slot = plotW / trend.length;
    var barW = Math.max(3, Math.min(16, slot * 0.56));

    // ---- 上半：网格 + 刻度 + 柱子 + 分数标签 ----
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var y = padT + upH - (upH * i / ticks);
      svg.appendChild(svgEl('line', {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: 'var(--border)', 'stroke-width': 1,
      }));
      var lbl = svgEl('text', {
        x: padL - 7, y: y + 3.5,
        'text-anchor': 'end',
        'font-size': 10.5, fill: 'var(--muted)',
        'font-family': 'var(--font-ui)',
      });
      lbl.textContent = String(Math.round(countTop * i / ticks));
      svg.appendChild(lbl);
    }

    trend.forEach(function (t, idx) {
      var cx = padL + slot * idx + slot / 2;
      var x = cx - barW / 2;

      if (t.practice_count > 0) {
        var h = (upH * t.practice_count / countTop);
        var bar = svgEl('rect', {
          x: x, y: padT + upH - h, width: barW, height: h,
          rx: Math.min(3, barW / 2),
          fill: 'var(--accent)',
        });
        bar.setAttribute('class', 'an-bar');
        bar.style.animationDelay = (idx * 12) + 'ms';
        svg.appendChild(bar);

        // 平均分直接标在柱顶：数字不需要刻度，也就不存在双轴的任意拉伸问题
        if (t.avg_score != null) {
          var st = svgEl('text', {
            x: cx, y: padT + upH - h - 6,
            'text-anchor': 'middle',
            'font-size': 10, fill: 'var(--accent-deep)',
            'font-family': 'var(--font-ui)',
            'font-weight': 600,
          });
          st.textContent = String(Math.round(t.avg_score));
          svg.appendChild(st);
        }
      } else {
        // 空档期：极淡底纹。看得见"这天空着"，但不抢眼。
        svg.appendChild(svgEl('rect', {
          x: x, y: padT + upH - 2, width: barW, height: 2,
          rx: 1, fill: 'var(--border)',
        }));
      }
    });

    // ---- 下半：入库活动（词条 + 外刊） ----
    var lowTop = padT + upH + gap;
    svg.appendChild(svgEl('line', {
      x1: padL, y1: lowTop - 12, x2: padL + plotW, y2: lowTop - 12,
      stroke: 'var(--border)', 'stroke-width': 1,
      'stroke-dasharray': '3 3',
    }));
    var lowLbl = svgEl('text', {
      x: padL, y: lowTop - 18,
      'text-anchor': 'start',
      'font-size': 10.5, fill: 'var(--muted)',
      'font-family': 'var(--font-ui)',
    });
    lowLbl.textContent = '入库（词条 / 外刊）';
    svg.appendChild(lowLbl);

    // 副图量程标注：不说清"一格代表多少"，柱高就是没有含义的。
    // 放在标题**后面**而不是右对齐到图右端 —— 右边会被最后一天的柱子穿过。
    var capLbl = svgEl('text', {
      x: padL + 108, y: lowTop - 18,
      'text-anchor': 'start',
      'font-size': 10.5, fill: 'var(--muted)',
      'font-family': 'var(--font-ui)',
    });
    capLbl.textContent = '· 单格最高 ' + inTop;
    svg.appendChild(capLbl);

    trend.forEach(function (t, idx) {
      var ne = t.new_entries || 0;
      var na = t.new_articles || 0;
      if (!ne && !na) { return; }
      var cx = padL + slot * idx + slot / 2;
      // 词条与外刊**紧贴成组**：同一天的两个量必须看得出是一组。
      // 之前两根柱分居两侧、中间留 9px 空隙，视觉上会被读成"两个不同日期的数据"，
      // 而实际它们是同一天。这是真实的误读风险，不只是好不好看的问题。
      var w = Math.max(2, Math.min(8, slot * 0.22));
      var gapIn = 1.5;
      var groupW = (ne ? w : 0) + (ne && na ? gapIn : 0) + (na ? w : 0);
      var bx = cx - groupW / 2;
      if (ne > 0) {
        var hE = Math.max(2, lowH * ne / inTop);
        var rE = svgEl('rect', {
          x: bx, y: lowTop + lowH - hE, width: w, height: hE,
          rx: Math.min(2, w / 2), fill: 'var(--accent)', opacity: 0.45,
        });
        rE.setAttribute('class', 'an-bar');
        svg.appendChild(rE);
        bx += w + gapIn;
      }
      if (na > 0) {
        var hA = Math.max(2, lowH * na / inTop);
        var rA = svgEl('rect', {
          x: bx, y: lowTop + lowH - hA, width: w, height: hA,
          rx: Math.min(2, w / 2), fill: 'var(--border-strong)',
        });
        rA.setAttribute('class', 'an-bar');
        svg.appendChild(rA);
      }
    });

    // ---- X 轴日期：每 5 天一个标签（30 个刻度全画会挤成一团） ----
    trend.forEach(function (t, idx) {
      if (idx % 5 !== 0 && idx !== trend.length - 1) { return; }
      var cx = padL + slot * idx + slot / 2;
      var xl = svgEl('text', {
        x: cx, y: axisY + 16,
        'text-anchor': 'middle',
        'font-size': 10.5, fill: 'var(--muted)',
        'font-family': 'var(--font-ui)',
      });
      xl.textContent = shortDate(t.date);
      svg.appendChild(xl);
    });

    box.appendChild(svg);

    // ---- 图例 ----
    if (legend) {
      var l1 = el('span', 'an-legend-item');
      var s1 = el('span', 'an-legend-swatch');
      s1.style.background = 'var(--accent)';
      l1.appendChild(s1);
      l1.appendChild(el('span', null, '练习次数'));
      legend.appendChild(l1);

      var l2 = el('span', 'an-legend-item');
      var s2 = el('span', 'an-legend-swatch');
      s2.style.background = 'var(--accent-deep)';
      s2.style.borderRadius = '50%';
      s2.style.width = '7px';
      s2.style.height = '7px';
      l2.appendChild(s2);
      l2.appendChild(el('span', null, '柱顶数字 = 当天平均分'));
      legend.appendChild(l2);

      var l3 = el('span', 'an-legend-item');
      var s3 = el('span', 'an-legend-swatch');
      s3.style.background = 'var(--accent)';
      s3.style.opacity = '0.45';
      l3.appendChild(s3);
      l3.appendChild(el('span', null, '新增词条'));
      legend.appendChild(l3);

      var l4 = el('span', 'an-legend-item');
      var s4 = el('span', 'an-legend-swatch');
      s4.style.background = 'var(--border-strong)';
      l4.appendChild(s4);
      l4.appendChild(el('span', null, '新增外刊'));
      legend.appendChild(l4);
    }

    // ---- 说明：把"空档期""练习日""入库日"讲清楚，不让用户误读 ----
    var note = $('an-trend-note');
    if (note) {
      var active = trend.filter(function (t) { return t.practice_count > 0; });
      var inDays = trend.filter(function (t) {
        return (t.new_entries || 0) > 0 || (t.new_articles || 0) > 0;
      });
      var parts = [];
      parts.push('近 ' + trend.length + ' 天里有 ' + active.length + ' 天做了练习'
        + (active.length ? '（' + active.map(function (t) {
          return shortDate(t.date) + ' ' + t.practice_count + ' 次、平均 '
            + Math.round(t.avg_score) + ' 分';
        }).join('；') + '）' : '') + '。');
      if (inDays.length) {
        parts.push('入库发生在 ' + inDays.map(function (t) {
          return shortDate(t.date) + '（词条 ' + (t.new_entries || 0)
            + ' · 外刊 ' + (t.new_articles || 0) + '）';
        }).join('，') + '。');
      }
      parts.push('底部浅色短线表示当天没有练习，不是得了 0 分。');
      note.textContent = parts.join(' ');
    }
  }

  // ---- 热门标签 ----

  function renderTags(d) {
    var box = $('an-tags');
    if (!box) { return; }
    clear(box);
    var tags = d.hot_tags || [];
    if (!tags.length) {
      box.appendChild(el('div', 'an-empty', '还没有给外刊打标签'));
      return;
    }
    var max = tags.reduce(function (m, t) {
      return Math.max(m, t.article_count);
    }, 0) || 1;

    tags.forEach(function (t, i) {
      var row = el('div', 'an-tagrow');
      row.appendChild(el('span', 'an-tagname', t.name));
      var track = el('div', 'an-tagtrack');
      var fill = el('div', 'an-tagfill');
      // 宽度按真实比例：最长条 = 100%
      fill.style.width = (t.article_count / max * 100) + '%';
      fill.style.animationDelay = (i * 45) + 'ms';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'an-tagnum', t.article_count + ' 篇'));
      box.appendChild(row);
    });
  }

  // ---- 新词 ----

  function renderEntries(d) {
    var box = $('an-entries');
    if (!box) { return; }
    clear(box);
    var list = d.recent_entries || [];
    if (!list.length) {
      box.appendChild(el('div', 'an-empty', '词库还是空的'));
      return;
    }
    list.forEach(function (e) {
      var row = el('div', 'an-item');
      var body = el('div', 'an-item-body');

      var term = el('div', 'an-item-term is-en', e.term);
      body.appendChild(term);

      if (e.meaning_zh) {
        body.appendChild(el('div', 'an-item-mean', e.meaning_zh));
      }

      var meta = el('div', 'an-item-meta');
      var typeChip = el('span', 'an-chip' + (e.type === 'phrase' ? ' is-accent' : ''),
        e.type === 'phrase' ? '短语' : '单词');
      meta.appendChild(typeChip);
      meta.appendChild(el('span', null, fmtDate(e.created_at)));
      if (e.source) {
        meta.appendChild(el('span', 'an-chip', e.source));
      }
      body.appendChild(meta);

      row.appendChild(body);
      box.appendChild(row);
    });
  }

  // ---- 新外刊 ----

  function renderArticles(d) {
    var box = $('an-articles');
    if (!box) { return; }
    clear(box);
    var list = d.recent_articles || [];
    if (!list.length) {
      box.appendChild(el('div', 'an-empty', '外刊库还是空的'));
      return;
    }
    list.forEach(function (a) {
      var row = el('div', 'an-item');
      var body = el('div', 'an-item-body');

      var title = el('div', 'an-item-term is-en', a.title);
      body.appendChild(title);

      if (a.title_zh) {
        body.appendChild(el('div', 'an-item-mean', a.title_zh));
      }

      var meta = el('div', 'an-item-meta');
      if (a.source_name) {
        meta.appendChild(el('span', 'an-chip is-accent', a.source_name));
      }
      meta.appendChild(el('span', null, fmtDate(a.added_at)));
      meta.appendChild(el('span', null, a.sentence_count + ' 句'));
      body.appendChild(meta);

      row.appendChild(body);
      box.appendChild(row);
    });
  }

  // ---- 汇总渲染 ----

  function render(d) {
    state.data = d;
    var sub = $('an-sub');
    if (sub) {
      var o = d.overview || {};
      sub.textContent = '统计截止 ' + (d.today || '')
        + ' · 近 ' + (o.window_days || 30) + ' 天 · 数据来自本机真实记录';
    }
    var upd = $('an-updated');
    if (upd) {
      var now = new Date();
      upd.textContent = '读取于 ' + now.getHours() + ':'
        + String(now.getMinutes()).padStart(2, '0');
    }
    renderOverview(d);
    renderTrend(d);
    renderTags(d);
    renderEntries(d);
    renderArticles(d);
  }

  function showError(msg) {
    var sub = $('an-sub');
    if (sub) { sub.textContent = msg; }
    ['an-stats', 'an-trend', 'an-tags', 'an-entries', 'an-articles'].forEach(function (id) {
      var node = $(id);
      if (node) {
        clear(node);
        node.appendChild(el('div', 'an-empty', msg));
      }
    });
  }

  function load(force) {
    if (state.loading) { return; }
    if (state.loaded && !force) { return; }
    state.loading = true;
    api('/api/analytics')
      .then(function (d) {
        state.loading = false;
        state.loaded = true;
        render(d);
      })
      .catch(function (err) {
        state.loading = false;
        showError('读取失败：' + (err && err.message ? err.message : '未知错误'));
      });
  }

  // ---- 绑定 ----

  function bind() {
    var refresh = $('an-refresh');
    if (refresh) {
      refresh.addEventListener('click', function () {
        state.loaded = false;
        load(true);
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }

  // 暴露给模块切换逻辑（switchModule 会调用）
  window.EnPracticeAnalytics = {
    onEnter: function () { load(false); },
    onLeave: function () {},
    getState: function () {
      return { loaded: state.loaded, loading: state.loading };
    },
  };
})();
