#!/usr/bin/env node
'use strict';
/*
 * Second-cohort GEO spike — Gemini-only, Vertex + ADC.
 *
 * Mirrors the Joy basis (tiers A/B/C/D, 3 runs) so the two cohorts are
 * comparable, with two deliberate improvements learned from that run:
 *   1. Vertex grounding redirectors are RESOLVED before classification.
 *      100% of Gemini responses in the Joy corpus carried an unresolved
 *      vertexaisearch.cloud.google.com URL; classifying without resolving
 *      discards the entire Gemini destination signal.
 *   2. Every host in the answer is recorded (all_hosts), not just one
 *      link_destination. 49.8% of Joy responses cited >1 resolvable host,
 *      which the single-link schema could not represent.
 *
 * Usage:
 *   BRAND="Our Place" DOMAIN=fromourplace.com node run_cohort2.js
 * Optional: COLLECTION (default best-sellers, falls back to all), RUNS (3),
 *           SKUS (8), COMPETITORS="A,B,C" (else inferred), OUTDIR.
 */
const fs = require('fs');
const path = require('path');
const AGENT = '/Users/pengchydan/dev/PIVOTA-Agent';
const { GoogleGenAI } = require(`${AGENT}/node_modules/@google/genai`);
const vertexGemini = require(`${AGENT}/src/llm/vertexGemini`);

