/* Canonical dependency-free meeting Markdown renderer; mirror this file verbatim. */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.ScientificMarkdown = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  var MAX_INPUT = 200000;
  var ATTACHMENT = /^attachments?\/[a-f0-9]{32}\.(?:png|jpg|webp)$/;
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function linkUrl(value) {
    // Strict allow-list: entities, control/space and quotes are never decoded into URLs.
    if (/[\s<>"'`\\]/.test(value) || /&#/.test(value)) return '';
    return /^(?:https?:\/\/[^/]+|mailto:[^@]+@[^@]+|#[A-Za-z0-9_-]+)(?:[^\s]*)$/i.test(value) ? value : '';
  }
  function find(text, needle, start, state) {
    var span = Math.min(8192, text.length - start);
    if (state.work < span) return -1;
    var at = text.slice(start, start + span).indexOf(needle);
    state.work -= at < 0 ? span : at + needle.length;
    return at < 0 ? -1 : start + at;
  }
  function inline(text, state, depth, inLink) {
    if (depth > 6 || state.work <= 0 || state.tokens <= 0) return escapeHtml(text);
    var parts = [], plain = '', i = 0;
    function flush() { if (plain) { parts.push(escapeHtml(plain)); plain = ''; } }
    while (i < text.length) {
      if (state.work <= 0 || state.tokens <= 0) { plain += text.slice(i); break; }
      var ch = text[i], end, inner, token, size;
      if (ch === '\\' && i + 1 < text.length) { plain += text[i + 1]; i += 2; continue; }
      if (ch === '\n') { flush(); parts.push('<br>'); i++; continue; }
      if (ch === '`') {
        size = 1;
        while (size < 3 && text[i + size] === '`') size++;
        token = '`'.repeat(size);
        end = find(text, token, i + size, state);
        if (end >= 0) {
          flush(); parts.push('<code>' + escapeHtml(text.slice(i + size, end)) + '</code>');
          i = end + size; state.tokens--; continue;
        }
      }
      var image = ch === '!' && text[i + 1] === '[';
      if (image || (ch === '[' && !inLink)) {
        var start = i + (image ? 2 : 1);
        var labelEnd = find(text, '](', start, state);
        if (labelEnd >= 0) {
          var cursor = labelEnd + 2, balance = 1, limit = Math.min(text.length, cursor + 8192);
          for (; cursor < limit && balance && state.work > 0; cursor++, state.work--) {
            if (text[cursor] === '(') balance++;
            if (text[cursor] === ')') balance--;
          }
          if (!balance) {
            var label = text.slice(start, labelEnd), dest = text.slice(labelEnd + 2, cursor - 1);
            flush(); state.tokens--;
            if (image) {
              var blob = ATTACHMENT.test(dest) && Object.prototype.hasOwnProperty.call(state.images, dest) ? state.images[dest] : '';
              if (typeof blob === 'string' && /^blob:[^\s<>"'`]+$/.test(blob)) {
                parts.push('<img src="' + escapeHtml(blob) + '" alt="' + escapeHtml(label) + '" loading="lazy" decoding="async">');
              } else parts.push('<span class="meeting-markdown-image-placeholder">[图片：' + escapeHtml(label || '未加载') + ']</span>');
            } else {
              var safe = linkUrl(dest), content = inline(label, state, depth + 1, true);
              parts.push(safe ? '<a href="' + escapeHtml(safe) + '"' + (/^https?:/i.test(safe) ? ' target="_blank" rel="noopener noreferrer"' : '') + '>' + content + '</a>' : content);
            }
            i = cursor; continue;
          }
        }
      }
      if (ch === '*' || ch === '_') {
        size = text[i + 1] === ch ? 2 : 1;
        // Underscores inside identifiers remain literal.
        if (ch !== '_' || !/[\p{L}\p{N}]/u.test(text[i - 1] || '')) {
          token = ch.repeat(size); end = find(text, token, i + size, state);
          if (end > i + size) {
            inner = text.slice(i + size, end);
            if (!/^\s|\s$/.test(inner)) {
              flush(); var tag = size === 2 ? 'strong' : 'em';
              parts.push('<' + tag + '>' + inline(inner, state, depth + 1, inLink) + '</' + tag + '>');
              i = end + size; state.tokens--; continue;
            }
          }
        }
      }
      plain += ch; i++;
    }
    flush(); return parts.join('');
  }
  function cells(line) {
    var value = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    var result = [], cell = '', escaped = false, inCode = false;
    for (var i = 0; i < value.length; i++) {
      var ch = value[i];
      if (!escaped && ch === '`') inCode = !inCode;
      if (!escaped && !inCode && ch === '|') {
        result.push(cell.trim()); cell = '';
        if (result.length >= 64) return result;
      } else cell += ch;
      escaped = !escaped && ch === '\\';
    }
    result.push(cell.trim()); return result;
  }
  function tableHeader(lines, i) {
    if (i + 1 >= lines.length || lines[i].indexOf('|') < 0) return false;
    var separator = cells(lines[i + 1]);
    return separator.length > 0 && separator.every(function (s) { return /^:?-{3,}:?$/.test(s); });
  }
  function listItem(line) { return /^(\s*)([-+*]|\d{1,9}[.)])\s+(.*)$/.exec(line); }
  function startsBlock(lines, i) {
    return /^(?: {0,3}#{1,6}\s| {0,3}>|\s*`{3}|\s*~{3})/.test(lines[i]) || listItem(lines[i]) || tableHeader(lines, i);
  }
  function blocks(lines, state, depth) {
    if (depth > 8) return '<p>' + escapeHtml(lines.join('\n')) + '</p>';
    var output = [], i = 0;
    while (i < lines.length) {
      if (!lines[i].trim()) { i++; continue; }
      if (--state.blocks < 0) { output.push('<p>' + escapeHtml(lines.slice(i).join('\n')) + '</p>'); break; }
      var line = lines[i], match = /^\s*(`{3,}|~{3,})([^\s`]*)\s*$/.exec(line);
      if (match) {
        var fence = match[1], code = []; i++;
        while (i < lines.length && !new RegExp('^\\s*' + fence[0] + '{' + fence.length + ',}\\s*$').test(lines[i])) code.push(lines[i++]);
        if (i < lines.length) i++;
        output.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>'); continue;
      }
      match = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (match) { var h = match[1].length; output.push('<h' + h + '>' + inline(match[2], state, 0, false) + '</h' + h + '>'); i++; continue; }
      if (/^ {0,3}>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^ {0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^ {0,3}> ?/, ''));
        output.push('<blockquote>' + blocks(quote, state, depth + 1) + '</blockquote>'); continue;
      }
      if (tableHeader(lines, i)) {
        var header = cells(line), rows = [], width = header.length;
        rows.push('<table><thead><tr>' + header.map(function (cell) { return '<th>' + inline(cell, state, 0, false) + '</th>'; }).join('') + '</tr></thead><tbody>'); i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0 && state.tableCells >= width) {
          state.tableCells -= width;
          var row = cells(lines[i++]);
          rows.push('<tr>' + header.map(function (_, col) { return '<td>' + inline(row[col] || '', state, 0, false) + '</td>'; }).join('') + '</tr>');
        }
        rows.push('</tbody></table>'); output.push(rows.join('')); continue;
      }
      match = listItem(line);
      if (match) {
        var baseIndent = match[1].length, ordered = /^\d/.test(match[2]), tag = ordered ? 'ol' : 'ul', items = [];
        while (i < lines.length) {
          var current = listItem(lines[i]);
          if (!current || current[1].length !== baseIndent || /^\d/.test(current[2]) !== ordered) break;
          var item = [current[3]]; i++;
          while (i < lines.length && lines[i].trim() && /^\s+/.test(lines[i]) && (listItem(lines[i]) ? listItem(lines[i])[1].length > baseIndent : true)) item.push(lines[i++].slice(Math.min(baseIndent + 2, lines[i - 1].length)));
          var itemHtml = blocks(item, state, depth + 1);
          itemHtml = itemHtml.replace(/^<p>([\s\S]*?)<\/p>/, '$1');
          items.push('<li>' + itemHtml + '</li>');
        }
        output.push('<' + tag + '>' + items.join('') + '</' + tag + '>'); continue;
      }
      var paragraph = [line]; i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) paragraph.push(lines[i++]);
      output.push('<p>' + inline(paragraph.join('\n'), state, 0, false) + '</p>');
    }
    return output.join('\n');
  }
  function render(text, options) {
    var input = String(text == null ? '' : text).slice(0, MAX_INPUT).replace(/\r\n?/g, '\n');
    var metadata = '', front = /^---\n([\s\S]{0,10000}?)\n---(?:\n|$)/.exec(input);
    if (front) { metadata='<details class="meeting-frontmatter"><summary>报告元信息</summary><pre>'+escapeHtml(front[1])+'</pre></details>\n'; input=input.slice(front[0].length); }
    var images = options && options.images;
    return metadata + blocks(input.split('\n'), { images: images && typeof images === 'object' ? images : {}, work: 4000000, tokens: 10000, blocks: 10000, tableCells: 20000 }, 0);
  }
  var css = '.meeting-markdown{line-height:1.8;overflow-wrap:anywhere;color:inherit;font-size:15px;min-width:0}.meeting-markdown .meeting-frontmatter{font-size:.85em;color:#778296;border:1px solid #8598ad30;border-radius:7px;padding:.5em .8em;margin:.5em 0}.meeting-markdown .meeting-frontmatter summary{cursor:pointer}.meeting-markdown .meeting-frontmatter pre{margin:.5em 0}.meeting-markdown h1,.meeting-markdown h2,.meeting-markdown h3,.meeting-markdown h4,.meeting-markdown h5,.meeting-markdown h6{line-height:1.35;margin:1.5em 0 .65em;font-weight:650}.meeting-markdown h1{font-size:1.75em}.meeting-markdown h2{font-size:1.4em;border-bottom:1px solid #8884;padding-bottom:.35em}.meeting-markdown h3{font-size:1.18em}.meeting-markdown p{margin:.8em 0}.meeting-markdown ul,.meeting-markdown ol{padding-left:1.7em;margin:.65em 0}.meeting-markdown li{margin:.25em 0}.meeting-markdown li>ul,.meeting-markdown li>ol{margin:.2em 0}.meeting-markdown blockquote{margin:1em 0;padding:.15em 1em;border-left:3px solid #6d8fba;background:#8598ad10}.meeting-markdown code{font-family:ui-monospace,Consolas,monospace;font-size:.9em;background:#8598ad18;padding:.15em .35em;border-radius:4px;white-space:pre-wrap;overflow-wrap:anywhere}.meeting-markdown pre{background:#8598ad14;padding:1em;border:1px solid #8598ad30;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}.meeting-markdown pre code{padding:0;background:transparent}.meeting-markdown table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;margin:1em 0}.meeting-markdown th,.meeting-markdown td{border:1px solid #8598ad55;padding:.5em .8em;text-align:left;min-width:4em;overflow-wrap:anywhere}.meeting-markdown th{background:#8598ad18;font-weight:650}.meeting-markdown a{color:#4285bd;text-decoration:underline;text-underline-offset:3px}.meeting-markdown img{display:block;max-width:100%;height:auto;border-radius:6px;margin:.8em auto}.meeting-markdown-image-placeholder{display:inline-block;color:#7e8997;font-size:.9em;border:1px dashed #8598ad55;padding:.15em .5em;border-radius:4px}';
  return Object.freeze({ render: render, css: css });
});
