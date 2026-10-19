// Exercise the shipped page script with a minimal DOM; no browser dependency.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() {
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.value = '';
    this.clientWidth = 1000;
    this.clientHeight = 700;
    this.classes = new Set();
    this.classList = {toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name)};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener() {}
  scrollIntoView() { this.scrolled = true; }
}

function textOf(element) {
  return [element.textContent || '', ...element.children.map(textOf)]
    .join(' ').replace(/\s+/g, ' ').replace(/ \./g, '.');
}

async function page() {
  const elements = new Map();
  const nodes = Array.from({length: 25}, (_, i) => ({id: `file${i}.py`, changed: true,
    status: {xy: ' M', unstaged: true}, in_graph: true}));
  const state = {revision: 1, busy: false, data: {root: '/fixture', generated: 'now', nodes,
    edges: [{source: 'file0.py', target: 'file24.py', evidence: [{relation: 'imports'}]}]}};
  let selection = {revision: 1, path: 'file24.py'};
  const context = vm.createContext({
    document: {
      getElementById: id => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      },
      createElement: () => new Element(), createElementNS: () => new Element(),
    },
    fetch: async route => ({ok: true, json: async () => route === 'selection' ? selection : state}),
    setTimeout: () => {},
  });
  const html = fs.readFileSync('src/review_graph/view.html', 'utf8');
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, state, run: code => vm.runInContext(code, context),
    select: async (path, revision) => {
      selection = {path, revision};
      await vm.runInContext('pollSelection()', context);
    }};
}

test('initial terminal selection centers the node and selects its relationships', async () => {
  const p = await page();
  assert.equal(p.run('selected'), 'file24.py');
  const [x, y, w, h] = Array.from(p.run('box'));
  assert.equal(x + w / 2, 40 + 190);
  assert.equal(y + h / 2, 24 * 100 + 35 + 38);
  assert.match(textOf(p.elements.get('detail')), /file0.py importuje plik file24.py\./);
  assert.equal(p.elements.get('detail-title').textContent, 'file24.py');
});

test('new selection stays visible through a filter; unchanged polling preserves manual browsing', async () => {
  const p = await page();
  p.elements.get('search').value = 'file0.py';
  await p.select('file20.py', 2);
  assert.ok(p.run('positions.has("file20.py")'));
  assert.equal(p.elements.get('search').value, 'file0.py');
  assert.equal(p.run('selected'), 'file20.py');
  p.run('select("file0.py")');
  await p.select('file20.py', 2);
  assert.equal(p.run('selected'), 'file0.py');
  await p.select('file21.py', 3);
  assert.equal(p.run('selected'), 'file21.py');
  p.state.revision++;
  await p.run('poll()');
  assert.equal(p.run('selected'), 'file21.py');
});

test('clearing selection and selecting a file absent from a stale graph are explicit', async () => {
  const p = await page();
  await p.select('app/missing.py', 2);
  assert.equal(p.run('selected'), null);
  assert.equal(p.elements.get('detail-title').textContent, 'app/missing.py');
  assert.match(p.elements.get('detail').textContent, /Naciśnij r/);
  p.state.data.nodes.push({id: 'app/missing.py', changed: true, status: {xy: '??'}});
  p.state.revision++;
  await p.run('poll()');
  assert.equal(p.run('selected'), 'app/missing.py');
  assert.equal(p.elements.get('detail-title').textContent, 'app/missing.py');
  await p.select(null, 3);
  assert.equal(p.run('selected'), null);
});

test('relationships explain direction, group repeated kinds and keep original evidence collapsed', async () => {
  const p = await page();
  p.state.data.edges.push({source: 'file24.py', target: 'file1.py', evidence: [
    {relation: 'calls', source: 'symbolA', target: 'symbolB'},
    {relation: 'calls', source: 'symbolC', target: 'symbolB'},
    {relationship: 'custom_relation'},
  ]});
  p.run('select("file24.py")');
  const detail = p.elements.get('detail');
  const text = textOf(detail);
  assert.match(text, /Połączone pliki: 2/);
  assert.match(text, /Co robi file24.py \(1\)/);
  assert.match(text, /Jak inne pliki korzystają z file24.py \(1\)/);
  assert.match(text, /file24.py wywołuje kod z pliku file1.py\./);
  assert.equal(text.match(/file24.py wywołuje kod z pliku file1.py\./g).length, 1);
  assert.doesNotMatch(text, /Liczba powiązań w kodzie|Ten plik →/);
  assert.match(text, /file24.py ma powiązanie z plikiem file1.py\./);
  const cards = detail.children.filter(n => n.className === 'connection-card');
  const raw = cards[0].children.at(-1);
  assert.equal(raw.open, undefined);
  assert.deepEqual(JSON.parse(raw.children[1].textContent), p.state.data.edges[1].evidence);
  assert.equal(cards[0].children[0].className, 'relation-list');
  cards[0].children.find(n => n.className === 'connection-file').onclick();
  assert.equal(p.run('selected'), 'file1.py');
});

test('edge details name both endpoints and preserve full paths as text', async () => {
  const p = await page();
  p.state.data.edges[0] = {source: 'src/<example>/same.py', target: 'tests/same.py',
    evidence: [{type: 'imports_from'}]};
  p.run('evidence([graph.edges[0]], null)');
  const text = textOf(p.elements.get('detail'));
  assert.match(text, /Z pliku same.py src\/<example>\/same.py/);
  assert.match(text, /Do pliku same.py tests\/same.py/);
  assert.match(text, /src\/<example>\/same.py importuje elementy z pliku tests\/same.py/);
});

test('imports_from reads as the same explicit sentence from either selected file', async () => {
  const p = await page();
  for (const focus of ['main.py', 'router.py', null]) {
    p.run(`evidence([{source: 'app/main.py', target: 'app/router.py', evidence: [
      {relation: 'imports_from'}, {relationship: ' IMPORTS_FROM '}
    ]}], ${JSON.stringify(focus && 'app/' + focus)})`);
    const card = p.elements.get('detail').children.find(n => n.className === 'connection-card');
    const sentences = card.children[0];
    assert.equal(sentences.children.length, 1);
    assert.equal(textOf(sentences.children[0]).trim(),
      'main.py importuje elementy z pliku router.py.');
    assert.equal(sentences.children[0].children[0].textContent, 'main.py');
    assert.equal(sentences.children[0].children[2].textContent, 'router.py');
    assert.equal(card.children.at(-1).open, undefined);
  }
});