const BRAND = process.env.BRAND;
const DOMAIN = (process.env.DOMAIN || '').replace(/^https?:\/\//, '').replace(/\/.*$/, '');
if (!BRAND || !DOMAIN) { console.error('BRAND and DOMAIN are required'); process.exit(2); }
const RUNS = Number(process.env.RUNS || 3);
const NSKU = Number(process.env.SKUS || 8);
const MODEL = process.env.MODEL || 'gemini-2.5-flash';
const OUT = process.env.OUTDIR || path.join(process.env.HOME, 'dev',
  `${BRAND.toLowerCase().replace(/[^a-z0-9]+/g, '_')}_geo_${new Date().toISOString().slice(0, 10)}`);
const RAW = path.join(OUT, 'raw');
fs.mkdirSync(RAW, { recursive: true });
const ROWS = path.join(OUT, 'per_response_rows.jsonl');

const ai = new GoogleGenAI(vertexGemini.geminiClientOptions(process.env.GEMINI_API_KEY || ''));
const norm = s => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const REDIR = 'vertexaisearch.cloud.google.com';

function host(u) { try { return new URL(u).hostname.toLowerCase().replace(/^www\./, ''); } catch { return ''; } }
function urlsIn(t) { return [...String(t || '').matchAll(/https?:\/\/[^\s<>"'\])}]+/gi)].map(m => m[0].replace(/[.,;:]+$/, '')); }

// --- Vertex redirector resolution (HEAD, no redirect follow, Location only) ---
const rcache = new Map();
async function resolveRedirect(u) {
  if (rcache.has(u)) return rcache.get(u);
  let out = null;
  try {
    const r = await fetch(u, { method: 'GET', redirect: 'manual' });
    const loc = r.headers.get('location');
    if (loc && /^https?:\/\//i.test(loc)) out = loc;
  } catch { /* leave null */ }
  rcache.set(u, out);
  return out;
}
async function resolvedHosts(text) {
  const seen = new Set();
  for (const u of urlsIn(text)) {
    const h = host(u);
    if (!h) continue;
    if (h === REDIR) { const real = await resolveRedirect(u); if (real) { const rh = host(real); if (rh) seen.add(rh); } else seen.add(REDIR); }
    else seen.add(h);
  }
  seen.delete('schema.org'); seen.delete('w3.org'); seen.delete('shop.app');
  return [...seen];
}

async function shopifyProducts() {
  for (const coll of [process.env.COLLECTION || 'best-sellers', 'all']) {
    const u = `https://${DOMAIN}/collections/${coll}/products.json?limit=250`;
    try {
      const r = await fetch(u, { headers: { 'User-Agent': 'Pivota-GEO-Baseline/1.0 (+read-only)' } });
      if (!r.ok) continue;
      const j = await r.json();
      const BUNDLE = /\b(set|kit|bundle|duo|trio|routine|edit|collection|pack)\b/i;
      const ps = (j.products || [])
        .filter(p => (p.variants || []).some(v => v.available === true))
        .filter(p => !BUNDLE.test(p.title || '') && !BUNDLE.test(p.product_type || ''));
      if (ps.length >= 3) return { collection: coll, products: ps };
    } catch { /* next */ }
  }
  throw new Error(`no usable Shopify feed for ${DOMAIN}`);
}

async function gemini(prompt) {
  const r = await ai.models.generateContent({
    model: MODEL, contents: prompt,
    config: { tools: [{ googleSearch: {} }], temperature: Number(process.env.TEMP ?? 0) },
  });
  return r.text || '';
}

function auditInstruction(query) {
  return `You are a US consumer shopping assistant using live web search. Answer the following query as a normal, concise shopping answer for a US shopper. Use USD, recommend only products/brands you would genuinely recommend, and include a direct buying URL whenever you recommend a purchase path. Do not invent a URL, price, stock status, product name, or retailer.\n\nQuery: ${query}\n\nAfter the answer, append exactly one <GEO_AUDIT> JSON object and no prose after it. The JSON must be valid and have this shape: {"recommendations":[{"brand":"...","product":"exact product name or null","url":"https://... or null","price_usd":number or null,"availability":"in stock|out of stock|not stated"}],"first_brand_recommended":"... or null"}. Include every brand or product you recommended in recommendation order; do not include brands merely mentioned as context.`;
}

async function inferContext(products) {
  const titles = products.slice(0, 12).map(p => p.title).join('; ');
  const types = [...new Set(products.map(p => p.product_type).filter(Boolean))].slice(0, 8).join(', ');
  const raw = await gemini(
    `Brand "${BRAND}" (${DOMAIN}) sells: ${titles}. Product types: ${types || 'unknown'}.\n` +
    `Reply with ONLY minified JSON, no prose, no code fence:\n` +
    `{"category":"<3-6 word US retail category>","price_anchor":"<a realistic budget phrase a US shopper would type, e.g. 'under $50'>",` +
    `"unbranded_queries":["<12 unbranded discovery queries a US shopper types when they do NOT know this brand; the kind where a purchase decision is made; no brand names>"],` +
    `"incumbents":["<6 well-known competing brands a US shopper would name in this category>"]}`);
  const m = raw.match(/\{[\s\S]*\}/);
  if (!m) throw new Error('context inference returned no JSON');
  return JSON.parse(m[1] || m[0]);
}


// --- Joy-basis reuse: the EXACT Tier B / Tier C queries from the
// judydoll_joocyee 2026-08-23 run, so a same-vertical cohort is compared on
// identical unbranded demand rather than freshly generated lookalikes. ---
const JOY_B = [
  'best contour palette under $15', 'best affordable mascara for straight lashes',
  'transfer-proof matte lip stain under $10', 'best cheap cream blush',
  'best budget highlighter for fair skin', "best drugstore-price liquid eyeliner that doesn't smudge",
  'makeup for warm/olive undertone on a budget', "long-wear lip gloss that isn't sticky",
  'beginner contour kit', "TikTok viral makeup that's actually good",
  'best drugstore lip gloss under $15', 'best mascara that holds a curl all day',
  'best powder contour palette for beginners', 'affordable makeup for oily skin',
  'best long-wear liquid lipstick under $15', 'best affordable C-beauty makeup in the US',
];
const JOY_C_TARGETS = ['Charlotte Tilbury contour wand', 'Rare Beauty liquid blush',
  'Fenty Gloss Bomb', 'NARS blush', 'Dior Lip Glow Oil', 'Patrick Ta contour'];


// --- SKINCARE_BASIS: a fixed skincare Tier B/C set, shared verbatim by every
// skincare brand in the cohort. The Joy makeup queries would be a category
// mismatch for a toner/serum brand and would fabricate a ~0% "confirmation".
// Cross-vertical comparison uses the branded/unbranded RATIO, not the raw rate. ---
const SKIN_B = [
  'best korean toner for glass skin', 'best affordable niacinamide serum',
  'best cleansing oil for blackheads under $25', 'best gentle exfoliating toner for sensitive skin',
  'best moisturizer for dry skin under $30', "best vitamin C serum that isn't sticky",
  'best korean skincare for acne-prone skin', 'best affordable retinol alternative',
  'best hydrating toner for dehydrated skin', 'beginner korean skincare routine products',
  'best budget ceramide moisturizer', 'best drugstore-price hyaluronic acid serum',
  'best pore minimizing toner under $25', "TikTok viral skincare that's actually good",
  'best affordable K-beauty skincare in the US', 'best double cleanse products for oily skin',
];
const SKIN_C_TARGETS = ["Paula's Choice BHA liquid exfoliant", 'The Ordinary niacinamide',
  'Glow Recipe Watermelon Toner', 'Drunk Elephant Protini', 'SK-II Facial Treatment Essence',
  'Laneige Water Bank cream'];

function buildQueries(ctx, skus) {
  const p = skus.map(s => s.title);
  const q = []; let i = { A: 0, B: 0, C: 0, D: 0 };
  const add = (tier, text) => q.push({ query_id: `X-${tier}${String(++i[tier]).padStart(2, '0')}`, tier, query: text });
  [`where to buy ${BRAND} ${p[0]}`, `${BRAND} ${p[1] || p[0]} price`, `is ${BRAND} ${p[3] || p[0]} worth it`, `${BRAND} official website`,
   `where to buy ${BRAND} ${p[2] || p[0]}`, `${BRAND} ${p[4] || p[0]} price`, `is ${BRAND} ${p[6] || p[0]} worth it`, `${BRAND} best sellers official website`,
  ].forEach(x => add('A', x));
  const joy = String(process.env.JOY_BASIS || '') === '1';
  const skin = String(process.env.SKINCARE_BASIS || '') === '1';
  if (skin) {
    SKIN_B.forEach(x => add('B', x));
    SKIN_C_TARGETS.forEach(x => { add('C', `affordable dupe for ${x}`); add('C', `cheaper alternative to ${x}`); });
  } else if (joy) {
    JOY_B.forEach(x => add('B', x));
    JOY_C_TARGETS.forEach(x => { add('C', `affordable dupe for ${x}`); add('C', `cheaper alternative to ${x}`); });
  } else {
    (ctx.unbranded_queries || []).slice(0, 12).forEach(x => add('B', x));
    while (i.B < 12) add('B', `best ${ctx.category} ${ctx.price_anchor}`);
    (ctx.incumbents || []).slice(0, 6).forEach(x => { add('C', `affordable dupe for ${x}`); add('C', `cheaper alternative to ${x}`); });
  }
  const rivals = skin ? ['The Ordinary', "Paula's Choice"]
    : joy ? ['Judydoll', 'Colorkey'] : (ctx.incumbents || []).slice(0, 2);
  [skin ? 'best affordable skincare brands available in the US'
        : joy ? 'best Chinese makeup brands available in the US'
        : `best ${ctx.category} brands available in the US`,
   (skin || !joy) ? `brands like ${BRAND}` : `C-beauty brands like ${BRAND}`,
   ...rivals.map(x => `${BRAND} vs ${x}`)].forEach(x => add('D', x));
  return q;
}

// OFFICIAL_DOMAINS: every domain the brand actually owns, comma-separated. A brand
// running a second storefront (anua.com + anua.us, medicube.us + medicube.com) has its
// official share understated by exactly the traffic to the unlisted one — measured at
// 13 points for Anua. The primary DOMAIN is always included.
const OFFICIAL_SET = [DOMAIN, ...String(process.env.OFFICIAL_DOMAINS || '')
  .split(',').map(x => x.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, ''))
  .filter(Boolean)].filter((v, i, a) => a.indexOf(v) === i);

function classify(hosts) {
  const official = hosts.filter(h => OFFICIAL_SET.some(d => h === d || h.endsWith(`.${d}`)));
  if (official.length) return { cls: 'official', host: official[0] };
  const RETAIL = ['amazon.com', 'target.com', 'walmart.com', 'ulta.com', 'sephora.com', 'nordstrom.com', 'wayfair.com', 'crateandbarrel.com', 'williams-sonoma.com', 'macys.com', 'bestbuy.com', 'rei.com', 'zappos.com', 'costco.com'];
  const ret = hosts.find(h => RETAIL.some(x => h === x || h.endsWith(`.${x}`)));
  if (ret) return { cls: 'retailer', host: ret };
  if (!hosts.length || (hosts.length === 1 && hosts[0] === REDIR)) return { cls: 'none', host: '' };
  return { cls: 'non_official', host: hosts[0] };
}

(async () => {
  const { collection, products } = await shopifyProducts();
  // HERO_SKUS: ordered substrings picking the brand's actual hero products, mirroring
  // the Joy runner's curated `requested` list. Feeds return alphabetical order, so
  // slice(0,N) would probe obscure SKUs and depress Tier A for reasons unrelated to
  // what we are measuring. Falls back to slice(0,N) when unset.
  const heroSpec = String(process.env.HERO_SKUS || '').split('|').map(x => x.trim()).filter(Boolean);
  let picked = [];
  if (heroSpec.length) {
    for (const want of heroSpec) {
      const hit = products.find(p => p.title.toLowerCase().includes(want.toLowerCase())
        && !picked.some(x => x.title === p.title));
      if (hit) picked.push(hit); else console.log(`  WARN hero not found: ${want}`);
    }
  }
  if (picked.length < NSKU) for (const p of products) { if (picked.length >= NSKU) break; if (!picked.some(x => x.title === p.title)) picked.push(p); }
  // Probe text uses the name a shopper would type: drop merchandising size
  // qualifiers ("Original Size", "(20ml)") that no one includes in a query.
  const cleanTitle = t => String(t)
    .replace(/\s*\((?:\d+\s*(?:ml|g|oz)[^)]*)\)\s*$/i, '')
    .replace(/\s*\b(original|travel|petite|mini|full|deluxe)\s+size\b\s*$/i, '')
    .trim();
  const skus = picked.slice(0, NSKU).map(p => ({ title: cleanTitle(p.title), handle: p.handle, raw_title: p.title }));
  console.log('  hero SKUs: ' + skus.map(x => x.title).join(' | '));
  console.log('  official domains: ' + OFFICIAL_SET.join(', '));
  console.log(`feed ok: ${DOMAIN}/collections/${collection} -> ${products.length} in-stock products; using ${skus.length}`);
  const ctx = await inferContext(products);
  console.log(`category: ${ctx.category} | anchor: ${ctx.price_anchor}`);
  console.log(`incumbents: ${(ctx.incumbents || []).join(', ')}`);
  const queries = buildQueries(ctx, skus);
  console.log(`queries: ${queries.length} (A${queries.filter(q => q.tier === 'A').length} B${queries.filter(q => q.tier === 'B').length} C${queries.filter(q => q.tier === 'C').length} D${queries.filter(q => q.tier === 'D').length}) x ${RUNS} runs = ${queries.length * RUNS} responses`);
  fs.writeFileSync(path.join(OUT, 'run_metadata.json'), JSON.stringify({
    brand: BRAND, domain: DOMAIN, collection, model: MODEL, runs: RUNS,
    context: ctx, queries, started_at: new Date().toISOString(),
    tool_version: 'cohort2_geo/1.0; Gemini 2.5-flash via Vertex+ADC; grounded search; US/USD; redirectors resolved',
  }, null, 2));

  const done = new Set();
  if (fs.existsSync(ROWS)) for (const l of fs.readFileSync(ROWS, 'utf8').split('\n')) { if (!l.trim()) continue; try { const r = JSON.parse(l); done.add(`${r.query_id}|${r.run}`); } catch {} }
  const brandTokens = [norm(BRAND), ...BRAND.toLowerCase().split(/\s+/).map(norm).filter(x => x.length > 3)];
  let n = 0;
  for (let run = 1; run <= RUNS; run += 1) {
    for (const q of queries) {
      const key = `${q.query_id}|${run}`;
      if (done.has(key)) continue;
      let text = '';
      try { text = await gemini(auditInstruction(q.query)); }
      catch (e) { console.log(`  ERR ${key}: ${String(e.message || e).slice(0, 90)}`); await sleep(3000); continue; }
      const rawPath = path.join(RAW, `${q.query_id}_gemini_run${run}.txt`);
      fs.writeFileSync(rawPath, text);
      const prose = text.replace(/<GEO_AUDIT>[\s\S]*?<\/GEO_AUDIT>/i, '');
      const hosts = await resolvedHosts(prose);
      const mentioned = brandTokens.some(t => t && norm(prose).includes(t)) ? 'Y' : 'N';
      const { cls, host: lh } = classify(hosts);
      const row = {
        query_id: q.query_id, tier: q.tier, model: 'gemini', model_id: MODEL, temperature: Number(process.env.TEMP ?? 0), run, brand: BRAND,
        mentioned, link_given: hosts.length ? 'Y' : 'N',
        link_destination: mentioned === 'Y' ? cls : (hosts.length ? cls : 'none'),
        link_domain: lh, all_hosts: hosts, host_count: hosts.length,
        query: q.query, raw_response_path: rawPath,
      };
      fs.appendFileSync(ROWS, JSON.stringify(row) + '\n');
      n += 1;
      if (n % 10 === 0) console.log(`  ${n} responses…`);
      await sleep(700);
    }
  }
  console.log(`\ndone: ${n} new responses -> ${ROWS}`);
})().catch(e => { console.error('FATAL:', e.message); process.exit(1); });
