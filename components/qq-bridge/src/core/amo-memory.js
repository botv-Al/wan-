import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const root = 'E:/Weixin/data/workspace/state/amo-memory';
const diaryDir = path.join(root, 'diary');
const archiveDir = path.join(root, 'archive');
const weeklyDir = path.join(root, 'weekly');
const profileFile = path.join(root, 'profile.md');
const pendingFile = path.join(root, 'pending', 'claims.json');

function ensure() {
  fs.mkdirSync(diaryDir, { recursive: true });
  fs.mkdirSync(weeklyDir, { recursive: true });
  fs.mkdirSync(path.join(root, 'people'), { recursive: true });
  fs.mkdirSync(path.join(root, 'pending'), { recursive: true });
  fs.mkdirSync(path.join(root, 'archive'), { recursive: true });
  if (!fs.existsSync(profileFile)) fs.writeFileSync(profileFile, '# 关于对方\n\n', 'utf8');
  if (!fs.existsSync(pendingFile)) fs.writeFileSync(pendingFile, JSON.stringify({ version: 1, items: [] }, null, 2) + '\n', 'utf8');
}

export function writeWeekly(label, text) {
  ensure();
  fs.writeFileSync(path.join(weeklyDir, `${label}.md`), String(text).trim() + '\n', 'utf8');
}

export function listDiaries(count = 7) {
  ensure();
  return fs.readdirSync(diaryDir)
    .filter(name => /^\d{4}-\d{2}-\d{2}\.md$/.test(name))
    .sort()
    .slice(-count)
    .map(name => ({ date: name.slice(0, 10), text: fs.readFileSync(path.join(diaryDir, name), 'utf8').trim() }));
}

export function writeDiary(date, text) {
  ensure();
  fs.writeFileSync(path.join(diaryDir, `${date}.md`), String(text).trim() + '\n', 'utf8');
}

export function archiveOldDiaries(days = 90) {
  ensure();
  fs.mkdirSync(archiveDir, { recursive: true });
  const cutoff = Date.now() - days * 86400000;
  let moved = 0;
  for (const name of fs.readdirSync(diaryDir)) {
    if (!/^\d{4}-\d{2}-\d{2}\.md$/.test(name)) continue;
    const source = path.join(diaryDir, name);
    if (fs.statSync(source).mtimeMs >= cutoff) continue;
    const target = path.join(archiveDir, name);
    if (fs.existsSync(target)) { fs.appendFileSync(target, '\n\n' + fs.readFileSync(source, 'utf8'), 'utf8'); fs.unlinkSync(source); }
    else fs.renameSync(source, target);
    moved++;
  }
  return moved;
}

export function readProfile() {
  ensure();
  return fs.readFileSync(profileFile, 'utf8');
}

function amonFile() {
  ensure();
  return path.join(root, 'people', 'amon.md');
}

function appendAmonFact(text, source = '') {
  ensure();
  const clean = String(text ?? '').replace(/^[\s\-*•]+/, '').trim();
  if (!clean) return;
  const file = amonFile();
  let body = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '# 阿蒙\n\n## 已确认的事实\n';
  if (body.includes(clean)) return;
  const line = `- ${clean}${source ? `（来源：${source}）` : ''}`;
  if (body.includes('## 已确认的事实')) {
    body = body.replace('## 已确认的事实', `## 已确认的事实\n\n${line}`);
  } else {
    body += `\n## 已确认的事实\n\n${line}\n`;
  }
  fs.writeFileSync(file, body.replace(/\n{3,}/g, '\n\n').trimEnd() + '\n', 'utf8');
}

function readPending() {
  ensure();
  try {
    const value = JSON.parse(fs.readFileSync(pendingFile, 'utf8').replace(/^\uFEFF/, ''));
    return Array.isArray(value.items) ? value : { version: 1, items: [] };
  } catch {
    return { version: 1, items: [] };
  }
}

function writePending(value) {
  ensure();
  const temp = pendingFile + '.tmp';
  fs.writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', 'utf8');
  fs.renameSync(temp, pendingFile);
}

