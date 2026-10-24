import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const workspace = path.resolve(here, '..');
const publicDir = path.join(workspace, 'public', 'console');
const diaryDir = path.join(workspace, 'state', 'amo-memory', 'diary');
const profileFile = path.join(workspace, 'state', 'amo-memory', 'profile.md');
const plannerFile = path.join(process.env.USERPROFILE || '', '.dsh', 'planner', 'items.json');
const port = Number(process.env.AMO_MEMORY_PORT || 8899);

const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };

function json(res, status, value) {
  const body = Buffer.from(JSON.stringify(value), 'utf8');
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length });
  res.end(body);
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', c => { size += c.length; if (size > 2_000_000) { reject(new Error('too large')); req.destroy(); } else chunks.push(c); });
    req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}); } catch (e) { reject(e); } });
    req.on('error', reject);
  });
}
function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); } catch { return fallback; }
}
function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + '\n', 'utf8');
}
function listDiary() {
  if (!fs.existsSync(diaryDir)) return [];
  return fs.readdirSync(diaryDir).filter(n => /^\d{4}-\d{2}-\d{2}\.md$/.test(n)).sort().reverse().map(n => {
    const date = n.slice(0, 10);
    let text = '';
    try { text = fs.readFileSync(path.join(diaryDir, n), 'utf8'); } catch {}
    const preview = text.replace(/^#.*$/gm, '').replace(/\s+/g, ' ').trim().slice(0, 80);
    return { date, preview };
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  try {
    if (req.method === 'GET' && url.pathname === '/api/info') return json(res, 200, { token: 'local', project: 'Amo Memory Viewer' });
    if (req.method === 'GET' && url.pathname === '/api/planner/items') return json(res, 200, { ok: true, items: readJson(plannerFile, { version: 1, items: [] }).items || [] });
    if (req.method === 'POST' && url.pathname === '/api/planner/items') {
      const body = await readBody(req);
      if (!Array.isArray(body.items)) return json(res, 400, { ok: false, error: 'items required' });
      writeJson(plannerFile, { version: 1, items: body.items });
      return json(res, 200, { ok: true });
    }
    if (req.method === 'GET' && url.pathname === '/api/planner/access') return json(res, 200, { ok: true, revision: 'local', selected: [] });
    if (req.method === 'POST' && url.pathname === '/api/planner/access') return json(res, 200, { ok: true });
    if (req.method === 'GET' && url.pathname === '/api/diary/list') return json(res, 200, { items: listDiary() });
    if (req.method === 'GET' && url.pathname === '/api/diary/profile') return json(res, 200, { text: fs.existsSync(profileFile) ? fs.readFileSync(profileFile, 'utf8') : '' });
    if (req.method === 'POST' && url.pathname === '/api/diary/profile') {
      const body = await readBody(req); fs.writeFileSync(profileFile, String(body.text || ''), 'utf8'); return json(res, 200, { ok: true });
    }
    if (req.method === 'GET' && url.pathname === '/api/diary/item') {
      const date = String(url.searchParams.get('date') || '');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json(res, 400, { error: 'invalid date' });
      const file = path.join(diaryDir, date + '.md');
      return json(res, 200, { text: fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '' });
    }
    if (req.method === 'POST' && url.pathname === '/api/diary/item') {
      const body = await readBody(req); const date = String(body.date || '');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json(res, 400, { error: 'invalid date' });
      fs.mkdirSync(diaryDir, { recursive: true }); fs.writeFileSync(path.join(diaryDir, date + '.md'), String(body.text || ''), 'utf8');
      return json(res, 200, { ok: true });
    }
    let file = null;
    if (url.pathname === '/' || url.pathname === '/planner') file = 'planner.html';
    else if (url.pathname === '/diary') file = 'diary.html';
    else if (/^\/[a-z0-9._-]+\.(html|css|js)$/i.test(url.pathname)) file = url.pathname.slice(1);
    if (file) {
      const target = path.join(publicDir, file);
      if (fs.existsSync(target) && fs.statSync(target).isFile()) {
        const body = fs.readFileSync(target);
        res.writeHead(200, { 'Content-Type': types[path.extname(target)] || 'application/octet-stream', 'Content-Length': body.length });
        return res.end(body);
      }
    }
    json(res, 404, { error: 'not found' });
  } catch (error) {
    json(res, 500, { error: String(error?.message || error) });
  }
});
server.listen(port, '127.0.0.1', () => console.log(`Amo memory viewer http://127.0.0.1:${port}`));
