// Runs the status page's own <script> against a minimal fake DOM and prints what it rendered as JSON.
// Input (stdin, JSON): {page, status, history, search}. No browser or npm packages needed.
const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const script = /<script>([\s\S]*?)<\/script>/.exec(input.page)[1];

const created = [];
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.style = {}; this._text = ""; this.className = ""; created.push(tag); }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); }
  replaceChildren(...cs) { this.children = cs; this._text = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
}
const ids = {};
for (const id of ["ver", "stamp", "cards", "control", "system", "health", "charts"]) ids[id] = new Node("#" + id);
created.length = 0;                                  // only count elements the page itself creates

const fetched = [];
const sandbox = {
  document: { getElementById: (id) => ids[id], createElement: (t) => new Node(t), createElementNS: (ns, t) => new Node(t) },
  location: { search: input.search || "" },
  fetch: async (url) => {
    fetched.push(url);
    if (input.failFetch) return { ok: false, status: input.failFetch, json: async () => ({}) };
    const body = url.startsWith("/status.json") ? input.status : input.history;
    return { ok: true, status: 200, json: async () => body };
  },
  setInterval: () => 0,
  console,
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

setTimeout(() => {
  const texts = (n) => n.children.map((c) => c.textContent);
  const charts = ids.charts.children;
  console.log(JSON.stringify({
    ver: ids.ver.textContent,
    stamp: ids.stamp.textContent,
    stampClass: ids.stamp.className,
    cards: texts(ids.cards),
    control: texts(ids.control),
    system: texts(ids.system),
    health: ids.health.children.map((c) => ({cls: c.className, text: c.textContent})),
    charts: charts.length,
    svgs: created.filter((t) => t === "svg").length,
    paths: created.filter((t) => t === "path").length,
    tags: Array.from(new Set(created)),
    fetched,
  }));
}, 50);
