import { Parser } from 'htmlparser2';
import { WEB_READER_LIMITS } from './web-reader-policy.mjs';

const excluded = new Set(['script','style','noscript','template','svg','canvas','iframe','object','embed','nav','footer','form','button','select']);
const blocks = new Set(['p','div','section','article','main','h1','h2','h3','h4','h5','h6','li','ul','ol','blockquote','pre','br','tr','hr']);
const articleContainer = /^(?:js_content|article[-_]?(?:body|content)|post[-_]?content)$/;
const tidy = text => text.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '')
  .replace(/[ \t\u00a0]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
const clip = (text, limit) => Array.from(text).slice(0, limit).join('');

/** Streaming HTML parser; no JavaScript, CSS, images or subresources execute. */
export function extractWebContent(body, contentType = 'text/html') {
  if (contentType === 'text/plain') return { title: '', text: clip(tidy(body), WEB_READER_LIMITS.chars), truncated: Array.from(tidy(body)).length > WEB_READER_LIMITS.chars };
  let title = '', ogTitle = '', fallback = '', head = false;
  const stack = [], candidates = [], active = [];
  const append = text => { fallback += text; for (const candidate of active) candidate.text += text; };
  const parser = new Parser({
    onopentag(name, attrs) {
      const parent = stack.at(-1);
      // 微信公众号等页面把 <title> 留空、真正的标题放在 og:title。
      if (name === 'meta' && attrs.property === 'og:title' && !ogTitle) ogTitle = String(attrs.content ?? '').slice(0, 300);
      const ownStyle = attrs.style ?? '';
      // 微信公众号的新版阅读器把正文预渲染在 style="visibility:hidden;opacity:0"
      // 的容器里，等 JS 激活才显示——正文确实在 HTML 中，不能当成隐藏丢弃。
      // 祖先的 display:none 仍然生效，只有文章容器自身的“待渲染”样式被放行。
      const pendingRender = articleContainer.test(attrs.id ?? '') &&
        /(?:visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?)/i.test(ownStyle) &&
        !/display\s*:\s*none/i.test(ownStyle);
      const hidden = parent?.hidden || excluded.has(name) || attrs.hidden !== undefined || attrs['aria-hidden'] === 'true' ||
        (!pendingRender && /(?:display\s*:\s*none|visibility\s*:\s*hidden)/i.test(ownStyle)) ||
        /^(?:navigation|banner|contentinfo)$/.test(attrs.role ?? '');
      if (name === 'head') head = true;
      let candidate;
      // Bound overlapping captures to avoid quadratic work in hostile markup.
      if (!hidden && !head && active.length < 8 && (name === 'article' || name === 'main' || attrs.role === 'main' ||
          articleContainer.test(attrs.id ?? '') ||
          /\b(?:article[-_]?(?:body|content)|post[-_]?content|entry[-_]?content)\b/.test(attrs.class ?? ''))) {
        candidate = { text: '', priority: attrs.id === 'js_content' ? 3 : name === 'article' ? 2 : 1 };
        candidates.push(candidate); active.push(candidate);
      }
      stack.push({ name, hidden, candidate });
      if (!hidden && !head && blocks.has(name)) append('\n');
    },
    ontext(text) {
      if (stack.at(-1)?.name === 'title') title += text;
      if (!head && !stack.at(-1)?.hidden) append(text);
    },
    onclosetag(name) {
      const entry = stack.pop();
      if (!entry?.hidden && !head && blocks.has(name)) append('\n');
      if (entry?.candidate) active.splice(active.indexOf(entry.candidate), 1);
      if (name === 'head') head = false;
    },
  }, { decodeEntities: true });
  parser.end(body);
  const articles = candidates.map(item => ({ ...item, text: tidy(item.text) })).filter(item => item.text.length >= 80)
    .sort((a,b) => b.priority - a.priority || b.text.length - a.text.length);
  const text = articles[0]?.text ?? tidy(fallback);
  if (/浏览器下载建议|浏览器版本过低|browser (?:is )?(?:outdated|not supported)/i.test(tidy(title)) ||
      text.length < 1500 && /您的浏览器版本过低|your browser (?:is )?(?:outdated|not supported)/i.test(text)) throw Error('网站返回浏览器兼容性提示页，未取得目标正文');
  if (/^(?:Just a moment|Access denied|安全验证|访问受限|验证访问)/i.test(tidy(title)) ||
      text.length < 800 && /verify you are human|checking your browser|enable javascript and cookies|验证码|完成验证|访问过于频繁/i.test(text)) throw Error('页面要求验证码或访问验证，未取得正文');
  if (text.length < 80 && /(?:log in|sign in|登录|登入)/i.test(text)) throw Error('页面要求登录，未取得正文');
  if (!text || text.length < 80 && /javascript|加载中|loading/i.test(text)) throw Error('网页需要 JavaScript 或没有可读取正文');
  return { title: clip(tidy(title) || tidy(ogTitle), 200), text: clip(text, WEB_READER_LIMITS.chars), truncated: Array.from(text).length > WEB_READER_LIMITS.chars };
}
