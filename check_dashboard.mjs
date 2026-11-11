import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync('dashboard.html', 'utf8');
console.log('tamanho dashboard.html:', html.length, 'bytes');
console.log('placeholders restantes:', ['__DATA__','__CHARTJS__','__GERADO_EM__'].map(p => html.includes(p)));

// nenhuma tag <script> aninhada dentro de conteúdo de script (bug do wrapper duplo)
const nested = [...html.matchAll(/<script>(\s*<)/g)];
if (nested.length) { console.error('ERRO: tag <script> aninhada logo após <script>'); process.exit(1); }
console.log('sem <script> aninhado ✔');

// extrai o último <script> (o da aplicação) e valida sintaxe + execução com DOM fake
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
console.log('scripts inline:', scripts.length, '| chart.js embutido:', scripts[0].length > 150000);
// o script do Chart.js precisa compilar
new Function(scripts[0]);
console.log('chart.js compila ✔');

const app = scripts[scripts.length - 1];
const dataM = app.match(/const DATA = (\{[\s\S]*?\});\s*const C = /);
if (!dataM) { console.error('DATA não encontrado no script da aplicação'); process.exit(1); }
const data = JSON.parse(dataM[1]);
console.log('DATA parse ok | dias:', data.dias.map(d => `${d.id}:${d.tem_dados}`).join(', '),
  '| interações geral:', data.geral.kpis.interacoes);

// executa o script da aplicação num DOM mínimo para pegar erros de runtime
const canvases = ['cDonut','cHoras','cDdd','cOper','cUf','cPdmDonut','cPdmNte'];
const els = {};
const mkEl = () => ({
  innerHTML: '', textContent: '', style: {}, dataset: {},
  closest: () => mkEl(),
  querySelectorAll: () => [],
  querySelector: () => mkEl(),
  onclick: null,
});
canvases.forEach(id => els[id] = mkEl());
const document_ = {
  querySelector: sel => {
    const id = sel.replace('#', '');
    return els[id] || mkEl();
  },
  getElementById: id => els[id] || mkEl(),
};
const Chart = function (id, cfg) {
  if (!cfg?.data?.labels || !cfg?.data?.datasets) throw new Error(`Chart ${id} sem data`);
  for (const ds of cfg.data.datasets)
    if (!Array.isArray(ds.data)) throw new Error(`Chart ${id} dataset sem array`);
  return { config: cfg };
};
const ctx = { document: document_, Chart, Intl, console };
vm.createContext(ctx);
try {
  vm.runInContext(app, ctx);
  console.log('script da aplicação executado sem erros ✔');
  // valida que os charts receberam dados
  const chartIds = Object.keys(ctx).filter(k => k.startsWith('c'));
  const K = data.geral.kpis;
  console.log(`KPIs renderizados: interações ${K.interacoes}, SIM ${K.pct_sim}%, PdM identificados ${data.geral.pdmeia.telefones_com_resposta}`);
} catch (e) {
  console.error('ERRO no runtime:', e.message);
  process.exit(1);
}
