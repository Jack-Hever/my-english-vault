/* ★ 新手引导（onboarding tour）★
 *
 * 零第三方依赖（不引 intro.js / driver.js）：一个遮罩 + 一个 popover。
 *
 * ── 触发 ──
 *   首次打开（localStorage 无 enp_onboarded）自动弹出；看过写 enp_onboarded=1。
 *   设置弹窗里点「重新查看新手引导」可随时重看（重看时不写标记，除非点完成）。
 *
 * ── 桌面 / 网页一致 ──
 *   本文件不碰 sharecode、不读云端模式、不发任何请求，纯 DOM + localStorage。
 *   ⇒ 两个形态行为完全相同；网页版那套令牌透传逻辑也不受牵连。
 *
 * ── 六步 ──
 *   1 词汇库 v-new / 2 外刊库 a-new / 3 翻译练习 p-new
 *   4 关联库 lk-add / 5 设置按钮 s-open / 6 设置弹窗内（主题 + 密钥，不关弹窗）
 */
(function () {
  'use strict';

  var KEY = 'enp_onboarded';

  // ★ 文案逐字照抄，不改写 ★
  var STEPS = [
    {
      title: '词汇库',
      text: '在平时阅读时遇到词汇就记录在这里',
      panel: 'vocab',
      target: '#v-new'
    },
    {
      title: '外刊库',
      text: '如果遇到让你读得痛苦不堪或者兴奋不已的文章，可以记录在这里',
      panel: 'articles',
      target: '#a-new'
    },
    {
      title: '翻译练习',
      text: '用那些刻在你心底的词表达想法',
      panel: 'practice',
      target: '#p-new'
    },
    {
      title: '关联库',
      text: '词与词的联系比人与人的关系更简单',
      panel: 'links',
      target: '#lk-add'
    },
    {
      title: '设置',
      text: '在这里更换界面风格和填写密钥',
      target: '#s-open'
      // 无 panel：当前页不动，只指向右上角设置按钮
    },
    {
      // 第 6 步在设置弹窗**内部**，不关弹窗
      title: '界面主题',
      text: '换一个界面换一种心情',
      target: '#s-theme-grid',
      inModal: true
    },
    {
      title: 'AI 点评接口',
      text: 'AI 让世界更简单',
      target: '#s-key',
      inModal: true
    }
  ];

  var cur = 0;
  var mask = null, ring = null, pop = null;
  var openedModal = false;   // 记录是不是我们为了第 5 步打开的设置弹窗
  var running = false;

  // ── 小工具 ────────────────────────────────────────────────
  function $(sel) { return document.querySelector(sel); }

  function seen() {
    try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
  }
  function mark() {
    // 隐私模式/存储被禁时静默失败：引导最多再弹一次，不该因此报错
    try { localStorage.setItem(KEY, '1'); } catch (e) { /* noop */ }
  }
  function clearMark() {
    try { localStorage.removeItem(KEY); } catch (e) { /* noop */ }
  }

  // 切面板：复用应用自己的 switchModule，保证和点导航完全一致
  // （自己写一套显隐逻辑会与 applyTheme 等旁路不一致，见 app.js 注释）
  function gotoPanel(name) {
    if (!name) return;
    var btn = document.querySelector('[data-module="' + name + '"]');
    if (btn) { btn.click(); return; }
    // 兜底：直接切 .active（switchModule 不可用时的降级，不静默失败）
    var panels = document.querySelectorAll('.panel');
    for (var i = 0; i < panels.length; i++) {
      panels[i].classList.toggle('active', panels[i].id === 'panel-' + name);
    }
  }

  // 把目标滚进可视区（元素可能在折叠区/下方）
  function scrollTo(el) {
    try {
      el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
    } catch (e) {
      try { el.scrollIntoView(); } catch (e2) { /* noop */ }
    }
  }

  // ── 构建 DOM ─────────────────────────────────────────────
  function build() {
    mask = document.createElement('div');
    mask.className = 'enp-ob-mask';
    ring = document.createElement('div');
    ring.className = 'enp-ob-ring';

    pop = document.createElement('div');
    pop.className = 'enp-ob-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-modal', 'true');
    pop.innerHTML =
      '<div class="enp-ob-head">' +
        '<span class="enp-ob-step"></span>' +
        '<span class="enp-ob-title"></span>' +
        '<button type="button" class="enp-ob-skip">跳过</button>' +
      '</div>' +
      '<p class="enp-ob-text"></p>' +
      '<div class="enp-ob-foot">' +
        '<span class="enp-ob-dots"></span>' +
        '<span class="enp-ob-acts">' +
          '<button type="button" class="enp-ob-btn prev">上一步</button>' +
          '<button type="button" class="enp-ob-btn primary next">下一步</button>' +
        '</span>' +
      '</div>' +
      '<i class="enp-ob-arrow"></i>';

    document.body.appendChild(mask);
    document.body.appendChild(ring);
    document.body.appendChild(pop);

    pop.querySelector('.enp-ob-skip').addEventListener('click', function () { finish(true); });
    pop.querySelector('.enp-ob-next, .next').addEventListener('click', onNext);
    pop.querySelector('.enp-ob-prev, .prev').addEventListener('click', onPrev);

    // Esc 退出
    document.addEventListener('keydown', onKey);
    // 点遮罩不关闭（避免误触丢失进度），但点窗口外也不做处理
  }

  function onKey(e) {
    if (!running) return;
    if (e.key === 'Escape') { finish(true); }
    else if (e.key === 'ArrowRight') { onNext(e); }
    else if (e.key === 'ArrowLeft') { onPrev(e); }
  }

  // ── 定位：把 mask 挖成"洞"、ring 贴边、popover 选边 ──────
  var PAD = 8;          // 高亮环与目标之间的留白
  var placeTries = 0;   // ★ 定位重试计数（见 place() 里的说明）★
  var GAP = 12;         // popover 与目标的间距

  function place() {
    var step = STEPS[cur];
    var el = $(step.target);

    // ★ 目标是否就绪 → 由纯函数 decide() 决定（onboarding_ready.js，可单测）★
    //   原实现在这里直接 next() 跳步：第5→6 步之间设置弹窗刚打开、
    //   色板尚未布局，rect 短暂全 0 → 连续 next()冲到末步 → finish()，
    //   用户看到的就是「点下一步卡一下，引导直接没了」。
    //   现在：等待重试；重试到上限也**停在当前步**，绝不静默结束。
    var d = (window.EnpOnboardReady || {}).decide
      ? window.EnpOnboardReady.decide(el, placeTries)
      : (el ? { action: 'place', tries: 0 } : { action: 'hold', tries: placeTries });

    placeTries = d.tries;

    if (d.action !== 'place') {
      fillText();                       // 文案先就位，避免闪断
      if (d.action === 'retry') {
        setTimeout(place, (window.EnpOnboardReady || {}).RETRY_MS || 80);
      }
      // hold：保持当前步显示，等用户自己跳过或点下一步（不静默结束）
      return;
    }

    scrollTo(el);
    // 等布局稳定（scrollIntoView 后 rect 要重新取）
    var r = el.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;

    // ★ 高亮环用 box-sizing: border-box（见 onboarding.css）★
    //   这样 width/height 设定值 == 外沿尺寸，border 画在**盒子内部**，
    //   不会被 getBoundingClientRect 多算 2px。
    //   踩过的坑：不加 box-sizing 时环整体外扩 2px（实测 dt 恒差 4.1）。
    ring.style.left   = (r.left - PAD) + 'px';
    ring.style.top    = (r.top - PAD) + 'px';
    ring.style.width  = (r.width + PAD * 2) + 'px';
    ring.style.height = (r.height + PAD * 2) + 'px';
    ring.style.display = 'block';

    // 遮罩挖洞：亮区与高亮环重合
    mask.style.left = (r.left - PAD) + 'px';
    mask.style.top = (r.top - PAD) + 'px';
    mask.style.width = (r.width + PAD * 2) + 'px';
    mask.style.height = (r.height + PAD * 2) + 'px';
    mask.style.transform = 'translate(0,0)';
    mask.style.display = 'block';

    fillText();
    positionPop(r, vw, vh);
  }

  // ★ 只填文案/进度点/按钮态，不依赖目标元素 ★
  //   抽出来是为了：place() 重试期间也能先把文案显示出来，
  //   否则用户会看到「上一句话卡住不动」。
  function fillText() {
    var step = STEPS[cur];
    pop.querySelector('.enp-ob-step').textContent = (cur + 1) + '/' + STEPS.length;
    pop.querySelector('.enp-ob-title').textContent = step.title;
    pop.querySelector('.enp-ob-text').textContent = step.text;

    var dots = pop.querySelector('.enp-ob-dots');
    dots.innerHTML = '';
    for (var i = 0; i < STEPS.length; i++) {
      var d = document.createElement('span');
      d.className = 'enp-ob-dot' + (i === cur ? ' on' : '');
      dots.appendChild(d);
    }

    var prev = pop.querySelector('.enp-ob-prev, .prev');
    var next = pop.querySelector('.enp-ob-next, .next');
    prev.style.visibility = cur === 0 ? 'hidden' : 'visible';
    next.textContent = (cur === STEPS.length - 1) ? '完成' : '下一步';
  }

  // 选边：优先放在下方，放不下就上方，再不行就右侧/左侧
  function positionPop(r, vw, vh) {
    pop.style.visibility = 'hidden';
    pop.style.top = '0px';
    pop.style.left = '0px';
    var pw = pop.offsetWidth || 296;
    var ph = pop.offsetHeight || 150;
    var side, top, left;

    var below = r.bottom + GAP;
    var above = r.top - GAP - ph;
    var rightX = r.right + GAP;
    var leftX = r.left - GAP - pw;

    if (below + ph <= vh - 12) { side = 'bottom'; top = below; left = r.left; }
    else if (above >= 12) { side = 'top'; top = above; left = r.left; }
    else if (rightX + pw <= vw - 12) { side = 'right'; top = r.top; left = rightX; }
    else if (leftX >= 12) { side = 'left'; top = r.top; left = leftX; }
    else { side = 'bottom'; top = Math.min(below, vh - ph - 12); left = r.left; }

    // 水平方向夹进可视区（别顶出左右边缘）
    left = Math.max(12, Math.min(left, vw - pw - 12));
    top = Math.max(12, Math.min(top, vh - ph - 12));

    pop.setAttribute('data-side', side);
    var arrow = pop.querySelector('.enp-ob-arrow');
    // 箭头对准目标中心，且不能超出卡片边缘
    var cx = r.left + r.width / 2 - left;
    arrow.style.left = Math.max(14, Math.min(cx - 6, pw - 26)) + 'px';
    arrow.style.top = '';
    if (side === 'top') { arrow.style.left = Math.max(14, Math.min(cx - 6, pw - 26)) + 'px'; }
    else if (side === 'bottom') { arrow.style.left = Math.max(14, Math.min(cx - 6, pw - 26)) + 'px'; }
    else if (side === 'right') { arrow.style.top = Math.max(14, Math.min(r.top + r.height / 2 - top - 6, ph - 26)) + 'px'; }
    else { arrow.style.top = Math.max(14, Math.min(r.top + r.height / 2 - top - 6, ph - 26)) + 'px'; }

    pop.style.top = top + 'px';
    pop.style.left = left + 'px';
    pop.style.visibility = 'visible';
  }

  // ── 流转 ─────────────────────────────────────────────────
  function show() {
    var step = STEPS[cur];
    if (step.panel) gotoPanel(step.panel);

    // 第 5 步需要设置弹窗是开的
    if (step.inModal) {
      var modal = $('#s-modal');
      if (modal && modal.hidden) {
        var open = $('#s-open');
        if (open) open.click();
        openedModal = true;
      }
    }

    // ★ 先把文案填上，再等重排★
    // 原来只有 place() 一个入口，且要等 120ms 才动 —— 这 120ms 里用户看到的是
    // 「上一句话卡住不动」，正是用户报告的「卡住了一下」。
    // 现在文案立即更新，place() 内部自己会等目标就绪（可重试）。
    placeTries = 0;
    fillText();
    setTimeout(place, step.inModal ? 120 : 40);
  }

  function onNext(e) {
    if (e) e.preventDefault();
    if (cur >= STEPS.length - 1) { finish(false); return; }
    cur++;
    show();
  }

  function onPrev(e) {
    if (e) e.preventDefault();
    if (cur <= 0) return;
    cur--;
    show();
  }

  // 从第5/6/7步往回退时，若还开着设置弹窗要留着；退出时才关
  function closeModalIfOurs() {
    if (!openedModal) return;
    var modal = $('#s-modal');
    if (modal && !modal.hidden) {
      var cancel = $('#s-cancel');
      if (cancel) cancel.click();
    }
    openedModal = false;
  }

  function finish(skipped) {
    running = false;
    closeModalIfOurs();
    if (mask) mask.remove();
    if (ring) ring.remove();
    if (pop) pop.remove();
    mask = ring = pop = null;
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onResize);
    // 看完（无论跳过还是完成）都算看过，不再自动弹
    mark();
  }

  function onResize() { if (running) place(); }

  // ── 对外入口 ─────────────────────────────────────────────
  function start(fromStep) {
    if (running) return;
    running = true;
    cur = fromStep || 0;
    openedModal = false;
    build();
    window.addEventListener('resize', onResize);
    show();
  }

  // 自动触发：首屏渲染完再弹，避免和数据渲染抢主线程
  function autoStart() {
    if (seen()) return;
    var t = setInterval(function () {
      // 等应用把首屏数据渲染出来（#p-new 存在即可）
      if (document.querySelector('#p-new')) {
        clearInterval(t);
        setTimeout(function () { start(0); }, 350);
      }
    }, 120);
    // 兜底：8 秒还没出现也弹，别让引导永远不出现
    setTimeout(function () { clearInterval(t); if (!running && !seen()) start(0); }, 8000);
  }

  // 设置里的「重新查看」：重看期间不要因为 seen() 而跳过
  function replay() {
    var modal = $('#s-modal');
    // 从设置弹窗里重看 → 先关掉弹窗，让第 1 步的切页可见
    if (modal && !modal.hidden) {
      var cancel = $('#s-cancel');
      if (cancel) cancel.click();
    }
    clearMark();          // 顺手清标记，走完整流程
    start(0);
  }

  window.EnPracticeOnboarding = { start: start, replay: replay, STEPS: STEPS };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoStart);
  } else {
    autoStart();
  }
})();