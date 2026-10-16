// musloop phone remote. Source: frontend/remote.src.js (JSX), compiled by
// tools/build.py to frontend/remote.js. Audio plays on the PC; this page only
// sends commands and shows the PC's state, over /ws/remote.
const { useState, useEffect, useRef, useCallback } = React;

// Platform hooks. The web page (served by musloop on the PC) uses the defaults.
// The Android app (mobile/) sets window.MUSLOOP_NATIVE before this script:
//   { server: 'http://192.168.1.20:8801', key: '...', vibrate(ms), keepAwake(on),
//     onBadKey(), onDisconnectRequest() }
const NATIVE = window.MUSLOOP_NATIVE || null;

const STR = {
  en: {
    badKeyApp: 'This PC changed its code. Scan the new QR code (Live mode → 📱 Phone).', rescan: 'Scan again', changePc: 'Another PC',
    connecting: 'Connecting…', online: 'PC online', offline: 'PC not responding', lost: 'Connection lost — reconnecting…',
    noKey: 'Open this page by scanning the QR code in musloop (Live mode → 📱 Phone).',
    badKey: 'This QR code is no longer valid. Scan the new code in musloop (Live mode → 📱 Phone).',
    pcOff: 'The musloop tab on the PC is closed or asleep. Open musloop on the PC — the remote reconnects by itself.',
    noTrack: 'No track open on the PC yet. Pick one below or open it on the PC.',
    now: 'Now', ready: 'Ready', next: 'Next up', pass: 'pass', inS: 'in {n} s', atBoundary: 'at the loop boundary', pick: 'pick a part or a scene',
    play: '▶ Play', stop: '■ Stop', nextBtn: 'Next →', cancel: '✕ Cancel', fade: '◢ Fade',
    modes: { natural: 'loop end', soon: 'phrase', now: 'right away' },
    parts: 'Parts', scenes: 'Scenes', otherScenes: 'Scenes of other tracks', tracks: 'Tracks', loading: 'Loading:',
    oneShot: 'plays once', close: 'Close',
    err: { not_playing: 'Press Play first', host_offline: 'The PC is not responding', no_track: 'Open a track first' },
  },
  ru: {
    badKeyApp: 'На этом ПК сменили код. Отсканируйте новый QR-код (Live-режим → 📱 Телефон).', rescan: 'Сканировать снова', changePc: 'Другой ПК',
    connecting: 'Подключение…', online: 'ПК на связи', offline: 'ПК не отвечает', lost: 'Связь потеряна — переподключаемся…',
    noKey: 'Откройте эту страницу через QR-код в musloop (Live-режим → 📱 Телефон).',
    badKey: 'Этот QR-код больше не действует. Отсканируйте новый в musloop (Live-режим → 📱 Телефон).',
    pcOff: 'Вкладка musloop на ПК закрыта или компьютер уснул. Откройте musloop на ПК — пульт переподключится сам.',
    noTrack: 'На ПК ещё не открыт трек. Выберите его ниже или откройте на ПК.',
    now: 'Сейчас', ready: 'Готово', next: 'Дальше', pass: 'проход', inS: 'через {n} с', atBoundary: 'на границе петли', pick: 'выберите часть или сцену',
    play: '▶ Играть', stop: '■ Стоп', nextBtn: 'Дальше →', cancel: '✕ Отмена', fade: '◢ Затухание',
    modes: { natural: 'конец петли', soon: 'по фразе', now: 'сразу' },
    parts: 'Части', scenes: 'Сцены', otherScenes: 'Сцены других треков', tracks: 'Треки', loading: 'Загрузка:',
    oneShot: 'играет один раз', close: 'Закрыть',
    err: { not_playing: 'Сначала нажмите «Играть»', host_offline: 'ПК не отвечает', no_track: 'Сначала откройте трек' },
  },
  zh: {
    badKeyApp: '这台电脑更换了代码。请扫描新的二维码（现场模式 → 📱 手机）。', rescan: '重新扫描', changePc: '其他电脑',
    connecting: '正在连接…', online: '电脑在线', offline: '电脑无响应', lost: '连接中断——正在重连…',
    noKey: '请扫描 musloop 中的二维码打开此页面（现场模式 → 📱 手机）。',
    badKey: '此二维码已失效。请扫描 musloop 中的新二维码（现场模式 → 📱 手机）。',
    pcOff: '电脑上的 musloop 标签页已关闭或电脑已休眠。在电脑上打开 musloop——遥控会自动重连。',
    noTrack: '电脑上还没有打开音轨。请在下方选择或在电脑上打开。',
    now: '正在播放', ready: '就绪', next: '下一段', pass: '第', inS: '{n} 秒后', atBoundary: '在循环边界', pick: '选择段落或场景',
    play: '▶ 播放', stop: '■ 停止', nextBtn: '下一段 →', cancel: '✕ 取消', fade: '◢ 淡出',
    modes: { natural: '循环结束', soon: '乐句', now: '立即' },
    parts: '段落', scenes: '场景', otherScenes: '其他音轨的场景', tracks: '音轨', loading: '加载中：',
    oneShot: '只播放一次', close: '关闭',
    err: { not_playing: '请先点击播放', host_offline: '电脑无响应', no_track: '请先打开音轨' },
  },
};
const LANG = (() => {
  const saved = localStorage.getItem('musloop_lang');
  if (saved && STR[saved]) return saved;
  const n = (navigator.language || 'en').slice(0, 2);
  return STR[n] ? n : 'en';
})();
// Language: the phone's own (the web page shares localStorage with the PC's
// musloop page, so a choice made on the PC also counts). In the Android app
// the phone's system language is used — it is the GM's phone.
let t = STR[LANG];
document.documentElement.lang = LANG;

