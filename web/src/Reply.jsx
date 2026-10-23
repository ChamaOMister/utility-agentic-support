import React from 'react';

// Renders the safe Markdown subset the model uses (bold, lists, simple tables) as React elements.
// Model text only ever becomes text nodes, never HTML.

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*$/;
const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^\s*#{1,6}\s+(.*)$/;

function inline(text) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') && part.length > 4
      ? <strong key={i}>{part.slice(2, -2)}</strong> : part);
}

const cells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

function parse(text) {
  const lines = text.split('\n');
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (TABLE_ROW.test(line)) {
      const rows = [];
      while (i < lines.length && TABLE_ROW.test(lines[i])) rows.push(lines[i++]);
      const rule = rows.findIndex(r => TABLE_RULE.test(r));
      const align = rule > 0 ? cells(rows[rule]).map(c => (c.endsWith(':') ? 'right' : undefined)) : [];
      const data = rows.filter((_, k) => k !== rule).map(cells);
      blocks.push({type: 'table', head: rule > 0 ? data[0] : null, body: rule > 0 ? data.slice(1) : data, align});
      continue;
    }
    const list = BULLET.test(line) ? [BULLET, 'ul'] : NUMBERED.test(line) ? [NUMBERED, 'ol'] : null;
    if (list) {
      const [pattern, type] = list;
      const items = [];
      while (i < lines.length && pattern.test(lines[i])) items.push(lines[i++].match(pattern)[1]);
      blocks.push({type, items});
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !TABLE_ROW.test(lines[i])
      && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i])) {
      const heading = lines[i].match(HEADING);
      para.push(heading ? `**${heading[1]}**` : lines[i]);
      i += 1;
    }
    blocks.push({type: 'p', text: para.join('\n')});
  }
  return blocks;
}

export default function Reply({text}) {
  return <div className="reply" data-testid="assistant-reply">{parse(text).map((block, b) => {
    if (block.type === 'p') return <p key={b}>{inline(block.text)}</p>;
    if (block.type === 'table') {
      return <div key={b} className="reply-table"><table>
        {block.head && <thead><tr>{block.head.map((c, k) => <th key={k} style={{textAlign: block.align[k]}}>{inline(c)}</th>)}</tr></thead>}
        <tbody>{block.body.map((row, r) => <tr key={r}>{row.map((c, k) =>
          <td key={k} style={{textAlign: block.align[k]}}>{inline(c)}</td>)}</tr>)}</tbody>
      </table></div>;
    }
    const List = block.type;
    return <List key={b}>{block.items.map((item, k) => <li key={k}>{inline(item)}</li>)}</List>;
  })}</div>;
}
