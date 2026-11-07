'use strict';
// Runs the GATEWAY'S OWN search-hop readers on each case's full and stripped product and prints every
// result as JSON. Driven by scripts/ops/seed_data_gateway_gate_equivalence.py; see its docstring.
//
//   node seed_data_gateway_gate_eval.cjs <gateway-dir> <cases.json> <out.json>
//
// <gateway-dir> is a PIVOTA-Agent tree (e.g. a `git archive` export of a pinned commit with its
// node_modules available). The tree is NOT modified: readers server.js does not export are reached by
// appending an export line to server.js's source IN MEMORY as Node loads it. Nothing here touches the
// network; run it under a loopback guard (--require <guard>) when in doubt.
const path = require('path');
const fs = require('fs');
const Module = require('module');

const [gatewayDir, casesPath, outPath] = process.argv.slice(2);
if (!gatewayDir || !casesPath || !outPath) {
  console.error('usage: node seed_data_gateway_gate_eval.cjs <gateway-dir> <cases.json> <out.json>');
  process.exit(2);
}

// server.js functions the search hop runs on backend products but does not export on `_debug`.
const UNEXPORTED_READERS = [
  'beautyRetinoidProductReading',
  'buildBeautyProductRawSafetyText',
  'buildBeautyProductWideRawSafetyText',
  'buildBeautyProductSafetyText',
  'productLooksLikeFragranceMerchandise',
  'firstSearchProductCategoryPath',
  'hasOfferProductTransactionHold',
  'hasSearchSourceUnavailableContract',
  'buildBeautyProductFitAttributesForResponse',
  'buildBeautyProductLocalAuthorityForResponse',
  'firstSearchProductImageUrl',
  'readExternalSeedRichVariants',
  'isWeakPregnancyAntiAgingDisplayCandidate',
];
const serverPath = path.join(gatewayDir, 'src', 'server.js');
const loadJs = Module._extensions['.js'];
Module._extensions['.js'] = function load(module, filename) {
  if (filename !== serverPath) return loadJs(module, filename);
  const exportsLine = UNEXPORTED_READERS.map((n) => `...(typeof ${n} === 'function' ? { ${n} } : {})`).join(', ');
  const source = `${fs.readFileSync(filename, 'utf8')}\n;module.exports.__seedDataGateEval = { ${exportsLine} };\n`;
  return module._compile(source, filename);
};

const app = require(serverPath);
const d = app._debug;
const r = app.__seedDataGateEval || {};
const missing = UNEXPORTED_READERS.filter((n) => typeof r[n] !== 'function');
const strictIngredient = require(path.join(gatewayDir, 'src/findProductsMulti/strictIngredientQuery.js'));
const relevance = require(path.join(gatewayDir, 'src/services/beautyRelevanceGate.js'));
const variantResolver = require(path.join(gatewayDir, 'src/services/shopifyVariantResolver.js'));
const handoff = require(path.join(gatewayDir, 'src/services/checkoutHandoffResolver.js'));
const pdp = require(path.join(gatewayDir, 'src/pdpBuilder.js'));
const livePrice = require(path.join(gatewayDir, 'src/services/liveMerchantSearchPrice.js'));

const contract = (hard) => ({
  contract_version: 'search_quality_contract_v1',
  target_domain: 'beauty',
  query_class: 'need_solution',
  hard_constraints: hard,
});
const HARD_CONSTRAINTS = {
  none: {},
  retinoid_forward: { exclusions: ['retinoid_forward'] },
  fragrance_free: { fragrance_free_skincare: true },
  category_skincare: { category_path_prefix: 'beauty/skincare' },
  category_fragrance: { category_path_prefix: 'beauty/fragrance' },
  category_lips: { category_path_prefix: 'beauty/makeup/lips' },
  brand: { brand: 'Example Brand' },
  strict_lipstick: { strict_lipstick: true },
  strict_shave: { strict_shave: true },
};
const SAFETY_QUERIES = [
  'pregnancy safe night serum',
  'retinol free night cream',
  'serum for my 9 year old',
  'I take isotretinoin, which moisturizer',
];

const clone = (o) => JSON.parse(JSON.stringify(o ?? null));
function attempt(fn) {
  try {
    const value = fn();
    return value === undefined ? null : value;
  } catch (err) {
    return { error: String((err && err.message) || err) };
  }
}

