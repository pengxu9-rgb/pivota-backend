'use strict';
// Runs the GATEWAY'S OWN search gates on each case's full and stripped product and prints every decision
// as JSON. Driven by scripts/ops/seed_data_gateway_gate_equivalence.py; see its docstring.
//
//   node seed_data_gateway_gate_eval.cjs <gateway-dir> <cases.json>
//
// <gateway-dir> is a PIVOTA-Agent tree (e.g. a `git archive` export of a pinned commit with its
// node_modules available). Nothing here touches the network; run it under a loopback guard
// (NODE_OPTIONS=--require <guard>) when in doubt.
const path = require('path');
const fs = require('fs');

const [gatewayDir, casesPath] = process.argv.slice(2);
if (!gatewayDir || !casesPath) {
  console.error('usage: node seed_data_gateway_gate_eval.cjs <gateway-dir> <cases.json>');
  process.exit(2);
}

const app = require(path.join(gatewayDir, 'src/server.js'));
const d = app._debug;
const strictIngredient = require(path.join(gatewayDir, 'src/findProductsMulti/strictIngredientQuery.js'));
const relevance = require(path.join(gatewayDir, 'src/services/beautyRelevanceGate.js'));

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

function attempt(fn) {
  try {
    const value = fn();
    return value === undefined ? null : value;
  } catch (err) {
    return { error: String((err && err.message) || err) };
  }
}

function decisions(product) {
  const out = {
    retinoid_for_safety: attempt(() => d.beautyProductIsRetinoidForSafety(product)),
    retinoid_stand_in_only: attempt(() => d.beautyProductRetinoidMentionIsStandInOnly(product)),
    known_unservable: attempt(() => d.isKnownUnservableSearchProduct(product)),
    canonical_price: attempt(() => d.resolveCanonicalSearchProductPrice(product)),
    ingredient_source_text: attempt(() => d.buildBeautyIngredientSourceText(product)),
    category_path_text: attempt(() => relevance.getProductCategoryPathText(product)),
    x_free_retinol_kept: attempt(
      () => strictIngredient.excludeProductsNamingIngredients([product], { ingredientIds: ['retinol'] }).products.length,
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

const cases = JSON.parse(fs.readFileSync(casesPath, 'utf8'));
const result = {};
for (const [name, c] of Object.entries(cases)) {
  result[name] = { full: decisions(c.full), stripped: decisions(c.stripped) };
}
process.stdout.write(`${JSON.stringify(result)}\n`);
process.exit(0);