export function addClaims(claims, source = '') {
  ensure();
  const pending = readPending();
  const auto = [];
  const queued = [];
  for (const raw of Array.isArray(claims) ? claims : []) {
    const item = typeof raw === 'string' ? { text: raw } : (raw ?? {});
    const text = String(item.text ?? '').replace(/^[\s\-*•]+/, '').trim();
    if (!text || text === 'NONE') continue;
    const kind = String(item.kind ?? 'fact');
    const confidence = String(item.confidence ?? 'medium');
    const importance = String(item.importance ?? 'normal');
    if (pending.items.some(entry => entry.text === text)) continue;
    if (confidence === 'high' && importance !== 'high' && !['identity', 'dream', 'relationship'].includes(kind)) {
      appendAmonFact(text, source);
      auto.push(text);
    } else {
      pending.items.push({ id: crypto.randomUUID(), text, kind, confidence, importance, source, createdAt: Date.now() });
      queued.push(text);
    }
  }
  writePending(pending);
  return { auto, pending: queued };
}

export function listPending() {
  return readPending().items;
}

export function resolvePending(id, action) {
  const pending = readPending();
  const index = pending.items.findIndex(item => item.id === id);
  if (index < 0) throw new Error('pending claim not found');
  const item = pending.items[index];
  if (action === 'approve') appendAmonFact(item.text, item.source);
  pending.items.splice(index, 1);
  writePending(pending);
  return item;
}

export function appendFacts(lines) {
  ensure();
  const clean = (lines ?? []).map(line => String(line).replace(/^[\s\-*•]+/, '').trim()).filter(line => line && line !== 'NONE');
  if (!clean.length) return;
  const existing = fs.readFileSync(profileFile, 'utf8').split('\n')
    .map(line => line.replace(/^[\s\-*•]+/, '').trim())
    .filter(Boolean);
  for (const line of clean) {
    if (line.startsWith('替换|')) {
      const [, oldFact, newFact] = line.split('|').map(part => part.trim());
      if (!newFact) continue;
      const index = existing.findIndex(item => item === oldFact);
      if (index >= 0) existing[index] = newFact;
      else if (!existing.includes(newFact)) existing.push(newFact);
    } else if (!existing.includes(line)) {
      existing.push(line);
    }
  }
  fs.writeFileSync(profileFile, '# 关于对方\n\n' + existing.map(line => '- ' + line).join('\n') + '\n', 'utf8');
}

function recentDiaries(count = 2) {
  ensure();
  return fs.readdirSync(diaryDir)
    .filter(name => /^\d{4}-\d{2}-\d{2}\.md$/.test(name))
    .sort()
    .slice(-count)
    .reverse()
    .map(name => ({ name, text: fs.readFileSync(path.join(diaryDir, name), 'utf8').trim() }));
}

function latestWeekly() {
  ensure();
  const files = fs.readdirSync(weeklyDir).filter(name => /^\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.md$/.test(name)).sort();
  if (!files.length) return null;
  const name = files.at(-1);
  return { name, text: fs.readFileSync(path.join(weeklyDir, name), 'utf8').trim() };
}

const dateKey = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const shiftDay = days => { const date = new Date(); date.setDate(date.getDate() + days); return dateKey(date); };

function dateFromQuery(query) {
  if (/大前天/.test(query)) return shiftDay(-3);
  if (/前天/.test(query)) return shiftDay(-2);
  if (/昨天|昨晚/.test(query)) return shiftDay(-1);
  if (/今天|今晚/.test(query)) return shiftDay(0);
  const full = query.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (full) return `${full[1]}-${String(full[2]).padStart(2, '0')}-${String(full[3]).padStart(2, '0')}`;
  const short = query.match(/(\d{1,2})月(\d{1,2})日/);
  if (short) return `${new Date().getFullYear()}-${String(short[1]).padStart(2, '0')}-${String(short[2]).padStart(2, '0')}`;
  return '';
}

const stopTerms = new Set(['昨天', '昨晚', '前天', '今天', '今晚', '什么', '怎么', '记得', '你还', '那个', '这个', '后来', '我们', '现在', '时候', '一下', '事情', '还是', '就是']);