// TEXT readers: their output contains the product's text, so the full product's carries the denied
// internal strings too. They are compared against the full product with exactly the denied keys deleted.
function texts(product) {
  return {
    raw_text: attempt(() => r.buildBeautyProductRawSafetyText(product)),
    wide_text: attempt(() => r.buildBeautyProductWideRawSafetyText(product)),
    safety_text: attempt(() => r.buildBeautyProductSafetyText(product, '')),
    ingredient_source_text: attempt(() => d.buildBeautyIngredientSourceText(product)),
    rich_variants: attempt(() => r.readExternalSeedRichVariants(product)),
  };
}

// DECISIONS: compared against the full product as stored.
function decisions(product) {
  const out = {
    retinoid_for_safety: attempt(() => d.beautyProductIsRetinoidForSafety(product)),
    retinoid_stand_in_only: attempt(() => d.beautyProductRetinoidMentionIsStandInOnly(product)),
    retinoid_reading: attempt(() => r.beautyRetinoidProductReading(product)),
    fragrance_merchandise: attempt(() => r.productLooksLikeFragranceMerchandise(product)),
    first_category_path: attempt(() => r.firstSearchProductCategoryPath(product)),
    category_path_text: attempt(() => relevance.getProductCategoryPathText(product)),
    transaction_hold: attempt(() => r.hasOfferProductTransactionHold(product)),
    source_unavailable: attempt(() => r.hasSearchSourceUnavailableContract(product)),
    known_unservable: attempt(() => d.isKnownUnservableSearchProduct(product)),
    availability_contract_kept: attempt(
      () => (d.enforceFindProductsMultiAvailabilityContract({ products: [clone(product)] }).products || []).length,
    ),
    fit_attributes: attempt(() => r.buildBeautyProductFitAttributesForResponse(product)),
    local_authority: attempt(() => r.buildBeautyProductLocalAuthorityForResponse(product)),
    first_image: attempt(() => r.firstSearchProductImageUrl(product)),
    images: attempt(() => d.normalizeProductImages(clone(product))),
    rich_pdp: attempt(() => d.hasExternalSeedRichPdpContent(product)),
    weak_pregnancy: attempt(() => r.isWeakPregnancyAntiAgingDisplayCandidate(product, 'pregnancy safe firming serum')),
    canonical_price: attempt(() => d.resolveCanonicalSearchProductPrice(product)),
    seed_variant_gid: attempt(() => variantResolver.resolveVariantFromSeed(product.seed_data, { preferAvailable: true })),
    warm_handoff_gid: attempt(() => handoff.deriveWarmHandoffVariantGid({}, product, null)),
    pdp_redirect_url: attempt(() => pdp.resolveProductExternalRedirectUrl(product)),
    pdp_template: attempt(() => pdp.detectTemplateHint(product)),
    pdp_schema: attempt(() => pdp.resolvePdpSchemaProfile(product)),
    live_price_target: attempt(() => livePrice.targetOf(product)),
    similar_projection: attempt(() => {
      const x = d.buildSimilarCatalogProductProjection(clone(product), {});
      return x && { destination_url: x.destination_url, canonical_url: x.canonical_url };
    }),
    x_free_kept: attempt(
      () => strictIngredient.excludeProductsNamingIngredients([product], {
        ingredientIds: ['retinol', 'fragrance', 'salicylic_acid'],
      }).products.length,
    ),
  };
  for (const [name, hard] of Object.entries(HARD_CONSTRAINTS)) {
    out[`hard_${name}`] = attempt(
      () => d.getSearchQualityContractHardConstraintResult(product, contract(hard), 'night serum').reasons,
    );
  }
  for (const query of SAFETY_QUERIES) {
    out[`net_kept:${query}`] = attempt(() => {
      const classes = d.resolveBeautyRetinoidSafetyClasses(query);
      return { classes, kept: (d.applyRetinoidSafetyNetToSearchBody({ products: [product] }, classes).products || []).length };
    });
  }
  return out;
}

function withoutKeys(value, keys) {
  if (Array.isArray(value)) return value.map((v) => withoutKeys(v, keys));
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) if (!keys.has(k)) out[k] = withoutKeys(v, keys);
    return out;
  }
  return value;
}

const payload = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const deniedKeys = new Set(payload.denied_keys || []);
const result = { missing_readers: missing, cases: {} };
for (const [name, c] of Object.entries(payload.cases)) {
  const fullMinusDenied = { ...c.full, seed_data: withoutKeys(c.full.seed_data, deniedKeys) };
  result.cases[name] = {
    full: decisions(c.full),
    stripped: decisions(c.stripped),
    full_texts: texts(fullMinusDenied),
    stripped_texts: texts(c.stripped),
  };
}
// A FILE, written synchronously: a large stdout write to a pipe is asynchronous, and the exit below
// would truncate it.
fs.writeFileSync(outPath, JSON.stringify(result));
process.exit(0);
