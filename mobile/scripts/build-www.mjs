// Assemble mobile/www from the shared web remote:
//   frontend/remote.html  -> www/index.html  (+ the app's connect screen)
//   frontend/remote.js    -> www/remote.js   (compiled by tools/build.py)
//   frontend/vendor/react*.min.js -> www/vendor/
//   mobile/src/app.js     -> www/app.js
// Run: npm run www   (or npm run sync / apk:debug, which call it)
import { copyFileSync, mkdirSync, readFileSync, writeFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const mobile = join(here, '..');
const front = join(mobile, '..', 'frontend');
const www = join(mobile, 'www');
mkdirSync(join(www, 'vendor'), { recursive: true });

const remoteJs = join(front, 'remote.js');
const remoteSrc = join(front, 'remote.src.js');
if (!existsSync(remoteJs) || statSync(remoteSrc).mtimeMs > statSync(remoteJs).mtimeMs) {
  console.error('frontend/remote.js is missing or older than remote.src.js — run: python3 tools/build.py');
  process.exit(1);
}
copyFileSync(remoteJs, join(www, 'remote.js'));
for (const f of ['react.min.js', 'react-dom.min.js']) copyFileSync(join(front, 'vendor', f), join(www, 'vendor', f));
copyFileSync(join(mobile, 'src', 'app.js'), join(www, 'app.js'));

// index.html = the web remote page, with the connect screen added and
// remote.js loaded by app.js (after the PC is chosen) instead of directly
let html = readFileSync(join(front, 'remote.html'), 'utf8');
html = html.replace('<script src="remote.js"></script>', '<script src="app.js"></script>');
html = html.replace('<div id="root"></div>', '<div id="connect" style="display:none"></div>\n<div id="root" style="display:none"></div>');
html = html.replace('</style>', `
  /* connect screen (app only) */
  #connect { max-width: 560px; margin: 0 auto; padding: 18px 16px 40px; }
  #connect .brand { font-weight: 800; font-size: 18px; margin-bottom: 18px; }
  #connect .brand b { color: var(--accent); }
  #connect h1 { font-size: 24px; margin: 0 0 6px; }
  #connect .sub { color: var(--muted); font-size: 14px; line-height: 1.45; margin: 0 0 18px; }
  #connect .big { width: 100%; padding: 18px; font-size: 17px; font-weight: 700; }
  #connect .hint { color: var(--muted); font-size: 13px; margin: 6px 2px; }
  #connect .msg { min-height: 20px; margin: 10px 2px; font-size: 14px; color: var(--accent2); line-height: 1.4; }
  #connect .msg.err { color: var(--danger); }
  #connect .item { width: 100%; text-align: left; display: flex; flex-direction: column; gap: 3px; margin-bottom: 6px; min-height: 56px; }
  #connect .item small { color: var(--muted); font-family: var(--mono); font-size: 12px; }
  #connect .row { display: flex; gap: 6px; align-items: stretch; }
  #connect .row .item { flex: 1; }
  #connect .ghost { background: transparent; }
  #connect input { width: 100%; background: var(--panel2); color: var(--text); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px; font-size: 16px; margin-bottom: 8px; font-family: var(--mono); outline: none; }
  #connect #go { width: 100%; min-height: 50px; }
</style>`);
writeFileSync(join(www, 'index.html'), html);
console.log('www/ ready:', ['index.html', 'app.js', 'remote.js', 'vendor/react.min.js', 'vendor/react-dom.min.js'].join(', '));
