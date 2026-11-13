// ThinkSmart kiosk overlay (round 109, 2026-11-09). Injected by thinksmart-kiosk.py into every
// top-level page as a WebKit UserScript (this file is ours; XiaoZhi's files stay untouched).
//  * 127.0.0.1:8090 (XiaoZhi):
//    - Lays our home page (thinksmart-home, 127.0.0.1:8095) over XiaoZhi as a persistent frame
//      (#tsHomeLayer, z-index 9000) so returning home is instant with zero XiaoZhi flash.
//    - Opens sub-pages (网易云音乐 :8091, 服务器监控 :8092, etc.) in an upper full-screen frame
//      (#tsSubLayer, z-index 9100) instead of unloading :8090, so 「回桌面」 / 「主页」 reveals the
//      already-warm home page immediately without reloading :8090 or flashing XiaoZhi.
//    - Opens XiaoZhi's 「设置」 / 「WiFi」 sheets (z-index 9200+) directly over #tsHomeLayer when
//      tapped from the home dock, so closing 「设置」 returns straight to the home page.
//    - 「小智」 hides #tsHomeLayer; a wake word / alarm shows XiaoZhi automatically and the home
//      page comes back 30 s after that conversation (3 min after the last touch when opened by hand).
//    - While covered, XiaoZhi's DOM is hidden and its animation frames / CSS animations are parked.
//    - If the home service does not answer, XiaoZhi simply stays usable (retried every 30 s).
//  * Unread phone messages (#tsMsgCard, z-index 9500) sit above home and sub-pages until 「收到」.
//  * 8092 (server monitor): brightness button in the header; when framed inside #tsSubLayer,
//    「主页」 and 「亮度」 message the parent overlay instead of navigating top-level.
(() => {
  'use strict';
  if (!['127.0.0.1', 'localhost'].includes(location.hostname) || window.__tsHomeOverlay) return;
  const port = location.port, isXZ = port === '8090', isMusic = port === '8091', isMon = port === '8092';
  if (!isXZ && !isMusic && !isMon) return;
  window.__tsHomeOverlay = true;
  const framed = window.parent && window.parent !== window;
  const HOME = 'http://127.0.0.1:8095', MON = HOME;   // the display panel moved from the monitor app into the home service
  function okSubUrl(u) {
    try {
      const p = new URL(u);
      return p.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(p.hostname) && ['8091', '8092'].includes(p.port)
        && p.pathname === '/' && !p.username && !p.password && !p.search && !p.hash;
    } catch (e) { return false; }
  }
  const $ = id => document.getElementById(id);
  const sun = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg>';
  const house = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/></svg>';
  const msgIco = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H8l-4 4z"/><path d="M8 9h8M8 12.5h5"/></svg>';

  const style = document.createElement('style');
  style.textContent = `
  #tsHomeLayer{position:fixed;inset:0;z-index:9000;background:#0a111e;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .12s cubic-bezier(.4,0,.2,1),visibility 0s linear .12s}
  #tsHomeLayer iframe{border:0;width:100%;height:100%;display:block;background:#0a111e}
  html.ts-homeload #tsHomeLayer{opacity:1;visibility:visible;pointer-events:auto;transition:none}
  html.ts-homeload #tsHomeLayer iframe{opacity:0;pointer-events:none}
  html.ts-home #tsHomeLayer{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsSubLayer{position:fixed;inset:0;z-index:9100;background:#0a111e;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsSubLayer iframe{position:absolute;inset:0;border:0;width:100%;height:100%;display:block;visibility:hidden;pointer-events:none;z-index:0;background:#0a111e}
  #tsSubLayer.ready iframe.active,#tsSubLayer iframe.leaving{visibility:visible;z-index:1}
  html.ts-sub #tsSubLayer.ready iframe.active{pointer-events:auto}
  #tsSubLoading{position:absolute;inset:0;z-index:2;display:none;align-items:center;justify-content:center;flex-direction:column;gap:20px;background:radial-gradient(500px 350px at 50% 45%,#1b3154,#0a111e);color:#b8cee9;font:700 23px -apple-system,"Noto Sans CJK SC",sans-serif;pointer-events:none}
  html.ts-sub #tsSubLayer:not(.ready) #tsSubLoading{display:flex}
  #tsSubLoading .ts-dots{display:flex;gap:10px}#tsSubLoading i{width:9px;height:9px;border-radius:50%;background:#81cefc}
  html.ts-sub #tsSubLayer:not(.ready) #tsSubLoading i{animation:tsLoad 1.2s steps(8) infinite alternate}
  #tsSubLoading i:nth-child(2){animation-delay:.2s}#tsSubLoading i:nth-child(3){animation-delay:.4s}
  @keyframes tsLoad{from{opacity:.25;transform:translateY(0)}to{opacity:1;transform:translateY(-5px)}}
  html.ts-sub #tsSubLayer{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsSubErr{position:absolute;inset:0;z-index:3;display:none;flex-direction:column;align-items:center;justify-content:center;gap:18px;background:#0a111e;color:#e8f0fc;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif}
  #tsSubErr.show{display:flex}
  #tsSubErr p{margin:0;font-size:22px;font-weight:600;color:#9fb2cd}
  #tsSubErr button{height:54px;padding:0 28px;border:1px solid #8cb4ff40;border-radius:16px;background:#1b2b45;color:#eef4ff;font:700 18px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation}
  .app{contain:layout style paint;transition:visibility 0s linear 0s}
  html.ts-homeload .app{visibility:hidden!important;pointer-events:none!important;transition:none!important}
  html.ts-xz-covered .app{visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  html.ts-xz-covered body>*:not(#tsHomeLayer):not(#tsSubLayer):not(#tsMsgCard):not(#tsBrightnessPanel):not(#tsBrightnessShade):not(#settings):not(#wifi):not(#toast):not(#hud):not(#powerOff):not(.tskb-dock):not(style):not(script){visibility:hidden!important;pointer-events:none!important;transition:visibility 0s linear .14s}
  html.ts-home #settings,html.ts-home #wifi{z-index:9200;contain:strict;transform:translate3d(0,0,0);backface-visibility:hidden;transition:opacity .115s cubic-bezier(.4,0,.2,1)!important}
  html.ts-home #wifi{z-index:9205}
  html.ts-home #settings.on,html.ts-home #wifi.on{transition:opacity .14s cubic-bezier(.22,1,.36,1)!important}
  html.ts-sub #settings,html.ts-sub #wifi,html.ts-home #settings .sheet-bar,html.ts-home #settings .grid,html.ts-home #settings .panel,html.ts-home #wifi .sheet-bar,html.ts-home #wifi .panel{transform:none!important;transition:none!important}
  html.ts-home #toast,html.ts-sub #toast{z-index:9300}
  html.ts-home #hud,html.ts-sub #hud{z-index:9600}
  html.ts-home #powerOff,html.ts-sub #powerOff{z-index:9900}
  html.ts-xz-paused .app,html.ts-xz-paused .app *,html.ts-xz-paused .app *::before,html.ts-xz-paused .app *::after{animation:none!important;transition:none!important;will-change:auto!important}
  #tsHomeBtn,#tsBrightness{height:48px;padding:0 19px;display:flex;align-items:center;gap:9px;border:1px solid #9ec2ff29;border-radius:15px;background:#19253aeb;color:#eaf2ff;font:700 18.5px -apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;-webkit-tap-highlight-color:transparent;touch-action:manipulation;cursor:pointer;flex-shrink:0;box-shadow:0 8px 22px rgba(0,0,0,.28);transition:transform .14s cubic-bezier(.22,1,.36,1),background-color .14s ease}
  #tsHomeBtn{height:56px;padding:0 22px;border-radius:18px;font-size:20px}
  #tsHomeBtn:active,#tsBrightness:active{background:#263d5b;transform:scale(.95);transition:transform .05s ease-out,background-color .05s ease}#tsHomeBtn svg,#tsBrightness svg{width:22px;height:22px;color:#81cefc;flex-shrink:0}#tsHomeBtn svg{width:24px;height:24px}
  #tsHomeBtn.fixed{position:fixed;top:20px;right:24px;z-index:15}
  #tsBrightness.monitor-brightness{height:48px;border-radius:13px;font-size:19.5px;font-weight:700;padding:0 19px;gap:9px;border:1px solid #9ec2ff29;background:#1a2840eb;color:#eaf2ff;box-shadow:none}
  #tsBrightness.monitor-brightness svg{width:22px;height:22px}
  @keyframes tsMsgDrop{from{opacity:0;transform:translate(-50%,-14px)}to{opacity:1;transform:translate(-50%,0)}}
  @keyframes tsAckRing{from{opacity:.85;transform:scale(.96)}to{opacity:0;transform:scale(1.14)}}
  #tsBrightnessPanel{position:fixed;z-index:10001;left:50%;top:50%;transform:translate3d(-50%,calc(-50% + 8px),0);backface-visibility:hidden;width:520px;height:424px;max-width:calc(100% - 32px);max-height:calc(100% - 32px);border:1px solid #9cc2ff42;border-radius:26px;box-shadow:0 10px 28px #0008;background:#142033;overflow:hidden;display:block;opacity:0;visibility:hidden;pointer-events:none;contain:strict;transition:opacity .115s cubic-bezier(.4,0,.2,1),transform .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsBrightnessPanel.open{opacity:1;visibility:visible;pointer-events:auto;transform:translate3d(-50%,-50%,0);transition:opacity .145s cubic-bezier(.22,1,.36,1),transform .155s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsBrightnessPanel iframe{position:relative;z-index:1;border:0;width:100%;height:100%;display:block;opacity:0;pointer-events:none;transition:opacity .1s ease}
  #tsBrightnessPanel.ready iframe{opacity:1;pointer-events:auto}
  #tsBrightFallback{position:absolute;inset:0;z-index:2;display:none;flex-direction:column;align-items:center;justify-content:center;gap:18px;color:#b8cee9;font:700 20px -apple-system,"Noto Sans CJK SC",sans-serif}
  #tsBrightnessPanel.open:not(.ready) #tsBrightFallback{display:flex}
  #tsBrightFallback button{height:48px;padding:0 26px;border:1px solid #8cb4ff40;border-radius:14px;background:#1b2b45;color:#eef4ff;font:700 17px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation}
  #tsBrightnessShade{position:fixed;inset:0;background:rgba(2,6,14,.64);z-index:10000;display:block;opacity:0;visibility:hidden;pointer-events:none;transition:opacity .115s cubic-bezier(.4,0,.2,1),visibility 0s linear .115s}
  #tsBrightnessShade.open{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .135s cubic-bezier(.22,1,.36,1),visibility 0s linear 0s}
  #tsMsgCard{position:fixed;z-index:9500;top:22px;left:50%;transform:translateX(-50%);width:800px;max-width:calc(100% - 40px);display:none;gap:20px;align-items:center;padding:20px 24px 20px 28px;border-radius:24px;background:#1b2436;border:1px solid #ffc85766;box-shadow:0 10px 26px #0008;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif;color:#eef3fb;-webkit-user-select:none;user-select:none;contain:layout paint}
  #tsMsgCard.show{display:flex;animation:tsMsgDrop .26s cubic-bezier(.22,1,.36,1) backwards}
  #tsMsgCard .tm-b{flex:1;min-width:0}
  #tsMsgCard .tm-h{font-size:18px;font-weight:700;color:#ffd98a;display:flex;gap:8px;align-items:center}
  #tsMsgCard .tm-tag{display:inline-flex;align-items:center;gap:7px}
  #tsMsgCard .tm-tag svg{width:20px;height:20px;flex-shrink:0}
  #tsMsgCard .tm-t{font-size:28px;font-weight:700;line-height:1.42;margin-top:8px;word-break:break-all;max-height:162px;overflow:hidden;white-space:pre-line}
  #tsMsgCard button{position:relative;height:70px;min-width:132px;padding:0 26px;border:0;border-radius:18px;background:#f5c04a;color:#0a101c;font:800 22px -apple-system,"Noto Sans CJK SC",sans-serif;cursor:pointer;touch-action:manipulation;flex-shrink:0;-webkit-tap-highlight-color:transparent;transition:transform .14s cubic-bezier(.22,1,.36,1)}
  #tsMsgCard.show button::after{display:none}
  #tsMsgCard.settled button::after{animation:none;will-change:auto}
  #tsMsgCard button:active{filter:brightness(.92);transform:scale(.95);transition:transform .05s ease-out}
  #tsMsgCard button:disabled{opacity:.6}
  @media(max-width:620px){#tsBrightnessPanel{width:calc(100% - 24px)}}
  @media(prefers-reduced-motion:reduce){#tsHomeLayer,#tsSubLayer,#tsSubLayer *,#tsMsgCard *,#tsMsgCard *::after,#tsBrightnessPanel,#tsBrightnessShade{animation:none!important;transition:none!important}}
  /* 2026-11-12: brightness panel taller for the 深色 / 浅色 row and in the home page's colours; light versions of the overlay layers */
  #tsBrightnessPanel{height:500px;background:#121a2b;border-color:#232e45}
  html.ts-light #tsHomeLayer,html.ts-light #tsHomeLayer iframe,html.ts-light #tsSubLayer,html.ts-light #tsSubLayer iframe,html.ts-light #tsSubErr{background:#e3dbce}
  html.ts-light #tsSubLoading{background:#e3dbce;color:#424449}
  html.ts-light #tsSubLoading i{background:#0f6d63}
  html.ts-light #tsSubErr{color:#18191b}html.ts-light #tsSubErr p{color:#424449}
  html.ts-light #tsSubErr button,html.ts-light #tsBrightFallback button{background:#f6f2ea;border-color:#d6cdbf;color:#18191b}
  html.ts-light #tsBrightnessPanel{background:#f6f2ea;border-color:#d6cdbf;box-shadow:0 0 0 1px #d6cdbf,0 10px 26px rgba(28,24,18,.20)}
  html.ts-light #tsBrightFallback{color:#46484d}
  html.ts-light #tsBrightnessShade{background:rgba(24,22,19,.42)}
  html.ts-light #tsMsgCard{background:#f6f2ea;border-color:#dab984;color:#18191b;box-shadow:0 0 0 1px #dab984,0 10px 24px rgba(28,24,18,.18)}
  html.ts-light #tsMsgCard .tm-h{color:#84510a}
  html.ts-light #tsMsgCard button{background:#a26008;color:#faf8f4}
  `;
  document.head.appendChild(style);

  /* ---------------- brightness panel (monitor header button; home tile via postMessage) ---------------- */
  let brightBtn = null, brightShade = null, brightPanel = null, brightFrame = null, homeFrameRef = null;
  let previousFocus = null, brightReady = false, brightFailed = false, brightStartedAt = 0, brightPauseTimer = 0, brightRetryTimer = 0, brightRetries = 0;
  let brightFbTx = null;
  function brightnessOpen() { return !!(brightPanel && brightPanel.isConnected && brightPanel.classList.contains('open')); }
  function syncBrightnessVis(vis) {
    try { brightFrame?.contentWindow?.postMessage({ ts: 'display', visible: vis }, MON); } catch (e) {}
  }
  function loadBrightnessSrc(bframe, prewarm) {
    clearTimeout(brightRetryTimer); brightRetryTimer = 0;
    brightReady = false; brightFailed = false; brightStartedAt = Date.now();
    if (brightFbTx) brightFbTx.textContent = '正在加载亮度设置…';
    brightPanel?.classList.remove('ready');
    if (prewarm) bframe.name = 'ts-display-prewarm';
    bframe.src = MON + '/display';
  }
  function ensureBrightnessDom(prewarm = false) {
    if (!brightShade || !brightPanel || !brightShade.isConnected || !brightPanel.isConnected) {
      $('tsBrightnessShade')?.remove(); $('tsBrightnessPanel')?.remove();
      const shade = document.createElement('div'); shade.id = 'tsBrightnessShade'; shade.onclick = closeBrightness;
      const panel = document.createElement('div'); panel.id = 'tsBrightnessPanel'; panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', '屏幕亮度设置');
      const fb = document.createElement('div'); fb.id = 'tsBrightFallback';
      const fbTx = document.createElement('span'); fbTx.textContent = '正在加载亮度设置…';
      const fbBtn = document.createElement('button'); fbBtn.type = 'button'; fbBtn.textContent = '关闭'; fbBtn.onclick = closeBrightness;
      fb.append(fbTx, fbBtn);
      const bframe = document.createElement('iframe'); bframe.title = '屏幕亮度设置';
      bframe.addEventListener('load', () => {
        setTimeout(() => {
          if (brightFrame !== bframe || brightReady) return;
          brightFailed = true;
          if (brightRetries < 6) {
            brightRetries++;
            clearTimeout(brightRetryTimer);
            brightRetryTimer = setTimeout(() => {
              brightRetryTimer = 0;
              if (brightFrame === bframe && !brightReady) loadBrightnessSrc(bframe, !brightnessOpen());
            }, brightnessOpen() ? 1200 : 2000);
          } else if (brightFbTx) {
            brightFbTx.textContent = '亮度服务暂不可用，请关闭后重试';
          }
        }, 220);
      });
      panel.append(bframe, fb); document.body.append(shade, panel);
      brightShade = shade; brightPanel = panel; brightFrame = bframe; brightFbTx = fbTx;
      loadBrightnessSrc(bframe, prewarm);
    }
    return { shade: brightShade, panel: brightPanel, bframe: brightFrame };
  }
  function closeBrightness(restoreFocus = true) {
    if (!brightPanel?.classList.contains('open') && !brightShade?.classList.contains('open')) return;
    brightPanel?.classList.remove('open'); brightShade?.classList.remove('open'); brightBtn?.setAttribute('aria-expanded', 'false');
    clearTimeout(brightPauseTimer);
    brightPauseTimer = setTimeout(() => { brightPauseTimer = 0; if (!brightnessOpen()) syncBrightnessVis(false); }, 125);
    const pf = previousFocus; previousFocus = null;
    if (restoreFocus) pf?.focus?.();
  }
  function openBrightness() {
    if (brightnessOpen()) return closeBrightness();
    clearTimeout(brightPauseTimer); brightPauseTimer = 0;
    brightRetries = 0;
    previousFocus = document.activeElement;
    const { shade, panel, bframe } = ensureBrightnessDom(false);
    if (bframe) {
      try { bframe.name = ''; } catch (e) {}
      if (brightFailed || (!brightReady && Date.now() - brightStartedAt > 2000)) {
        loadBrightnessSrc(bframe, false);
      }
    }
    shade.classList.add('open'); panel.classList.add('open');
    brightBtn?.setAttribute('aria-expanded', 'true');
    syncBrightnessVis(true);
  }
  window.addEventListener('message', e => {
    const bwin = brightFrame?.contentWindow;
    if (bwin && e.origin === MON && e.source === bwin) {
      if (e.data?.thinksmart === 'displayReady') {
        clearTimeout(brightRetryTimer); brightRetryTimer = 0;
        brightReady = true; brightFailed = false; brightRetries = 0;
        brightPanel?.classList.add('ready');
        syncBrightnessVis(brightnessOpen());
      } else if (e.data?.thinksmart === 'closeBrightness') closeBrightness();
    }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeBrightness(); });
  if (isMon) {
    const subParentOrigin = (() => {
      try {
        const o = document.referrer ? new URL(document.referrer).origin : '';
        if (o === 'http://127.0.0.1:8090' || o === 'http://localhost:8090') return o;
      } catch (e) {}
      return 'http://127.0.0.1:8090';
    })();
    const homeEl = document.querySelector('.home');
    if (framed && homeEl) {
      homeEl.addEventListener('click', e => {
        e.preventDefault();
        window.parent.postMessage({ ts: 'sub', cmd: 'close' }, subParentOrigin);
      });
    }
    if (!$('tsBrightness')) {
      brightBtn = document.createElement('button'); brightBtn.id = 'tsBrightness'; brightBtn.className = 'monitor-brightness';
      brightBtn.setAttribute('aria-label', '调节屏幕亮度'); brightBtn.setAttribute('aria-expanded', 'false'); brightBtn.innerHTML = sun + '亮度';
      brightBtn.onclick = framed ? () => window.parent.postMessage({ ts: 'sub', cmd: 'brightness' }, subParentOrigin) : openBrightness;
      if (homeEl) homeEl.after(brightBtn); else document.body.appendChild(brightBtn);
    }
    if (framed) window.parent.postMessage({ ts: 'sub', cmd: 'ready' }, subParentOrigin);
  }

  /* ---------------- message card (unread phone messages, until 「收到」) ---------------- */
  let card = null, dismissInboxById = () => {}, triggerInboxPoll = () => {}, isHomeActiveView = () => false, syncInboxSchedule = () => {}, stopOverlayClip = () => {};
  if (!framed) {
    card = document.createElement('div'); card.id = 'tsMsgCard'; card.setAttribute('role', 'alert');
    card.innerHTML = `<div class="tm-b"><div class="tm-h"><span class="tm-tag">${msgIco}新留言</span><span id="tsMsgMeta"></span></div><div class="tm-t" id="tsMsgText"></div></div><button id="tsMsgAck">收到</button>`;
    document.body.appendChild(card);
    const msgMetaEl = $('tsMsgMeta'), msgTextEl = $('tsMsgText'), msgAckEl = $('tsMsgAck');
    let shownMsg = null, msgBusy = false, msgStillTimer = 0, unreadCache = [], unreadTotal = 0, inboxTimer = 0, inboxNextDue = 0;
    function armInboxTimer(ms) {
      clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0;
      if (document.hidden) return;
      const delay = ms !== undefined ? ms : (isHomeActiveView() ? 30000 : 5000);
      inboxNextDue = Date.now() + delay;
      inboxTimer = setTimeout(() => { inboxTimer = 0; inboxNextDue = 0; pollInbox(); }, delay);
    }
    syncInboxSchedule = () => {
      if (document.hidden) { clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0; return; }
      if (!inboxTimer) armInboxTimer();
      else if (!isHomeActiveView() && inboxNextDue - Date.now() > 5000) armInboxTimer(5000);
    };
    const settleMessage = () => {
      clearTimeout(msgStillTimer); card.classList.remove('settled');
      msgStillTimer = setTimeout(() => card.classList.add('settled'), 30000);
    };
    card.addEventListener('pointerdown', settleMessage, { passive: true });
    async function inboxRequest(path, opt = {}) {
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 8000);
      try {
        const r = await fetch(HOME + path, Object.assign({ cache: 'no-store' }, opt, { signal: ctl.signal }));
        if (!r.ok) throw new Error('inbox');
        return await r.json();
      } finally { clearTimeout(timer); }
    }
    const pad = n => String(n).padStart(2, '0');
    function renderUnreadCard() {
      const m = unreadCache[0];
      if (!m) {
        retryMsgId = null;
        if (shownMsg !== null || card.classList.contains('show')) { shownMsg = null; clearTimeout(msgStillTimer); card.classList.remove('show'); }
        return;
      }
      const d = new Date(m.at * 1000);
      const count = Math.max(unreadTotal, unreadCache.length);
      const dd = m.due ? new Date(m.due * 1000) : d;
      const from = m.due ? `提醒${m.who || ''}` : (m.who ? `${m.who}发来` : '来自手机');
      const meta = `${pad(dd.getHours())}:${pad(dd.getMinutes())} · ${from}${count > 1 ? ` · 还有 ${count - 1} 条` : ''}`;
      const shownText = m.kind === 'voice' && m.transcript ? `“${m.transcript}”` : m.text;
      if (msgTextEl.textContent !== shownText) msgTextEl.textContent = shownText;
      if (msgMetaEl.textContent !== meta) msgMetaEl.textContent = meta;
      if (shownMsg !== m.id) { if (retryMsgId && retryMsgId !== m.id) retryMsgId = null; settleMessage(); }
      const wantAckLbl = retryMsgId === m.id
        ? (m.kind === 'voice' ? '▶ 重试' : '重试')
        : (m.kind === 'voice' ? `▶ 播放 · ${Math.max(1, Math.round(Number(m.duration) || 3))}″` : (m.due ? '知道了' : '收到'));
      if (msgAckEl.textContent !== wantAckLbl) msgAckEl.textContent = wantAckLbl;
      shownMsg = m.id; if (!card.classList.contains('show')) card.classList.add('show');
    }
    let inboxFetching = false, inboxPending = false, inboxAckEpoch = 0, retryMsgId = null, overlayClipUntil = 0;
    stopOverlayClip = () => {
      if (Date.now() < overlayClipUntil) {
        overlayClipUntil = 0;
        inboxRequest('/api/home/messages/play', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"stop":true}' }).catch(() => {});
      }
    };
    dismissInboxById = id => {
      inboxAckEpoch++;
      if (retryMsgId === id) retryMsgId = null;
      const idx = unreadCache.findIndex(x => x.id === id);
      if (idx >= 0) {
        unreadCache.splice(idx, 1);
        unreadTotal = Math.max(0, unreadTotal - 1);
        renderUnreadCard();
      } else if (shownMsg === id) {
        shownMsg = null; clearTimeout(msgStillTimer); card.classList.remove('show');
      }
    };
    async function pollInbox() {
      if (document.hidden) { clearTimeout(inboxTimer); inboxTimer = 0; inboxNextDue = 0; return; }
      if (msgBusy) { armInboxTimer(1000); return; }
      if (inboxFetching) { inboxPending = true; return; }
      inboxFetching = true;
      const myEpoch = inboxAckEpoch;
      try {
        const j = await inboxRequest('/api/home/inbox');
        if (myEpoch !== inboxAckEpoch || msgBusy) return;
        const nextTotal = Number(j?.count) || 0;
        if (nextTotal === 0 && unreadTotal === 0 && unreadCache.length === 0 && shownMsg === null) return;
        unreadCache = Array.isArray(j?.unread) ? j.unread.slice() : [];
        unreadTotal = nextTotal || unreadCache.length;
        renderUnreadCard();
      } catch (e) {} finally {
        inboxFetching = false;
        if (inboxPending && !document.hidden && !msgBusy) {
          inboxPending = false;
          pollInbox();
        } else {
          armInboxTimer();
        }
      }
    }
    triggerInboxPoll = () => { pollInbox(); };
    msgAckEl.onclick = async () => {
      if (!shownMsg || msgBusy) return;
      const targetId = shownMsg;
      const targetItem = unreadCache.find(x => x.id === targetId) || null;
      retryMsgId = null;
      msgBusy = true;
      msgAckEl.disabled = true;
      dismissInboxById(targetId);
      try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', inboxAck: targetId, kind: targetItem?.kind, duration: targetItem?.duration }, HOME); } catch (e) {}
      try {
        const j = await inboxRequest('/api/home/inbox/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: targetId }) });
        if (!j.ok) throw new Error('ack');
        if (targetItem?.kind === 'voice') overlayClipUntil = Date.now() + Math.max(1, Math.min(60, Number(targetItem.duration) || 4)) * 1000 + 1200;
        setTimeout(pollInbox, 200);
      } catch (e) {
        overlayClipUntil = 0;
        msgBusy = false;
        msgAckEl.disabled = false;
        retryMsgId = targetId;
        if (targetItem && !unreadCache.some(x => x.id === targetId)) {
          unreadCache.unshift(targetItem);
          unreadTotal = Math.max(unreadTotal + 1, unreadCache.length);
        }
        renderUnreadCard();
        try { homeFrameRef?.contentWindow?.postMessage({ ts: 'home', inboxRefresh: true, inboxAckFail: targetId }, HOME); } catch (err) {}
      } finally { msgBusy = false; msgAckEl.disabled = false; }
    };
    pollInbox();
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollInbox(); else syncInboxSchedule(); });
  }

  if (!isXZ) return;

  /* ---------------- home & sub-page layers over XiaoZhi ---------------- */
  const root = document.documentElement;
  const layer = document.createElement('div'); layer.id = 'tsHomeLayer';
  const frame = document.createElement('iframe'); frame.title = '主页';
  homeFrameRef = frame;
  layer.appendChild(frame);

  const subLayer = document.createElement('div'); subLayer.id = 'tsSubLayer';
  const subLoading = document.createElement('div'); subLoading.id = 'tsSubLoading';
  subLoading.innerHTML = '<span id="tsSubLabel">正在打开应用</span><span class="ts-dots" aria-hidden="true"><i></i><i></i><i></i></span>';
  const subErr = document.createElement('div'); subErr.id = 'tsSubErr';
  subErr.innerHTML = '<p>页面暂时无法打开</p><button type="button" id="tsSubBack">返回主页</button>';
  subLayer.append(subLoading, subErr);
  document.body.append(layer, subLayer);
  const subLabelEl = $('tsSubLabel');
  let settingsEl = $('settings'), wifiEl = $('wifi'), armXzStill = () => {};
  const getSettings = () => settingsEl || (settingsEl = $('settings'));
  const getWifi = () => wifiEl || (wifiEl = $('wifi'));

  let view = 'home';
  try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
  let homeOk = false, autoReturn = false, lastTouch = Date.now(), lastBusy = 0, readyTimer = 0, xzQuietTimer = 0;
  let subOpen = false, subUrl = '', subShowTimer = 0, activeSub = null;
  const subEntries = new Map(), subPauseTimers = new Map();
  const BUSY = new Set(['listening', 'thinking', 'speaking']);
  const isBusy = () => BUSY.has(document.body.dataset.state || 'idle') || document.body.classList.contains('ringing');
  const sheetOpen = () => !!(getSettings()?.classList.contains('on') || getWifi()?.classList.contains('on'));
  isHomeActiveView = () => view === 'home' && homeOk && !subOpen && !sheetOpen();

  // 「主页」 button on XiaoZhi's page (top bar, falls back to a fixed corner)
  const homeBtn = document.createElement('button'); homeBtn.id = 'tsHomeBtn'; homeBtn.setAttribute('aria-label', '回到主页');
  homeBtn.innerHTML = house + '主页'; homeBtn.style.display = 'none';
  const bar = document.querySelector('.top .right');
  if (bar) bar.prepend(homeBtn); else { homeBtn.classList.add('fixed'); document.body.appendChild(homeBtn); }

  // Guard XiaoZhi's 1s clock tick from unconditionally dirtying unchanged text nodes (saves 59/60 CPU repaints/min)
  const tcDesc = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
  if (tcDesc?.get && tcDesc?.set) {
    for (const id of ['time', 'bigTime', 'bigDate', 'greet', 'ringTime', 'timerText']) {
      const el = $(id);
      if (el) {
        try {
          Object.defineProperty(el, 'textContent', {
            configurable: true,
            get() { return tcDesc.get.call(this); },
            set(v) { const s = String(v ?? ''); if (tcDesc.get.call(this) !== s) tcDesc.set.call(this, s); }
          });
        } catch (e) {}
      }
    }
  }

  // park XiaoZhi's animation frames while it is hidden behind the home page or a sub-page
  const nativeRaf = window.requestAnimationFrame.bind(window), nativeCaf = window.cancelAnimationFrame.bind(window);
  const parked = new Map(), moved = new Map(); let parkSeq = 0;
  window.requestAnimationFrame = cb => {
    if ((!root.classList.contains('ts-xz-covered') && !root.classList.contains('ts-homeload')) || sheetOpen()) return nativeRaf(cb);
    if (parked.size >= 64) parked.delete(parked.keys().next().value);
    const id = -(++parkSeq); parked.set(id, cb); return id;
  };
  window.cancelAnimationFrame = id => {
    if (typeof id === 'number' && id < 0) { parked.delete(id); const n = moved.get(id); if (n !== undefined) { moved.delete(id); nativeCaf(n); } }
    else nativeCaf(id);
  };
  const flushParked = () => { moved.clear(); for (const [id, cb] of parked) moved.set(id, nativeRaf(ts => { moved.delete(id); cb(ts); })); parked.clear(); };

  let homeVisTimer = 0, lastHomeVis = null, xzPauseTimer = 0, subFadeTimer = 0;
  function syncHomeVis(vis, immediate = false) {
    clearTimeout(homeVisTimer);
    const send = () => {
      if (lastHomeVis === vis) return;
      lastHomeVis = vis;
      try { frame.contentWindow?.postMessage({ ts: 'home', visible: vis }, HOME); } catch (e) {}
    };
    if (vis || immediate) send();
    else homeVisTimer = setTimeout(send, 150);
  }
  function syncSubVis(entry, vis, immediate = false) {
    if (!entry) return;
    clearTimeout(subPauseTimers.get(entry.url));
    subPauseTimers.delete(entry.url);
    const send = () => {
      try { entry.frame.contentWindow?.postMessage({ ts: 'sub', visible: vis }, entry.origin); } catch (e) {}
    };
    if (vis || immediate) send();
    else subPauseTimers.set(entry.url, setTimeout(() => { subPauseTimers.delete(entry.url); send(); }, 130));
  }

  function apply() {
    const wasCov = root.classList.contains('ts-xz-covered');
    const showHome = view === 'home' && homeOk;
    if (homeOk) root.classList.remove('ts-homeload');
    const homeload = root.classList.contains('ts-homeload');
    const xzCov = showHome || subOpen || homeload;
    root.classList.toggle('ts-home', showHome);
    root.classList.toggle('ts-xz-covered', xzCov);
    clearTimeout(xzPauseTimer);
    if (xzCov) {
      if (sheetOpen() && parked.size) flushParked();
      if (!root.classList.contains('ts-xz-paused')) {
        if (homeload) root.classList.add('ts-xz-paused');
        else xzPauseTimer = setTimeout(() => { xzPauseTimer = 0; if (root.classList.contains('ts-xz-covered')) root.classList.add('ts-xz-paused'); }, 150);
      }
    } else {
      root.classList.remove('ts-xz-paused');
      flushParked();
      if (wasCov) armXzStill();
    }
    syncHomeVis(showHome && !subOpen && !sheetOpen());
    syncInboxSchedule();
    syncXzQuietTimer();
    const wantDisp = homeOk ? '' : 'none';
    if (homeBtn.style.display !== wantDisp) homeBtn.style.display = wantDisp;
  }
  function setView(v) {
    view = v;
    try { sessionStorage.setItem('tsView', v); } catch (e) {}
    if (v === 'xiaozhi') lastTouch = Date.now();
    if (v === 'home') closeBrightness(false);
    apply();
  }
  const subRetryCounts = new Map(), subRetryTimers = new Map();
  function forceCloseSheets() {
    const s = getSettings(), w = getWifi(), ae = document.activeElement;
    if (ae && ((s && s.contains(ae)) || (w && w.contains(ae)))) { try { ae.blur(); } catch (e) {} }
    s?.classList.remove('on');
    w?.classList.remove('on');
  }
  function attachSubLoadGuard(entry, title, winName) {
    entry.frame.addEventListener('load', () => {
      setTimeout(() => {
        if (subEntries.get(entry.url) !== entry || entry.ready) return;
        entry.loading = false;
        entry.failed = true;
        const n = (subRetryCounts.get(entry.url) || 0) + 1;
        subRetryCounts.set(entry.url, n);
        if (subOpen && activeSub === entry) {
          if (n <= 8) {
            clearTimeout(subRetryTimers.get(entry.url));
            subRetryTimers.set(entry.url, setTimeout(() => {
              subRetryTimers.delete(entry.url);
              if (subOpen && activeSub === entry && !entry.ready) {
                entry.failed = false;
                entry.loading = true;
                entry.startedAt = Date.now();
                entry.frame.src = entry.url;
              }
            }, 1000));
          } else {
            clearTimeout(subShowTimer);
            subErr.classList.add('show');
          }
        } else {
          entry.frame.remove();
          subEntries.delete(entry.url);
          if (n <= 8) {
            clearTimeout(subRetryTimers.get(entry.url));
            subRetryTimers.set(entry.url, setTimeout(() => {
              subRetryTimers.delete(entry.url);
              prewarmSub(entry.url, title, winName);
            }, 2200));
          }
        }
      }, 250);
    });
  }
  function openSub(url, subCmd = '') {
    if (!okSubUrl(url)) return false;
    const cleanSubCmd = (typeof subCmd === 'string' && /^[a-zA-Z0-9_-]{1,24}$/.test(subCmd)) ? subCmd : '';
    const u = new URL(url), key = u.origin + '/';
    if (subOpen && activeSub && activeSub.url === key) {
      if (cleanSubCmd) {
        if (activeSub.ready) {
          try { activeSub.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cleanSubCmd }, activeSub.origin); } catch (e) {}
        } else {
          activeSub.pendingCmd = cleanSubCmd;
        }
      }
      return true;
    }
    clearTimeout(subFadeTimer); subFadeTimer = 0;
    clearTimeout(subRetryTimers.get(key)); subRetryTimers.delete(key);
    subRetryCounts.delete(key);
    closeBrightness(false);
    forceCloseSheets();
    if (activeSub && activeSub.url !== key) {
      activeSub.frame.classList.remove('active', 'leaving');
      syncSubVis(activeSub, false);
    }
    let entry = subEntries.get(key);
    if (entry && (entry.failed || (!entry.ready && (!entry.loading || Date.now() - entry.startedAt > 7000)))) {
      entry.frame.remove();
      subEntries.delete(key);
      entry = null;
    }
    if (!entry) {
      const f = document.createElement('iframe');
      const title = u.port === '8091' ? '网易云音乐' : '服务器监控';
      const winName = u.port === '8091' ? 'ts-prewarm' : 'ts-monitor-prewarm';
      f.title = title;
      subLayer.insertBefore(f, subLoading);
      entry = { url: key, origin: u.origin, port: u.port, frame: f, ready: false, loading: false, failed: false, startedAt: 0, pendingCmd: '' };
      subEntries.set(key, entry);
      attachSubLoadGuard(entry, title, winName);
    } else if (!entry.ready) {
      try { entry.frame.name = ''; } catch (e) {}
    }
    entry.pendingCmd = cleanSubCmd;
    activeSub = entry;
    for (const v of subEntries.values()) {
      v.frame.classList.remove('leaving');
      v.frame.classList.toggle('active', v === entry);
    }
    subOpen = true;
    subUrl = key;
    autoReturn = false;
    view = 'home';
    try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
    clearTimeout(subShowTimer);
    subErr.classList.remove('show');
    if (entry.ready) {
      subLayer.classList.add('ready', 'warm');
      root.classList.add('ts-sub');
      apply();
      syncSubVis(entry, true, true);
      if (entry.pendingCmd) {
        const cmdToSend = entry.pendingCmd; entry.pendingCmd = '';
        try { entry.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cmdToSend }, entry.origin); } catch (e) {}
      }
      return true;
    }
    subLayer.classList.remove('ready', 'warm');
    subLabelEl.textContent = u.port === '8091' ? '正在打开网易云音乐' : '正在连接服务器';
    root.classList.add('ts-sub');
    apply();
    if (!entry.loading) {
      entry.loading = true;
      entry.startedAt = Date.now();
      entry.frame.src = key;
    }
    // A load event also fires for an HTTP error page. Only our child page's handshake is ready.
    subShowTimer = setTimeout(() => {
      if (subOpen && activeSub === entry && !entry.ready) {
        entry.failed = true;
        entry.loading = false;
        subErr.classList.add('show');
      }
    }, 8000);
    return true;
  }
  function closeSub(toHome = true) {
    if (!subOpen) return;
    const cur = activeSub;
    const smoothExit = toHome && !document.body.classList.contains('ringing') && !!(cur && cur.ready);
    subOpen = false;
    subUrl = '';
    activeSub = null;
    clearTimeout(subShowTimer);
    clearTimeout(subFadeTimer); subFadeTimer = 0;
    subLayer.classList.remove('ready', 'warm');
    subErr.classList.remove('show');
    closeBrightness(false);
    if (cur) {
      clearTimeout(subRetryTimers.get(cur.url));
      subRetryTimers.delete(cur.url);
      cur.frame.classList.remove('active');
      if (cur.ready) {
        if (smoothExit) {
          cur.frame.classList.add('leaving');
          subFadeTimer = setTimeout(() => { subFadeTimer = 0; cur.frame.classList.remove('leaving'); }, 125);
        } else {
          cur.frame.classList.remove('leaving');
        }
        syncSubVis(cur, false, !smoothExit);
      } else {
        cur.frame.classList.remove('leaving');
        cur.frame.remove();
        subEntries.delete(cur.url);
      }
    }
    if (toHome && !document.body.classList.contains('ringing')) {
      if (isBusy()) {
        autoReturn = true;
        view = 'xiaozhi';
      } else {
        autoReturn = false;
        view = 'home';
        try { sessionStorage.setItem('tsView', 'home'); } catch (e) {}
      }
    }
    apply();
    root.classList.remove('ts-sub');
    if (toHome && cur && cur.port === '8092') setTimeout(pollServerBadge, 350);   // monitor not deployed; no-op
  }
  let prewarmScheduled = false;
  function prewarmSub(key, title, winName) {
    if (subOpen || !homeOk || isBusy() || subEntries.has(key)) return;
    const u = new URL(key);
    const f = document.createElement('iframe');
    f.title = title;
    f.name = winName;
    subLayer.insertBefore(f, subLoading);
    const entry = { url: key, origin: u.origin, port: u.port, frame: f, ready: false, loading: true, failed: false, startedAt: Date.now() };
    subEntries.set(key, entry);
    attachSubLoadGuard(entry, title, winName);
    f.src = key;
  }
  function scheduleIdlePrewarm() {
    if (prewarmScheduled) return;
    prewarmScheduled = true;
    // Idle prewarm of the brightness panel (served by the home service at :8095/display);
    // the QQ-music / server-monitor sub-pages are not deployed in this build.
    setTimeout(() => { if (!brightnessOpen() && homeOk && !isBusy()) ensureBrightnessDom(true); }, 2300);
  }
  let srvBadgeTimer = 0, lastSrvBadgeSig = '', lastSrvOpenSeq = null, srvVoicePollT1 = 0, srvVoicePollT2 = 0;
  const MONITOR_DEPLOYED = false;   // the server-monitor app is not part of this build
  async function pollServerBadge(forceCheck = false) {
    if (!MONITOR_DEPLOYED) return;
    clearTimeout(srvBadgeTimer); srvBadgeTimer = 0;
    if (document.hidden || !homeOk) return;
    if (isHomeActiveView() || forceCheck) {
      const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 5000);
      try {
        const r = await fetch(MON + '/api/badge', { cache: 'no-store', signal: ctl.signal });
        if (r.ok) {
          const j = await r.json();
          const sig = `${j?.text || ''}:${j?.amber ? 1 : 0}:${j?.bad ? 1 : 0}`;
          if (sig !== lastSrvBadgeSig) {
            lastSrvBadgeSig = sig;
            try { frame.contentWindow?.postMessage({ ts: 'home', srvBadge: { text: j?.text || '', amber: !!j?.amber, bad: !!j?.bad } }, HOME); } catch (e) {}
          }
          const seq = Number(j?.open_ui_seq) || 0;
          if (lastSrvOpenSeq === null) {
            lastSrvOpenSeq = seq;
            if (forceCheck && seq > 0 && j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
              openSub(MON + '/', j.open_ui_cmd || 'overview');
            }
          } else if (seq > lastSrvOpenSeq) {
            lastSrvOpenSeq = seq;
            if (j?.open_ui_fresh && !document.body.classList.contains('ringing')) {
              openSub(MON + '/', j.open_ui_cmd || 'overview');
            }
          }
        }
      } catch (e) {} finally { clearTimeout(t); }
    }
    if (!document.hidden && homeOk) srvBadgeTimer = setTimeout(pollServerBadge, 60000);
  }
  function scheduleVoiceSrvCheck() {
    if (document.hidden || !homeOk) return;
    clearTimeout(srvVoicePollT1); clearTimeout(srvVoicePollT2);
    srvVoicePollT1 = setTimeout(() => pollServerBadge(true), 140);
    srvVoicePollT2 = setTimeout(() => pollServerBadge(true), 920);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && homeOk) pollServerBadge(); });
  /* ---------------- 深色 / 浅色 (2026-11-12): picked in the brightness panel, remembered here (8090 storage),
     sent to the home page, the sub pages and the panel; each of them also caches it for a flash-free start ---------------- */
  const THEME_KEY = 'ts.theme';
  let uiTheme = 'dark';
  try { const t = localStorage.getItem(THEME_KEY); if (t === 'light' || t === 'dark') uiTheme = t; } catch (e) {}
  // 2026-11-13 自动: 浅色 from sunrise to sunset (the home page sends the times; 07:00 / 18:00 until it has), 深色 at night.
  // It is the default; picking 深色 or 浅色 in the brightness panel turns it off, 自动 turns it back on.
  const MODE_KEY = 'ts.themeMode', SUN_KEY = 'ts.sun';
  let themeMode = 'auto', sunMin = null;
  try { if (localStorage.getItem(MODE_KEY) === 'manual') themeMode = 'manual'; } catch (e) {}
  const okSun = (r, s) => Number.isFinite(r) && Number.isFinite(s) && r >= 240 && r <= 570 && s >= 930 && s <= 1260;
  try { const j = JSON.parse(localStorage.getItem(SUN_KEY) || 'null'); if (j && okSun(j.rise, j.set)) sunMin = { rise: j.rise, set: j.set }; } catch (e) {}
  const autoTheme = () => { const n = new Date(), m = n.getHours() * 60 + n.getMinutes(), r = sunMin ? sunMin.rise : 420, s = sunMin ? sunMin.set : 1080; return m >= r && m < s ? 'light' : 'dark'; };
  if (themeMode === 'auto') { uiTheme = autoTheme(); try { localStorage.setItem(THEME_KEY, uiTheme); } catch (e) {} }
  root.classList.toggle('ts-light', uiTheme === 'light');
  const sendTheme = (win, origin, kind) => { try { win?.postMessage({ ts: kind, theme: uiTheme, mode: themeMode }, origin); } catch (e) {} };
  function broadcastTheme() {
    sendTheme(frame.contentWindow, HOME, 'home');
    for (const v of subEntries.values()) sendTheme(v.frame.contentWindow, v.origin, 'sub');
    sendTheme(brightFrame?.contentWindow, MON, 'display');
  }
  function setUiTheme(t) {
    if (t !== 'light' && t !== 'dark') return;
    if (t !== uiTheme) {
      uiTheme = t;
      try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
      root.classList.toggle('ts-light', t === 'light');
    }
    broadcastTheme();
  }
  function applyAutoTheme() { if (themeMode === 'auto' && autoTheme() !== uiTheme) setUiTheme(autoTheme()); }
  function setThemeMode(m) { themeMode = m === 'auto' ? 'auto' : 'manual'; try { localStorage.setItem(MODE_KEY, themeMode); } catch (e) {} }
  function setSun(rise, set) {
    if (!okSun(rise, set)) return;
    sunMin = { rise, set };
    try { localStorage.setItem(SUN_KEY, JSON.stringify(sunMin)); } catch (e) {}
    applyAutoTheme();
  }
  setInterval(applyAutoTheme, 60000);
  window.addEventListener('message', e => {
    const d = e.data;
    if (e.source === frame.contentWindow) {
      if (e.origin === HOME && d?.ts === 'home' && d.ready) sendTheme(frame.contentWindow, HOME, 'home');
      return;
    }
    const bwin = brightFrame?.contentWindow;
    if (bwin && e.source === bwin) {
      if (e.origin !== MON) return;
      if (d?.thinksmart === 'displayReady') sendTheme(bwin, MON, 'display');
      else if (d?.thinksmart === 'setTheme') {
        if (d.theme === 'auto') { setThemeMode('auto'); applyAutoTheme(); broadcastTheme(); }
        else if (d.theme === 'light' || d.theme === 'dark') { setThemeMode('manual'); setUiTheme(d.theme); }
      }
      return;
    }
    for (const v of subEntries.values()) {
      if (e.source === v.frame.contentWindow && e.origin === v.origin) {
        if (d?.ts === 'sub' && d.cmd === 'ready') sendTheme(v.frame.contentWindow, v.origin, 'sub');
        return;
      }
    }
  });

  $('tsSubBack').onclick = () => closeSub(true);
  window.__tsOpenSub = openSub;
  window.__tsCloseSub = closeSub;

  let bootRetryTimer = 0, bootDeadline = 0, bootCycles = 0;
  frame.addEventListener('load', () => {
    clearTimeout(bootRetryTimer);
    if (!homeOk && Date.now() < bootDeadline) {
      bootRetryTimer = setTimeout(() => { if (!homeOk && Date.now() < bootDeadline) { lastHomeVis = null; frame.src = HOME + '/?t=' + Date.now(); } }, 650);
    }
  });
  function loadHome() {
    homeOk = false;
    lastHomeVis = null;
    bootCycles++;
    bootDeadline = Date.now() + 14000;
    root.classList.toggle('ts-homeload', view === 'home');   // dark cover while loading: no flash of XiaoZhi
    apply();
    frame.src = HOME + '/?t=' + Date.now();
    clearTimeout(readyTimer);
    clearTimeout(bootRetryTimer);
    readyTimer = setTimeout(() => {
      if (homeOk) return;
      clearTimeout(bootRetryTimer);
      root.classList.remove('ts-homeload'); apply();   // home service down: XiaoZhi stays usable
      setTimeout(loadHome, bootCycles <= 3 ? 2500 : 15000);
    }, 14500);
  }
  window.addEventListener('message', e => {
    let matchedSub = null;
    for (const v of subEntries.values()) {
      if (e.source === v.frame.contentWindow && e.origin === v.origin) { matchedSub = v; break; }
    }
    if (matchedSub) {
      const d = e.data;
      if (d?.ts === 'sub' && d.cmd === 'reloading') {
        matchedSub.ready = false;
        matchedSub.failed = false;
        matchedSub.loading = true;
        matchedSub.startedAt = Date.now();
        subRetryCounts.delete(matchedSub.url);
        if (subOpen && activeSub === matchedSub) {
          subLayer.classList.remove('ready');
          clearTimeout(subShowTimer);
          subShowTimer = setTimeout(() => {
            if (subOpen && activeSub === matchedSub && !matchedSub.ready) {
              matchedSub.failed = true;
              matchedSub.loading = false;
              subErr.classList.add('show');
            }
          }, 8000);
        }
        return;
      }
      if (d?.ts === 'sub' && d.cmd === 'ready') {
        clearTimeout(subRetryTimers.get(matchedSub.url));
        subRetryTimers.delete(matchedSub.url);
        subRetryCounts.delete(matchedSub.url);
        matchedSub.ready = true;
        matchedSub.loading = false;
        matchedSub.failed = false;
        if (subOpen && activeSub === matchedSub) {
          clearTimeout(subShowTimer);
          subErr.classList.remove('show');
          subLayer.classList.add('ready');
          syncSubVis(matchedSub, true, true);
          if (matchedSub.pendingCmd) {
            const cmdToSend = matchedSub.pendingCmd; matchedSub.pendingCmd = '';
            try { matchedSub.frame.contentWindow?.postMessage({ ts: 'sub', cmd: cmdToSend }, matchedSub.origin); } catch (e) {}
          }
        } else {
          syncSubVis(matchedSub, false, true);
        }
        return;
      }
      if (subOpen && activeSub === matchedSub) {
        if (d?.type === 'CLOSE_QQMUSIC_OVERLAY' || (d?.ts === 'sub' && d.cmd === 'close')) { closeSub(true); return; }
        if (d?.ts === 'sub' && d.cmd === 'brightness') { openBrightness(); return; }
      }
      return;
    }
    if (e.origin !== HOME || e.source !== frame.contentWindow) return;
    const d = e.data;
    if (!d || d.ts !== 'home') return;
    if (d.ready) { homeOk = true; lastHomeVis = null; clearTimeout(readyTimer); clearTimeout(bootRetryTimer); if (!subOpen && isBusy()) { autoReturn = true; view = 'xiaozhi'; } apply(); scheduleIdlePrewarm(); return; }
    if (d.cmd === 'xiaozhi') {
      if (d.then === 'settings') {
        closeBrightness(false);
        bindSheetObs();
        $('btnSettings')?.click();
        bindSheetObs();
        if (sheetOpen()) { flushParked(); apply(); }
      } else {
        autoReturn = false;
        setView('xiaozhi');
      }
    } else if (d.cmd === 'navigate' && okSubUrl(d.url)) {
      openSub(d.url, d.subCmd);
    } else if (d.cmd === 'brightness') {
      openBrightness();
    } else if (d.cmd === 'sun') {
      setSun(d.rise, d.set);
    } else if (d.cmd === 'inboxAck' && typeof d.id === 'string') {
      dismissInboxById(d.id);
    } else if (d.cmd === 'inboxSync') {
      triggerInboxPoll();
    } else if (d.cmd === 'timerDone') {
      if (!document.body.classList.contains('ringing') && !isBusy()) {
        closeBrightness(false);
        forceCloseSheets();
        if (subOpen) closeSub(true);
        else { autoReturn = false; setView('home'); }
      }
    }
  });
  homeBtn.onclick = () => { autoReturn = false; setView('home'); };
  const observedSheets = new WeakSet();
  const sheetObs = new MutationObserver(() => {
    if (!sheetOpen() && !subOpen && isBusy() && view === 'home' && homeOk) {
      autoReturn = true;
      closeBrightness(false);
      setView('xiaozhi');
      return;
    }
    apply();
  });
  const bindSheetObs = () => {
    for (const el of [getSettings(), getWifi()]) {
      if (el && !observedSheets.has(el)) {
        observedSheets.add(el);
        sheetObs.observe(el, { attributes: true, attributeFilter: ['class'] });
      }
    }
  };
  bindSheetObs();

  // wake word / alarm -> XiaoZhi; quiet again -> back home
  let prevBodyState = document.body.dataset.state || 'idle';
  new MutationObserver(() => {
    const curState = document.body.dataset.state || 'idle';
    if (curState !== prevBodyState) {
      prevBodyState = curState;
      if (curState === 'speaking' || curState === 'idle' || curState === 'thinking') scheduleVoiceSrvCheck();
    }
    if (!isBusy()) return;
    lastBusy = Date.now();
    stopOverlayClip();
    try { frame.contentWindow?.postMessage({ ts: 'home', stopClip: true }, HOME); } catch (e) {}
    if (document.body.classList.contains('ringing')) {
      closeSub(false);
      closeBrightness(false);
      forceCloseSheets();
      autoReturn = true;
      setView('xiaozhi');
      return;
    }
    if (!subOpen && !sheetOpen() && view === 'home' && homeOk) {
      autoReturn = true;
      closeBrightness(false);
      setView('xiaozhi');
    }
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  for (const ev of ['pointerdown', 'touchstart', 'keydown']) {
    addEventListener(ev, e => {
      if (card && card.contains(e.target)) return;
      lastTouch = Date.now();
      if (view === 'xiaozhi') autoReturn = false;
    }, { capture: true, passive: true });
  }
  function syncXzQuietTimer() {
    const want = !subOpen && view === 'xiaozhi' && homeOk && !document.hidden;
    if (want) {
      if (!xzQuietTimer) xzQuietTimer = setInterval(() => {
        if (subOpen || view !== 'xiaozhi' || !homeOk || document.hidden) { syncXzQuietTimer(); return; }
        if (isBusy() || brightnessOpen() || sheetOpen()) { lastBusy = Date.now(); return; }
        const quiet = Date.now() - Math.max(lastTouch, lastBusy);
        if (quiet >= (autoReturn ? 30000 : 180000)) { autoReturn = false; setView('home'); }
      }, 5000);
    } else if (xzQuietTimer) {
      clearInterval(xzQuietTimer); xzQuietTimer = 0;
    }
  }
  document.addEventListener('visibilitychange', syncXzQuietTimer);

  // XiaoZhi idle stillness (kept from the previous overlay): after 60 s untouched while idle,
  // detach XiaoZhi's CSS animations; any touch or state change resumes them at once.
  const still = document.createElement('style');
  still.textContent = 'html.ts-still .app *,html.ts-still .app *::before,html.ts-still .app *::after{animation:none!important;transition:none!important;will-change:auto!important}';
  document.head.appendChild(still);
  let stillTimer = 0;
  const idle = () => (document.body.dataset.state || 'idle') === 'idle' && !document.body.classList.contains('ringing');
  const arm = () => {
    if (document.body.classList.contains('ringing')) closeBrightness(false);
    if (root.classList.contains('ts-xz-covered')) {
      if (stillTimer) { clearTimeout(stillTimer); stillTimer = 0; }
      return;
    }
    clearTimeout(stillTimer); stillTimer = 0;
    if (root.classList.contains('ts-still')) root.classList.remove('ts-still');
    if (idle()) stillTimer = setTimeout(() => { stillTimer = 0; if (idle() && !root.classList.contains('ts-xz-covered')) root.classList.add('ts-still'); }, 60000);
  };
  armXzStill = arm;
  for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart']) addEventListener(ev, arm, { capture: true, passive: true });
  new MutationObserver(arm).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });
  arm();

  if (isBusy()) { autoReturn = true; view = 'xiaozhi'; }
  loadHome();
})();
