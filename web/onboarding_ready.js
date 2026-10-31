/* ★ 纯函数：判断「目标元素是否就绪，可以开始定位」。
 *
 * 抽出来的原因：place() 里「目标缺失 / 尺寸全 0」这两条分支决定了
 * 引导是「等待重试」还是「静默跳步到结束」—— 后者就是用户报的 bug
 * （第5步点下一步卡一下，引导直接没了）。
 * 这段逻辑必须能脱离浏览器单测，否则每次验证都要起服务 + 精确压时序，
 * 而实测三轮都因为注入时序对不齐而抓不到 bug（判据不合格）。
 *
 * 契约：
 *   ready === true  → 可以定位高亮
 *   ready === false → 必须等待重试，**绝不允许跳步**
 */
(function (root) {
  'use strict';

  var MAX_TRIES = 20;      // 最多重试 20 次 × 80ms ≈ 1.6s
  var RETRY_MS = 80;

  // 元素是否已就绪（存在 + 有实际尺寸）。
  // ★ 用 rect 宽高判断，而不是 offsetWidth/offsetHeight：
  //   被 hidden 祖先藏住的元素 offsetWidth 仍可能非 0，rect 才是真实可见尺寸。
  function isReady(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (!r) return false;
    return !(r.width === 0 && r.height === 0);
  }

  /* 一步决策。
   * @param el        目标元素（可能为 null）
   * @param tries     已重试次数
   * @returns {{action: 'place'|'retry'|'hold', tries: number}}
   *   place → 可以定位
   *   retry → 还没就绪，安排一次重试
   *   hold  → 已重试够多次，**停在当前步**（不跳步、不结束）
   */
  function decide(el, tries) {
    if (isReady(el)) return { action: 'place', tries: 0 };
    if (tries < MAX_TRIES) return { action: 'retry', tries: tries + 1 };
    return { action: 'hold', tries: tries };
  }

  var api = { isReady: isReady, decide: decide, MAX_TRIES: MAX_TRIES, RETRY_MS: RETRY_MS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.EnpOnboardReady = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);