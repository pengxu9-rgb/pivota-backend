#!/usr/bin/env node
/*
 * Tier-G Ingredient KB seed loader (ADR-002 item 7).
 *
 * Upserts the curated, adversarially-reviewed ingredient entries in
 * docs/examples/ingredient_kb.*.json into aurora_ingredient_research_kb so the
 * PIVOTA-Agent ingredient-lookup path (src/auroraBff/routes.js ~99088) and the
 * future grounded generator (item 8) read them as status='ready'.
 *
 * Key parity: buildIngredientResearchKbKey / normalizeQueryText are copied
 * VERBATIM from PIVOTA-Agent/src/auroraBff/ingredientResearchKbStore.js so the
 * kb_key matches exactly what consumers compute. Do not "improve" them.
 *
 * Seed entries are the authoritative record: expiry is set 10y out (NOT the 30d
 * on-demand TTL) so they persist and a ready hit short-circuits regeneration.
 *
 * Run (borrows pg from the PIVOTA-Agent checkout; never prints the credential):
 *   cd <this repo>
 *   # Production is Cloud Run (pivota-prod/us-west1); DATABASE_URL is one of the
 *   # few secrets WITHOUT the `env-` prefix. Note its DSN resolves to a PRIVATE
 *   # IP, so this only connects from inside the VPC - from a laptop, point
 *   # DBURL at a database you can actually reach.
 *   DBURL=$(gcloud secrets versions access latest --secret=DATABASE_URL --project pivota-prod)
 *   NODE_PATH=../PIVOTA-Agent/node_modules DATABASE_URL="$DBURL" node scripts/load_ingredient_kb_seed.js
 *
 * Idempotent: ON CONFLICT (kb_key) DO UPDATE. Touches only rows whose kb_key is
 * derived from a seed lookup term. Pass --dry to print plan without writing.
 */
const fs = require('fs')
const path = require('path')
const crypto = require('crypto')

// ---- verbatim from ingredientResearchKbStore.js (key parity) ----
function normalizeLang(lang) {
  const token = String(lang || 'EN').trim().toUpperCase()
  return token === 'CN' ? 'CN' : 'EN'
}
function normalizeQueryText(value) {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw
    .replace(/[^\p{L}\p{N}\s+\-/,().]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 240)
}
function normalizeLayer(value) {
  const token = String(value || '').trim().toLowerCase()
  if (token === 'variant') return 'variant'
  return 'generic'
}
function buildIngredientResearchKbKey({ query, lang, layer = 'generic' } = {}) {
  const q = normalizeQueryText(query)
  const language = normalizeLang(lang)
  const kbLayer = normalizeLayer(layer)
  const normalizedVariantKey = ''
  if (!q) return null
  const hash = crypto.createHash('sha1').update(`${language}:${kbLayer}:${normalizedVariantKey}:${q}`).digest('hex')
  return `ing_research:${language}:${kbLayer}:${normalizedVariantKey}:${hash}`
}
// ---- end verbatim ----

const SEED_DIR = path.join(__dirname, '..', 'docs', 'examples')
const LANG = 'EN'
const TTL_MS = 10 * 365 * 24 * 3600 * 1000
const DRY = process.argv.includes('--dry')

function lookupTerms(profile, slug) {
  const ing = profile.ingredient || {}
  const terms = new Set()
  const add = (s) => {
    const n = normalizeQueryText(s)
    if (n && n.length >= 3) terms.add(n)
  }
  String(ing.inci || '').split(/[/,]/).forEach(add)
  add(String(ing.display_name || '').replace(/\(.*?\)/g, '').replace(/['"]/g, ''))
  ;(Array.isArray(ing.aliases) ? ing.aliases : []).forEach(add)
  add(slug.replace(/_/g, ' '))
  return [...terms]
}

async function main() {
  const files = fs.readdirSync(SEED_DIR).filter((f) => f.startsWith('ingredient_kb.') && f.endsWith('.json')).sort()
  const now = new Date()
  const expires = new Date(now.getTime() + TTL_MS)
  const plan = []
  for (const f of files) {
    const slug = f.replace('ingredient_kb.', '').replace('.json', '')
    const raw = JSON.parse(fs.readFileSync(path.join(SEED_DIR, f), 'utf8'))
    const profile = {}
    for (const k of Object.keys(raw)) if (!k.startsWith('_')) profile[k] = raw[k]
    const terms = lookupTerms(profile, slug)
    const sourceMeta = Object.assign({}, profile.source_meta || {}, {
      seed: true, seed_slug: slug, seed_loaded_at: now.toISOString(),
    })
    plan.push({ slug, terms, grade: (profile.evidence || {}).grade, profileJson: JSON.stringify(profile), smJson: JSON.stringify(sourceMeta) })
  }

  if (DRY) {
    for (const p of plan) console.log(`${p.slug} [grade ${p.grade}] -> ${p.terms.length} keys: ${p.terms.join(', ')}`)
    console.log(`\nDRY: would upsert ${plan.reduce((a, p) => a + p.terms.length, 0)} rows across ${plan.length} actives`)
    return
  }

  const { Pool } = require('pg')
  const pool = new Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false }, max: 4 })
  let total = 0
  const summary = []
  for (const p of plan) {
    let n = 0
    for (const term of p.terms) {
      const kbKey = buildIngredientResearchKbKey({ query: term, lang: LANG, layer: 'generic' })
      if (!kbKey) continue
      await pool.query(
        `INSERT INTO aurora_ingredient_research_kb
           (kb_key, query_norm, lang, kb_layer, variant_key, revision, status, provider, error_code,
            ingredient_profile_json, source_meta, updated_at, expires_at)
         VALUES ($1,$2,$3,'generic','',1,'ready',NULL,NULL,$4::jsonb,$5::jsonb,$6::timestamptz,$7::timestamptz)
         ON CONFLICT (kb_key) DO UPDATE SET
           query_norm=EXCLUDED.query_norm, lang=EXCLUDED.lang, kb_layer=EXCLUDED.kb_layer,
           variant_key=EXCLUDED.variant_key, revision=EXCLUDED.revision, status=EXCLUDED.status,
           provider=EXCLUDED.provider, error_code=EXCLUDED.error_code,
           ingredient_profile_json=EXCLUDED.ingredient_profile_json, source_meta=EXCLUDED.source_meta,
           updated_at=EXCLUDED.updated_at, expires_at=EXCLUDED.expires_at`,
        [kbKey, term, LANG, p.profileJson, p.smJson, now.toISOString(), expires.toISOString()],
      )
      n++; total++
    }
    summary.push(`  ${p.slug} [grade ${p.grade}]: ${n} keys`)
  }
  console.log(summary.join('\n'))
  console.log(`\nupserted ${total} rows across ${plan.length} actives (expires ${expires.toISOString().slice(0, 10)})`)
  await pool.end()
}

main().catch((e) => { console.error('LOAD FAILED:', e.message); process.exit(1) })