function queryTerms(query) {
  const out = new Set();
  for (const token of String(query).replace(/[^\p{Script=Han}a-zA-Z0-9]+/gu, ' ').split(/\s+/)) {
    if (!token) continue;
    if (/^[a-zA-Z0-9]+$/.test(token)) {
      if (token.length >= 3) out.add(token.toLowerCase());
      continue;
    }
    for (let i = 0; i < token.length - 1; i++) {
      const pair = token.slice(i, i + 2);
      if (!stopTerms.has(pair)) out.add(pair);
    }
    if (token.length <= 6 && !stopTerms.has(token)) out.add(token);
  }
  return [...out];
}

function scoreText(text, terms) {
  let score = 0;
  for (const term of terms) {
    let from = 0;
    while (true) {
      const index = text.indexOf(term, from);
      if (index < 0) break;
      score++;
      from = index + term.length;
    }
  }
  return score;
}

function snippet(text, terms) {
  const indexes = terms.map(term => text.indexOf(term)).filter(index => index >= 0);
  const start = indexes.length ? Math.max(0, Math.min(...indexes) - 60) : 0;
  return text.slice(start, start + 240).trim();
}

function oldDiaryFor(query, recentNames) {
  const target = dateFromQuery(query);
  const sources = [diaryDir, archiveDir].flatMap(dir => fs.existsSync(dir)
    ? fs.readdirSync(dir).filter(name => /^\d{4}-\d{2}-\d{2}\.md$/.test(name)).map(name => ({ name, file: path.join(dir, name) }))
    : []);
  if (target && !recentNames.has(target + '.md')) {
    const found = sources.find(item => item.name === target + '.md');
    if (found) return { name: target + '.md', text: fs.readFileSync(found.file, 'utf8').trim(), reason: 'date' };
  }
  const terms = queryTerms(query);
  if (!terms.length) return null;
  let best = null;
  for (const item of sources) {
    if (recentNames.has(item.name)) continue;
    const text = fs.readFileSync(item.file, 'utf8').trim();
    const score = scoreText(text, terms);
    if (score >= 2 && (!best || score > best.score)) best = { name: item.name, text, score };
  }
  return best ? { name: best.name, text: snippet(best.text, terms), reason: 'keyword' } : null;
}

function injectionText(text) {
  return String(text)
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^[ \t]*(今天记住|这周记住)[:：].*$/gm, '')
    .replace(/\n---\s*\n总结[:：][\s\S]*$/, '')
    .trim();
}

export function amoMemoryPrompt(query = '', maxChars = 1800) {
  ensure();
  const readLayer = rel => { try { return fs.readFileSync(path.join(root, rel), 'utf8').trim(); } catch { return ''; } };
  const core = readLayer('core.md');
  const amon = readLayer('people/amon.md');
  const relationship = readLayer('relationship.md');
  const profile = fs.readFileSync(profileFile, 'utf8').replace(/^#.*$/gm, '').trim();
  const diaries = recentDiaries(2);
  const weekly = latestWeekly();
  const recentNames = new Set(diaries.map(diary => diary.name));
  const old = oldDiaryFor(query, recentNames);
  const parts = [];
  if (core) parts.push('【底层认知】\n' + injectionText(core).slice(0, 900));
  if (amon) parts.push('【关于阿蒙】\n' + injectionText(amon).slice(0, 320));
  if (relationship) parts.push('【你们的关系】\n' + injectionText(relationship).slice(0, 260));
  if (!amon && profile) parts.push('【你记得的关于对方的事】\n' + profile.slice(0, 260));
  if (weekly) parts.push(`【这周（${weekly.name.replace('.md', '').replace('_', ' 到 ')}）】\n` + injectionText(weekly.text).slice(0, 200));
  for (const diary of diaries) {
    parts.push(`【${diary.name.replace('.md', '')} 的日记】\n` + injectionText(diary.text).slice(0, 240));
  }
  if (old) {
    parts.push(`【你想起的旧事（${old.name.replace('.md', '')}）】\n` + injectionText(old.text).slice(0, 200));
  }
  if (!parts.length) return '';
  return ('【你记得的事（只是背景，不要主动罗列，也不要提“记忆”“日记”）】\n' + parts.join('\n\n')).slice(0, maxChars);
}
