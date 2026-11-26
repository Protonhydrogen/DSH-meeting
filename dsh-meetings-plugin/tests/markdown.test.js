import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const canonical = new URL('../../src/scientific_conference/markdown.js', import.meta.url);
const mirror = new URL('../lib/markdown.js', import.meta.url);
function renderer() {
  const context = vm.createContext({});
  if (fs.existsSync(canonical)) vm.runInContext(fs.readFileSync(canonical, 'utf8'), context, { timeout: 1000 });
  return context.ScientificMarkdown;
}

test('classic script exposes a renderer without browser or third party globals', () => {
  assert.equal(typeof renderer()?.render, 'function');
  assert.equal(typeof renderer()?.css, 'string');
});

test('formats meeting notes with headings, emphasis, paragraphs, nested lists and quotations', () => {
  const html = renderer().render('# Meeting\n\nA **bold** and *quiet* `x < y`.\nnext line\n\n- First\n  - Child\n- Second\n\n1. One\n2. Two\n\n> Quoted **finding**');
  for (const expected of ['<h1>Meeting</h1>', '<strong>bold</strong>', '<em>quiet</em>', '<code>x &lt; y</code>', '<br>', '<ul>', '<ol>', '<li>Child</li>', '<blockquote>']) assert.ok(html.includes(expected), expected);
});

test('code blocks and raw HTML stay inert, including quoted attribute injection', () => {
  const html = renderer().render('<img src=x onerror=alert(1)>\n\n```html\n<script>alert("x")</script>\n```\n\n[<b>x</b>](https://example.test/\"onclick=\"bad)');
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes(' onclick='));
});

test('renders pipe tables and safe external links with opener isolation', () => {
  const html = renderer().render('| Name | Result |\n| --- | :---: |\n| **A** | 12 |\n\n[paper](https://example.test/paper?q=a&b=c) [email](mailto:a@example.test)');
  assert.ok(html.includes('<table>'));
  assert.ok(html.includes('<th>Name</th>'));
  assert.ok(html.includes('<td><strong>A</strong></td>'));
  assert.ok(html.includes('href="https://example.test/paper?q=a&amp;b=c"'));
  assert.ok(html.includes('target="_blank" rel="noopener noreferrer"'));
  assert.ok(html.includes('href="mailto:a@example.test"'));
});

test('blocks unsafe, encoded, relative and whitespace-obfuscated link destinations', () => {
  for (const url of ['javascript:alert(1)', 'JavaScript:bad', 'java\tscript:bad', 'data:text/html,hello', 'vbscript:bad', '//evil.test', '/api/destructive', 'jav&#97;script:bad', 'https://example.test/\"onload=bad']) {
    const html = renderer().render(`[go](${url})`);
    assert.ok(!html.includes('<a '), url);
  }
});

test('only exact authenticated attachment mappings produce images', () => {
  const attachment = 'attachments/0123456789abcdef0123456789abcdef.png';
  const key = attachment.replace('attachments/', 'attachment/');
  const html = renderer().render(`![plot <x>](${key}) ![web](https://evil.test/tracker.png) ![bad](${attachment})`, { images: { [key]: 'blob:https://localhost/known' } });
  assert.equal((html.match(/<img /g) || []).length, 1);
  assert.ok(html.includes('src="blob:https://localhost/known"'));
  assert.ok(html.includes('alt="plot &lt;x&gt;"'));
  assert.ok(!html.includes('src="https://evil.test'));
  assert.ok(html.includes('web'));
  assert.ok(html.includes('bad'));
  assert.ok(!renderer().render(`![plot](${key})`, { images: { [key]: 'data:image/png,evil' } }).includes('<img '));
  assert.ok(!renderer().render(`![plot](${key})`, { images: Object.create({ [key]: 'blob:https://localhost/inherited' }) }).includes('<img '));
});

test('caps large and adversarial input in a standalone vm execution budget', () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(canonical, 'utf8'), context);
  context.payload = '[*'.repeat(100000) + 'UNRENDERED_TAIL';
  const html = vm.runInContext('ScientificMarkdown.render(payload)', context, { timeout: 1500 });
  assert.ok(html.length < 4000000);
  assert.ok(!html.includes('UNRENDERED_TAIL'));
});

test('plugin mirrors canonical renderer exactly to preserve the security boundary', () => {
  assert.equal(fs.readFileSync(mirror, 'utf8'), fs.readFileSync(canonical, 'utf8'));
});

test('sparse adversarial tables cannot expand into millions of empty cells', () => {
  const header = '|x'.repeat(64) + '|\n' + '|---'.repeat(64) + '|\n';
  const html = renderer().render(header + '|\n'.repeat(80000));
  assert.ok(html.length < 4000000, `expanded to ${html.length} bytes`);
});

test('renders the real notebook plural attachments marker using its authenticated blob mapping', () => {
  const id = '53b1c292eb0fd8b846414337b5cd3b28';
  assert.equal(id.length, 32);
  const key = `attachments/${id}.jpg`;
  const html = renderer().render(`![报告截图](${key})`, { images: { [key]: 'blob:http://127.0.0.1:61040/notebook-upload' } });
  assert.ok(html.includes('<img src="blob:http://127.0.0.1:61040/notebook-upload"'));
  assert.ok(html.includes('alt="报告截图"'));
  assert.ok(!html.includes('meeting-markdown-image-placeholder'));
  for (const invalid of [`attachments/${id}0.jpg`, `attachments/${id.slice(1)}.jpg`, `attachments/../${id}.jpg`, `attachments/${id}.svg`]) {
    assert.ok(!renderer().render(`![bad](${invalid})`, { images: { [invalid]: 'blob:http://127.0.0.1:61040/unsafe' } }).includes('<img '));
  }
});


test('academic YAML metadata is collapsed while the report remains rendered and safe',()=>{
 const html=renderer().render('---\ntitle: "Report"\nspeaker: "<img src=x onerror=evil()>"\n---\n\n## Main findings\n\n**Evidence**');
 assert.match(html,/<details class="meeting-frontmatter">/);assert.match(html,/<summary>报告元信息<\/summary>/);assert.match(html,/<h2>Main findings<\/h2>/);assert.match(html,/<strong>Evidence<\/strong>/);assert(!html.includes('<img src=x'));
});