// key: from the URL fragment (#k=...) — never sent to the server in the URL,
// remembered so the page keeps working after the phone reloads it
const KEY = NATIVE ? NATIVE.key : (() => {
  const m = /[#&]k=([^&]+)/.exec(location.hash);
  if (m) { try { localStorage.setItem('musloop_remote_key', m[1]); } catch (e) {} return m[1]; }
  try { return localStorage.getItem('musloop_remote_key'); } catch (e) { return null; }
})();

const buzz = ms => {
  try { if (NATIVE && NATIVE.vibrate) NATIVE.vibrate(ms); else if (navigator.vibrate) navigator.vibrate(ms); } catch (e) {}
};
// ws://host:port of the PC: the page's own origin on the web, the saved PC in the app
const WS_BASE = NATIVE ? NATIVE.server.replace(/^http/, 'ws') : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`;

// keep the screen on: Wake Lock where allowed (secure contexts only), otherwise
// a muted looping video (the trick that works on plain http on iOS / Android)
function useKeepAwake(enabled) {
  useEffect(() => {
    if (!enabled) return;
    if (NATIVE && NATIVE.keepAwake) {           // the app: a real FLAG_KEEP_SCREEN_ON
      NATIVE.keepAwake(true);
      return () => NATIVE.keepAwake(false);
    }
    let lock = null, video = null, stop = false;
    const viaApi = async () => {
      try { if ('wakeLock' in navigator) { lock = await navigator.wakeLock.request('screen'); return true; } } catch (e) {}
      return false;
    };
    const viaVideo = () => {
      if (video) { video.play().catch(() => {}); return; }
      video = document.createElement('video');
      video.setAttribute('playsinline', ''); video.setAttribute('muted', ''); video.muted = true; video.loop = true;
      video.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;bottom:0;left:0';
      // 1-second blank video (webm for Chrome/Android, mp4 for Safari/iOS)
      const s1 = document.createElement('source'); s1.src = 'keepawake.webm'; s1.type = 'video/webm';
      const s2 = document.createElement('source'); s2.src = 'keepawake.mp4'; s2.type = 'video/mp4';
      video.appendChild(s1); video.appendChild(s2);
      document.body.appendChild(video);
      video.play().catch(() => {});
    };
    // both need a user gesture on some browsers: try now, retry on the first tap
    const arm = async () => { if (stop) return; if (!(await viaApi())) viaVideo(); };
    arm();
    const onTap = () => arm();
    const onVis = () => { if (document.visibilityState === 'visible') arm(); };
    document.addEventListener('pointerdown', onTap, { once: true });
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop = true;
      document.removeEventListener('visibilitychange', onVis);
      try { lock && lock.release(); } catch (e) {}
      if (video) { video.pause(); video.remove(); }
    };
  }, [enabled]);
}

function Remote() {
  const [conn, setConn] = useState(KEY ? 'connecting' : 'nokey');   // connecting|ok|lost|badkey|nokey
  const [hostOnline, setHostOnline] = useState(null);
  const [st, setSt] = useState(null);
  const [toast, setToast] = useState(null);
  const [sheet, setSheet] = useState(false);
  const [vol, setVol] = useState(null);                 // local slider value while dragging
  const wsRef = useRef(null);
  const seqRef = useRef(0);
  const volT = useRef(null);
  useKeepAwake(conn === 'ok');

  const showToast = useCallback(msg => {
    setToast(msg); clearTimeout(showToast._t);
    showToast._t = setTimeout(() => setToast(null), 2500);
  }, []);

  useEffect(() => {
    if (!KEY) return;
    let ws = null, stop = false, retry = null, delay = 800;
    const connect = () => {
      if (stop) return;
      ws = new WebSocket(`${WS_BASE}/ws/remote`);
      wsRef.current = ws;
      ws.onopen = () => { delay = 800; ws.send(JSON.stringify({ type: 'hello', key: KEY, ua: navigator.userAgent })); };
      ws.onmessage = ev => {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.type === 'welcome') { setConn('ok'); setHostOnline(!!m.host); }
        else if (m.type === 'presence') setHostOnline(!!m.host);
        else if (m.type === 'state') setSt(m.state);
        else if (m.type === 'error' && m.error === 'bad_key') { stop = true; setConn('badkey'); }
        else if (m.type === 'ack' && !m.ok) {
          buzz([40, 60, 40]);
          showToast((t.err && t.err[m.error]) || m.error || '?');
        }
      };
      ws.onclose = ev => {
        if (wsRef.current === ws) wsRef.current = null;
        if (ev.code === 4401) { stop = true; setConn('badkey'); return; }
        if (stop) return;
        setConn('lost');
        retry = setTimeout(connect, delay); delay = Math.min(delay * 1.7, 8000);
      };
    };
    connect();
    // phones freeze background tabs: reconnect at once when the page comes back
    const onVis = () => { if (document.visibilityState === 'visible' && (!wsRef.current || wsRef.current.readyState > 1)) { clearTimeout(retry); delay = 800; connect(); } };
    document.addEventListener('visibilitychange', onVis);
    return () => { stop = true; clearTimeout(retry); document.removeEventListener('visibilitychange', onVis); try { ws && ws.close(); } catch (e) {} };
  }, []);

  const send = (cmd, args = {}) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) { showToast(t.lost); return; }
    buzz(15);
    ws.send(JSON.stringify({ type: 'cmd', seq: ++seqRef.current, cmd, args }));
  };

  if (conn === 'nokey' || conn === 'badkey')
    return <div className="center">
      <div>{conn === 'nokey' ? t.noKey : (NATIVE ? t.badKeyApp : t.badKey)}</div>
      {NATIVE && <button style={{ marginTop: 18 }} onClick={() => NATIVE.onBadKey && NATIVE.onBadKey()}>{t.rescan}</button>}
    </div>;

  const playing = st && st.playing;
  const parts = (st && st.parts) || [];
  const cur = st && parts[st.index];
  const nxt = st && st.next != null ? parts[st.next] : null;
  const curVol = vol != null ? vol : (st ? st.volume : 1);
  const tracks = (st && st.tracks) || [];
  const curTrack = st && st.trackIndex >= 0 ? tracks[st.trackIndex] : null;

  return (
    <div>
      <div className="top">
        <div className="logo">musloop<b>_</b></div>
        {NATIVE && <button className="chg" onClick={() => NATIVE.onDisconnectRequest && NATIVE.onDisconnectRequest()}
          title={t.changePc}>⇄</button>}
        <div className="conn">
          <span className={'dot' + (conn === 'ok' && hostOnline ? ' ok' : conn === 'ok' ? ' warn' : '')}></span>
          {conn === 'ok' ? (hostOnline ? t.online : t.offline) : conn === 'lost' ? t.lost : t.connecting}
        </div>
      </div>
      {conn === 'ok' && hostOnline === false && <div className="banner">{t.pcOff}</div>}
      {conn === 'lost' && <div className="banner">{t.lost}</div>}

      {st && <React.Fragment>
        <div className="track">
          <button className="step" disabled={!tracks.length || st.trackIndex <= 0 && st.trackIndex !== -1 || !!st.switching}
            onClick={() => send('trackStep', { dir: -1 })}>◀</button>
          <button className="name" onClick={() => setSheet(true)}>
            {st.switching ? `${t.loading} ${st.switching}` : (st.track || '—')}
            {curTrack && tracks.length > 1 && <small>{st.trackIndex + 1}/{tracks.length}</small>} ▾
          </button>
          <button className="step" disabled={!tracks.length || st.trackIndex >= tracks.length - 1 || !!st.switching}
            onClick={() => send('trackStep', { dir: 1 })}>▶</button>
        </div>

        {!st.loaded ? <div className="banner" style={{ borderColor: 'var(--border)', color: 'var(--muted)' }}>{t.noTrack}</div> :
        <React.Fragment>
          <div className="stage">
            <div className="card now">
              <div className="label">{playing ? t.now : t.ready}</div>
              <div className="big">{cur ? cur.name : '—'}</div>
              <div className="sub">{playing ? `${t.pass} ${st.pass}` : ''}</div>
            </div>
            <div className={'card' + (nxt ? ' armed' : '')}>
              <div className="label">{t.next}</div>
              <div className="big">{nxt ? nxt.name : '—'}</div>
              <div className="sub">{nxt ? (st.eta != null ? t.inS.replace('{n}', st.eta) : t.atBoundary) : t.pick}</div>
            </div>
          </div>

          <div className="transport">
            {playing ? <button onClick={() => send('fade')}>{t.fade}</button>
                     : <button className="primary" onClick={() => send('play')}>{t.play}</button>}
            <button className={'go' + (st.armed ? ' armed' : '')} disabled={!playing || st.index >= parts.length - 1}
              onClick={() => send('next')}>{t.nextBtn}</button>
            <button disabled={!playing || st.next == null} onClick={() => send('cancel')}>{t.cancel}</button>
          </div>

          <div className="modes">
            {['natural', 'soon', 'now'].map(m => <button key={m} className={st.mode === m ? 'on' : ''}
              onClick={() => send('mode', { mode: m })}>{t.modes[m]}</button>)}
          </div>

          {st.scenes && st.scenes.length > 0 && <React.Fragment>
            <h3>{t.scenes}</h3>
            <div className="pads">
              {st.scenes.map(sc => {
                const p = parts[sc.part];
                return <button key={sc.id} className={'pad scene' + (playing && st.index === sc.part ? ' on' : '') + (st.next === sc.part ? ' queued' : '')}
                  style={{ '--c': p ? p.color : undefined }} onClick={() => send('scene', { id: sc.id })}>
                  <span className="pn">{sc.name}</span>
                  <span className="ps">{p ? p.name : ''}</span>
                </button>;
              })}
            </div>
          </React.Fragment>}

          <h3>{t.parts}</h3>
          <div className="pads">
            {parts.map(p => <button key={p.i} className={'pad' + (playing && st.index === p.i ? ' on' : '') + (st.next === p.i ? ' queued' : '')}
              style={{ '--c': p.color }} onClick={() => send('cue', { index: p.i })}>
              <span className="pn">{p.name}</span>
              <span className="ps">{p.loop ? `#${p.i + 1}` : t.oneShot}</span>
            </button>)}
          </div>

          {st.otherScenes && st.otherScenes.length > 0 && <React.Fragment>
            <h3>{t.otherScenes}</h3>
            <div className="pads">
              {st.otherScenes.map(sc => <button key={sc.id} className="pad scene" onClick={() => send('scene', { id: sc.id })}>
                <span className="pn">{sc.name}</span><span className="ps">{sc.track}</span>
              </button>)}
            </div>
          </React.Fragment>}
        </React.Fragment>}

        <div className="vol">
          <span style={{ width: 'auto' }}>🔊</span>
          <input type="range" min="0" max="1" step="0.01" value={curVol}
            onChange={e => {
              const v = +e.target.value; setVol(v);
              clearTimeout(volT.current);
              volT.current = setTimeout(() => { send('volume', { value: v }); setTimeout(() => setVol(null), 600); }, 120);
            }} />
          <span>{Math.round(curVol * 100)}%</span>
        </div>
      </React.Fragment>}

      {!st && conn === 'ok' && <div className="center">{t.connecting}</div>}

      {sheet && st && <div className="sheet" onClick={() => setSheet(false)}>
        <div onClick={e => e.stopPropagation()}>
          <div className="label" style={{ margin: '0 4px 10px' }}>{t.tracks}</div>
          {(() => {
            let last = null;
            return tracks.map(x => {
              const head = x.group && x.group !== last ? <div key={'g' + x.i} className="grp">{x.group}</div> : null;
              last = x.group;
              return <React.Fragment key={x.key}>
                {head}
                <button className={'item' + (x.i === st.trackIndex ? ' on' : '')}
                  onClick={() => { send('track', { index: x.i }); setSheet(false); }}>
                  <span className="n">{x.name}</span>
                  {x.sub && <span className="ps" style={{ fontSize: 12, color: 'var(--muted)' }}>{x.sub}</span>}
                </button>
              </React.Fragment>;
            });
          })()}
          <button style={{ width: '100%', marginTop: 6 }} onClick={() => setSheet(false)}>{t.close}</button>
        </div>
      </div>}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<Remote />);
