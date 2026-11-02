// musloop Android app — connect screen + glue to the shared remote UI.
// Plain ES2017, no build step needed for this file. Capacitor plugins are
// reached through window.Capacitor (injected by the native bridge).
(function () {
  const Cap = window.Capacitor;
  const P = (Cap && Cap.Plugins) || {};
  const Prefs = P.Preferences, Haptics = P.Haptics, KeepAwake = P.KeepAwake,
        QrScan = P.QrScan, Discovery = P.Discovery;

  const L = {
    en: { title: 'Connect to musloop', sub: 'Control Live mode on your computer. Phone and computer must be on the same Wi-Fi.',
          scan: 'Scan QR code', scanHint: 'On the computer: Live mode → 📱 Phone',
          found: 'Found on this Wi-Fi', searching: 'Searching the Wi-Fi…', none: 'No musloop found on this Wi-Fi.',
          retry: 'Search again', manual: 'Or type the address', connect: 'Connect', saved: 'Recent',
          needKey: 'Found it — now scan the QR code once to pair (Live mode → 📱 Phone).',
          badUrl: 'Not a musloop QR code.', unreachable: 'Can\u2019t reach {host}. Same Wi-Fi? Is musloop running? Firewall?',
          noScanner: 'The QR scanner needs Google Play services. Type the address instead.', installing: 'Google is installing the QR scanner — try again in a minute.',
          forget: 'Forget', keyLabel: 'Code (from the QR link, after #k=)' },
    ru: { title: 'Подключение к musloop', sub: 'Управляйте Live-режимом на компьютере. Телефон и компьютер должны быть в одной Wi-Fi-сети.',
          scan: 'Сканировать QR-код', scanHint: 'На компьютере: Live-режим → 📱 Телефон',
          found: 'Найдено в этой Wi-Fi-сети', searching: 'Ищем в Wi-Fi…', none: 'В этой сети musloop не найден.',
          retry: 'Искать снова', manual: 'Или введите адрес', connect: 'Подключить', saved: 'Недавние',
          needKey: 'Нашли — теперь один раз отсканируйте QR-код для подключения (Live-режим → 📱 Телефон).',
          badUrl: 'Это не QR-код musloop.', unreachable: 'Не удаётся связаться с {host}. Та же Wi-Fi? musloop запущен? Файрвол?',
          noScanner: 'Для сканера QR нужны сервисы Google Play. Введите адрес вручную.', installing: 'Google устанавливает сканер QR — попробуйте через минуту.',
          forget: 'Забыть', keyLabel: 'Код (из ссылки QR, после #k=)' },
    zh: { title: '连接 musloop', sub: '在手机上控制电脑的现场模式。手机和电脑需在同一 Wi-Fi 网络。',
          scan: '扫描二维码', scanHint: '在电脑上：现场模式 → 📱 手机',
          found: '在此 Wi-Fi 中找到', searching: '正在搜索 Wi-Fi…', none: '此网络中未找到 musloop。',
          retry: '重新搜索', manual: '或输入地址', connect: '连接', saved: '最近',
          needKey: '已找到——请扫描一次二维码完成配对（现场模式 → 📱 手机）。',
          badUrl: '这不是 musloop 的二维码。', unreachable: '无法连接 {host}。同一 Wi-Fi？musloop 是否运行？防火墙？',
          noScanner: '二维码扫描需要 Google Play 服务。请手动输入地址。', installing: 'Google 正在安装二维码扫描器——请一分钟后重试。',
          forget: '忘记', keyLabel: '代码（二维码链接中 #k= 之后）' },
  };
  const lang = (navigator.language || 'en').slice(0, 2);
  const t = L[lang] || L.en;

  const store = {
    async get(k) { try { return Prefs ? (await Prefs.get({ key: k })).value : localStorage.getItem(k); } catch (e) { return null; } },
    async set(k, v) { try { Prefs ? await Prefs.set({ key: k, value: v }) : localStorage.setItem(k, v); } catch (e) {} },
    async del(k) { try { Prefs ? await Prefs.remove({ key: k }) : localStorage.removeItem(k); } catch (e) {} },
  };

  // "http://192.168.1.20:8801/remote#k=abc" -> {server, key}
  function parseRemoteUrl(text) {
    try {
      const u = new URL(String(text).trim());
      const m = /(?:^|&)k=([^&]+)/.exec(u.hash.replace(/^#/, ''));
      if (!/^https?:$/.test(u.protocol) || u.pathname !== '/remote' || !m) return null;
      return { server: `${u.protocol}//${u.host}`, key: decodeURIComponent(m[1]) };
    } catch (e) { return null; }
  }
  function normServer(s) {
    s = String(s || '').trim().replace(/\/+$/, '');
    if (!s) return null;
    if (!/^https?:\/\//.test(s)) s = 'http://' + s;
    try { const u = new URL(s); return `${u.protocol}//${u.hostname}:${u.port || 8801}`; } catch (e) { return null; }
  }

  // Native HTTP (Discovery.ping): browsers now block page-to-LAN fetch() ("Local
  // Network Access"); the WebSocket of the remote itself is not affected.
  async function ping(server, key) {
    try {
      let status, body;
      if (Discovery && Discovery.ping) {
        ({ status, body } = await Discovery.ping({ server, key }));
      } else {   // plain browser (development): fetch
        const r = await fetch(server + '/api/remote/ping', { headers: { 'X-Musloop-Key': key } });
        status = r.status; body = await r.text();
      }
      if (status === 403) return { ok: false, why: 'key' };
      if (status !== 200) return { ok: false, why: 'net' };
      const j = JSON.parse(body || '{}');
      return j && j.app === 'musloop' ? { ok: true, name: j.name } : { ok: false, why: 'net' };
    } catch (e) { return { ok: false, why: 'net' }; }
  }

  // ---------- connect screen ----------
  const $ = sel => document.querySelector(sel);
  function show(html) { $('#connect').innerHTML = html; $('#connect').style.display = 'block'; $('#root').style.display = 'none'; }
  function msg(text, err) { const m = $('#msg'); if (m) { m.textContent = text || ''; m.className = 'msg' + (err ? ' err' : ''); } }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  async function renderConnect(note) {
    const recent = JSON.parse((await store.get('recent')) || '[]');
    show(`
      <div class="brand">musloop<b>_</b></div>
      <h1>${t.title}</h1>
      <p class="sub">${t.sub}</p>
      <button class="primary big" id="scan">📷 ${t.scan}</button>
      <div class="hint">${t.scanHint}</div>
      <div id="msg" class="msg"></div>
      <h3>${t.found}</h3>
      <div id="found"><div class="hint">${t.searching}</div></div>
      ${recent.length ? `<h3>${t.saved}</h3><div>${recent.map((r, i) => `
        <div class="row"><button class="item" data-recent="${i}">${esc(r.name || r.server)}<small>${esc(r.server)}</small></button>
        <button class="ghost" data-forget="${i}">${t.forget}</button></div>`).join('')}</div>` : ''}
      <h3>${t.manual}</h3>
      <input id="addr" placeholder="192.168.1.20:8801" inputmode="url" autocapitalize="off" autocorrect="off">
      <input id="key" placeholder="${t.keyLabel}" autocapitalize="off" autocorrect="off">
      <button id="go">${t.connect}</button>`);
    if (note) msg(note.text, note.err);
    $('#scan').onclick = scanQr;
    $('#go').onclick = async () => {
      const pasted = parseRemoteUrl($('#addr').value);
      if (pasted) return tryConnect(pasted.server, pasted.key);
      const server = normServer($('#addr').value), key = $('#key').value.trim();
      if (!server) return msg(t.badUrl, true);
      if (!key) return msg(t.needKey, true);
      tryConnect(server, key);
    };
    document.querySelectorAll('[data-recent]').forEach(b => b.onclick = () => {
      const r = recent[+b.dataset.recent]; tryConnect(r.server, r.key);
    });
    document.querySelectorAll('[data-forget]').forEach(b => b.onclick = async () => {
      recent.splice(+b.dataset.forget, 1); await store.set('recent', JSON.stringify(recent)); renderConnect();
    });
    discover(recent);
  }

  async function discover(recent) {
    const box = $('#found');
    if (!Discovery) { box.innerHTML = `<div class="hint">${t.none}</div>`; return; }
    box.innerHTML = `<div class="hint">${t.searching}</div>`;
    let servers = [];
    try { servers = (await Discovery.find({ timeoutMs: 1500 })).servers || []; } catch (e) {}
    if (!$('#found')) return;     // left the screen
    if (!servers.length) {
      box.innerHTML = `<div class="hint">${t.none}</div><button class="ghost" id="again">↻ ${t.retry}</button>`;
      $('#again').onclick = () => discover(recent);
      return;
    }
    box.innerHTML = servers.map((s, i) => `<button class="item" data-found="${i}">🖥 ${esc(s.name)}<small>${esc(s.url)}</small></button>`).join('');
    document.querySelectorAll('[data-found]').forEach(b => b.onclick = () => {
      const s = servers[+b.dataset.found];
      const known = recent.find(r => r.server === s.url);
      if (known) return tryConnect(known.server, known.key);
      $('#addr').value = s.url.replace(/^http:\/\//, '');
      msg(t.needKey, false);
    });
  }

  async function scanQr() {
    if (!QrScan) return msg(t.noScanner, true);
    try {
      const { text } = await QrScan.scan();
      if (!text) return;
      const p = parseRemoteUrl(text);
      if (!p) return msg(t.badUrl, true);
      tryConnect(p.server, p.key);
    } catch (e) {
      const code = e && e.code;
      if (code === 'CANCELED') return;
      // first use: Google downloads the scanner module (code 200 = not yet available)
      msg(code === 'MLKIT_200' ? t.installing : t.noScanner, true);
    }
  }

  async function tryConnect(server, key) {
    msg('…', false);
    const r = await ping(server, key);
    if (!r.ok) {
      if (r.why === 'key') return renderConnect({ text: t.needKey, err: true });
      return msg(t.unreachable.replace('{host}', server.replace(/^https?:\/\//, '')), true);
    }
    const recent = JSON.parse((await store.get('recent')) || '[]').filter(x => x.server !== server);
    recent.unshift({ server, key, name: r.name });
    await store.set('recent', JSON.stringify(recent.slice(0, 5)));
    await store.set('last', JSON.stringify({ server, key }));
    startRemote(server, key);
  }

  // ---------- the shared remote UI (frontend/remote.src.js -> remote.js) ----------
  function startRemote(server, key) {
    window.MUSLOOP_NATIVE = {
      server, key,
      vibrate(ms) {
        if (!Haptics) return;
        if (Array.isArray(ms)) Haptics.notification({ type: 'ERROR' }).catch(() => {});
        else Haptics.impact({ style: ms > 20 ? 'MEDIUM' : 'LIGHT' }).catch(() => {});
      },
      keepAwake(on) { if (KeepAwake) (on ? KeepAwake.keepAwake() : KeepAwake.allowSleep()).catch(() => {}); },
      onBadKey: async () => { await store.del('last'); location.reload(); },
      onDisconnectRequest: async () => { await store.del('last'); location.reload(); },
    };
    $('#connect').style.display = 'none';
    $('#root').style.display = 'block';
    const s = document.createElement('script');
    s.src = 'remote.js';
    document.body.appendChild(s);
  }

  // ---------- start ----------
  (async () => {
    const last = JSON.parse((await store.get('last')) || 'null');
    if (last && last.server && last.key) {
      const r = await ping(last.server, last.key);
      if (r.ok) return startRemote(last.server, last.key);
      if (r.why === 'key') { await store.del('last'); return renderConnect({ text: t.needKey, err: true }); }
      // PC offline right now: open the remote anyway — it shows "PC not
      // responding" and reconnects by itself when musloop comes back
      return startRemote(last.server, last.key);
    }
    renderConnect();
  })();
})();
