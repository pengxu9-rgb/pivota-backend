# Step-5 Lane 4 review — 2026-07-10

Per-group verdicts for the judgment residue (multi-domain, spared lane-3,
seed↔first-party twins). Machine-readable copy: `lane4_review_2026-07-10.json`.
This worksheet is the proto gold-label set (ADR-010 action item 5).

## Verdict summary

| Verdict | Groups |
|---|---|
| KEEP_regional_storefronts | 60 |
| KEEP_multi_seller_observation | 54 |
| REVIEW_possible_clone_conservative_spare | 31 |
| REVIEW_junk_copy_vs_variant | 10 |
| KEEP_size_variants | 6 |
| SUPPRESS_seed_twin_pending_signoff | 4 |
| KEEP_title_collision_distinct_products | 3 |
| FIX_URL | 1 |
| KEEP_audit_observation | 1 |

## Reading the verdicts

- **KEEP_multi_seller_observation** (brand+retailer) and **KEEP_regional_storefronts**
  (arencia.jp/us): correct family-key sharing under the ADR-010 two-grain model —
  these are the future resolver / multi-seller buy-box inputs, not duplicates.
- **KEEP_title_collision_distinct_products**: distinct products whose normalized
  brand+title collide (the documented SPU tradeoff); GTIN/URL discriminates downstream.
- **KEEP_size_variants**: distinct sellable sizes; suppressing either loses a product.
- **REVIEW_***: genuinely ambiguous — junk `-copy` rows paired with shade slugs, or
  conservative spares that may still be clones. Low count; revisit opportunistically.
- **SUPPRESS_seed_twin_pending_signoff**: 4 ownist groups matching the existing
  `cross_merchant_redundant_external_seed` precedent, held for sign-off because the
  first-party side is a test-named account (`merch_test_ownist_001`).
- **FIX_URL**: one row carries a vertexaisearch grounding-redirect instead of a PDP URL.

## Groups

### FIX_URL

- `ck_4ab2d5aab1658fd69a1ea5c52211cc5e` (external_seed, 3 rows) — ['bestbuy.com', 'vertexaisearch.cloud.google.com']
### KEEP_audit_observation

- `ck_db609742a340461140793408da201052` (cross, 2 rows) — ['https://anukoofficial.com/product/%EC%95%84%EB%88%84%EC%BD%9', 'https://anukoofficial.com/product/아누코-미니-디탱글-브러쉬/30/']
### KEEP_multi_seller_observation

- `ck_00766907d0ad7a56e1f03e54dbf948f3` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_01f63f94dd920c58fbc72771c1902a0c` (external_seed, 3 rows) — ['bestbuy.com', 'sony.com']
- `ck_020b7bc1543adf402c3544fbe78d1f3e` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_04ede53a3014779686fd18fd02277a01` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_0b3faa8916a03428b00675b36f4f9a42` (external_seed, 3 rows) — ['bestbuy.com', 'samsung.com']
- `ck_0c969710b2a7ba27b8ffd5aec13b743d` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_11efc6ebe08b5368b8d5a4f6d1958320` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_120d20d3f929e52dae563921176cb48c` (external_seed, 3 rows) — ['esteelauder.com', 'ulta.com']
- `ck_12c4b16eed04a766711d91e5a111d6d2` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_1a141552db513817359292a4ff4ad42d` (external_seed, 2 rows) — ['nordstrom.com', 'yslbeautyus.com']
- `ck_1bb81b0e2499ca2d8c9ba58bbb766067` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_1f74136fc6a800e2007507822a87d027` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_230c41b0bf08055c8a7bf04d41d8d341` (external_seed, 2 rows) — ['cosrx.com', 'sokoglam.com']
- `ck_252a70c2d457b3a518f5ac42dee5c19b` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_2ead0707d8ff88bbf0d38519d3d1e3f2` (external_seed, 3 rows) — ['bestbuy.com', 'sennheiser-hearing.com']
- `ck_3759e486e2e62aac95340f2c2fefefd8` (external_seed, 3 rows) — ['apple.com', 'bestbuy.com']
- `ck_3c985e7fac7e37fdaa2a9061737c110a` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_3daf075d8bc589076a6f3be2bac68e53` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_3de700484b6ac13c7964ad7c3dc34fbf` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_46250f773d2ebb50aa3fd83546eaf706` (external_seed, 2 rows) — ['lorealparisusa.com', 'ulta.com']
- `ck_465bfbf2cbf89826c1cf7875eb6134b2` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_5478a79409c09c1eedf79cbb5e72f472` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_55677dc41e173edc0d6028042813a915` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_5b1172242b28fb7f592a6bf23e4d0a9d` (external_seed, 2 rows) — ['esteelauder.com', 'nordstrom.com']
- `ck_606dc9051883cc143e4f393a9ce0fd60` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_685dda60794f3fecfaa1e77b0a9f6a75` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_6ae38af8dde24eb5039588bd0f57289e` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_6f2ce9465a5ae1e754c6fec1156f257a` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_700f2748c78d7d5cfa4a45bf3e5d0886` (external_seed, 3 rows) — ['benefitcosmetics.com', 'ulta.com']
- `ck_71304aeb63fb79bbca0484f3e6d90594` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_7392a953c262d155c1e1d7fbd78419fd` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_7c4f9604663317c4f0e496219b7a8340` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_7e96eee640a6852b140667c4eaac6852` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_8aada760b3c5897021ac9e0749b4f5cc` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_8bf284e6b50d9562cf326fa0187863c9` (external_seed, 4 rows) — ['maybelline.com', 'target.com', 'ulta.com']
- `ck_8fae0d7fcb5fd8dcc97d3ab3ea4c2ad9` (external_seed, 2 rows) — ['maccosmetics.com', 'ulta.com']
- `ck_939480cbc7bca69ca2792d22b2d83bf2` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_a14faa694f176655bc91efbb869a8572` (external_seed, 2 rows) — ['cosrx.com', 'sokoglam.com']
- `ck_a43ce905d6513a9465bc3d3df63585ef` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_a724d81c5c1491d22aa31947d8e18a19` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_a9180f65cfcce0b7fa35caf5733dd511` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_ab43e145163dcbb69a4662bb54f77f77` (external_seed, 4 rows) — ['amzn.to', 'bestbuy.com', 'sony.com']
- `ck_afbfe2fb4a9089d8b6ec1e779ff387f0` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_b75008a97bf9dd3e41854e6eafc3b216` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_bdb52051d1f82fb9d226ff1a5ad5ad66` (external_seed, 3 rows) — ['dior.com', 'ulta.com']
- `ck_c012b7375025d19a81830e441211afc4` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_d9a912570f483872e709234606fe4ba6` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_daa3093bb9b1f08033868762340a6d33` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_dca7de2b4e28a0cfc070582235e0e1f9` (external_seed, 3 rows) — ['bestbuy.com', 'us.kobobooks.com']
- `ck_de1dae962b7c673707948dfaa265a352` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_e50bcc83855c5a345c9dc0b2dbbbcfff` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
- `ck_ea208203030f0a1fe39a0bd72db46a01` (external_seed, 2 rows) — ['hudabeauty.com', 'sephora.com']
- `ck_f4380605d9e05a3abb3d5bccd1cc3d93` (external_seed, 3 rows) — ['bestbuy.com', 'bose.com']
- `ck_f5ce9623546fac62d0a8ea5777adc7fe` (external_seed, 2 rows) — ['theordinary.com', 'ulta.com']
### KEEP_regional_storefronts

- `ck_10a8ab2b19b695b39efb54c013b4c7dd` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_1c0f8e1ea9664b9eeb8558c9053b9324` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_2058bf8fad78c75fa9943c5d36547d11` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_205eecd46ac887246f761c3d6feb9f76` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_2249bc9cfbd6720b58d3240f8ca4891d` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_23ed79d5cd29360e81653577ab9d7ed0` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_247d90efc344e8b218550593ecaa72ef` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_2579545fc7afb0d6e375ecd0ece0329d` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_2a3ac9ecc23d89b6b0d1ea3500469fad` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_3008349189d0700a6cb2572e5e87dd84` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_308435f0a07e69cea31c02518c31fbfe` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_3352fd51b04563e1a19af1ee9bf5639a` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_394ebf7815b72f318744cc53c5302f15` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_3b2901ff67bfc04cf33c7c7db54f7732` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_3da3bd225110125ea276076b8f8c6b8c` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_4422d3c43ed76d3ca9ca3f7553e278d5` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_479bc1b79c926ecced7bf6c57048b0c4` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_4d831d782c92da2e3b1e6f1377937a03` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_55af98167f900b80bb7132d1dcd1f628` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_65ef16486c0d642620d5e7d080ac4928` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_698ddc49670eb31aace9a79426c01f9a` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_6b17321120dc49d1c26f21d8571b1daf` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_6e45a056ac625cfddb336fac7a07d8de` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_70ee3271bdb9e4d92d65564298f7158d` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_748777c5b43c3957cd3e29c99783bd81` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_77a5094009cb66c753b5f7a769a6a770` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_78a025db28e43e404d35c070f8eb3052` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_80138594d4579787d10d1db2315f0d5d` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_897f41994836055346252e7cde997db5` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_8bf562310a73d9fc6b5e8c3e9fffa87b` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_902e6fada28fdd5a1eebdaa43f4a0fe8` (external_seed, 4 rows) — ['arencia.jp', 'arencia.us']
- `ck_914bb89381d1583940ff576b8eaee902` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_91ea0a689b010219f7f14cea1b5cf592` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_9509938241fd783b27646f0f657a9d2c` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_a55cef1a581cb69d6e0ad95e944a87cf` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_a7c5868e030450d82b2d38e65d6062dc` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_a7d3d790c5b7ae4a0de360dea7fb3574` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_a86c41d6ac28b580795e7ee247187092` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_ac7d470cab7868b2fbe0de237604e50f` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_b29b3c9b81e944f8dc5f760bdb5c6203` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_bceff92177b6b327ea3e124d4930728d` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_bd40e6d907c4a5d70476f06c36fe42b9` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_c2442f47ec399bded1620e865bdd91cd` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_c62971e42827642b20a1543fd77fe747` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_d0cd374d5960910ca073b6629fd897d6` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_d19270e9a1aefc6173c031c6c413cf56` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_d19b066bebaead689eb8306ee8ffd797` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_d527c20672e40c86fb95409deeb2d598` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_da7a1d2fd549d5ac6720063538603cb8` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_df4965b9b0356ed787ae8406f5b4f567` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_e0302b9de16817bd7248c7085b45fcc4` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_e1c5f7836e69da127ca4e831c8d71a74` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_e8df3d2b0d8a68a074cec13a50870e18` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_e93d67e3fcadf720bf962dd0e4a4871f` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f1662e7ba056642d1b64cce324e0a797` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f31d6d279778b40beecaa9fd00b2be80` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f497d505eeeba996835383d66338e19c` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f4fb29540c470e8e71c569b25f930c2e` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f761a46d93b46ad040cfb0b58ed9763e` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
- `ck_f8219d4aa2a8ca08a81f3dc15786ce05` (external_seed, 2 rows) — ['arencia.jp', 'arencia.us']
### KEEP_size_variants

- `ck_1ce33d487dedcc50ccf8ae39b97b9c78` (external_seed, 2 rows) — ['huile-prodigieuse-100ml', 'huile-prodigieuse-50ml']
- `ck_36fc604d18829c8cef46f92cf953c2b5` (external_seed, 5 rows) — ['luminant-cream', 'remedy-cream', 'renight-cream', 'renight-cream-10ml', 'sublime-skin-cream']
- `ck_82a155613b1f181570194557d2ea6868` (external_seed, 2 rows) — ['damage-therapy-no-wash-treatment-ex-250ml-8-54-fl-oz-hidden-260527-gm', 'growus-damage-therapy-no-wash-treatment']
- `ck_9a9a086e48840ddb298dd5cfffc773a0` (external_seed, 2 rows) — ['cosmic-kylie-jenner-30ml-pen-spray-gift-set', 'cosmic-kylie-jenner-eau-de-parfum-30ml-10ml-pen-spray-gift-set']
- `ck_b750be54799aacc37372797f3b755328` (external_seed, 2 rows) — ['the-high-shine-shampoo-200ml', 'the-high-shine-shampoo-400ml']
- `ck_e9d1ffdec5b4dd35f1f39392cd6fdc1e` (external_seed, 2 rows) — ['huile-prodigieuse-florale-100ml', 'huile-prodigieuse-florale-50ml']
### KEEP_title_collision_distinct_products

- `ck_32f66dad624e1ed3f75f8f80b5b71038` (external_seed, 13 rows) — ['0627_cm_2544_pp_four_jhp1', '0627_cm_2544_pp_fourimg_jhp1', '0627_cm_4564_pp_four_jhp1', '0627_cm_4564_pp_fourimg_jhp1', '0627_cm_a_pp_four_jhp1', '0627_cm_a_pp_fourimg_jhp1']
- `ck_8a689896699a26bc2a9fb0b6a9fd618f` (external_seed, 2 rows) — ['4901301413246', '4901301447647']
- `ck_e5c5288eb234567b6434909d276a1a57` (external_seed, 2 rows) — ['stilligouttes', 'stilligouttes1']
### REVIEW_junk_copy_vs_variant

- `ck_0039f18aaa1e20c19bddd2bac33326de` (external_seed, 2 rows) — ['super-slick-lip-balm-copy-4', 'super-slick-lip-balm-sunset']
- `ck_1324d6e96e07bd36854ed9e87dee2974` (external_seed, 2 rows) — ['super-slick-mini-lip-balm-blossom', 'super-slick-mini-lip-balm-copy']
- `ck_5bcf8bd9bac9f5c07c46a2448912b7f7` (external_seed, 2 rows) — ['super-slick-lip-balm-cherry', 'super-slick-lip-balm-copy-1']
- `ck_696865ba4e849d1231a219fce1e3ef82` (external_seed, 2 rows) — ['super-slick-lip-balm-copy-3', 'super-slick-lip-balm-sierra']
- `ck_7993f4d4905de687df1808ac1cb35ca2` (external_seed, 4 rows) — ['amazon-nr-1-marke-invisible-collagen-sun-serum-kein-weissschleier-spf-50-pa', 'amazon-nr-1-marke-invisible-collagen-sun-serum-kein-weissschleier-spf-50-pa-1', 'amazon-nr-1-marke-invisible-collagen-sun-serum-kein-weissschleier-spf-50-pa-2', 'amazon-nr-1-marke-kollagen-gel-toner-pads-sanfter-glow-in-nur-15-minuten-copy']
- `ck_846e5cf8f942e71faed78dde9423c2be` (external_seed, 2 rows) — ['super-slick-lip-balm-breeze', 'super-slick-lip-balm-copy']
- `ck_9c4a3372ca6e3aeacf51ee787e219cba` (external_seed, 2 rows) — ['super-slick-mini-lip-balm-breeze', 'super-slick-mini-lip-balm-copy-1']
- `ck_dcd7ee9530134f04233394c413127634` (external_seed, 4 rows) — ['amazon-1-big-spring-sale-biodance-collagen-gel-toner-pads-copy', 'amazon-nr-1-marke-kollagen-gel-toner-pads-sanfter-glow-in-nur-15-minuten', 'amazon-nr-1-marke-kollagen-gel-toner-pads-sanfter-glow-in-nur-15-minuten-1', 'amazon-nr-1-marke-kollagen-gel-toner-pads-sanfter-glow-in-nur-15-minuten-2']
- `ck_ef05bf07aae4148721527a7cfff61bd1` (external_seed, 2 rows) — ['super-slick-lip-balm-copy-2', 'super-slick-lip-balm-dune']
- `ck_f580dc89591fa7b6ef59001f12e2d60e` (external_seed, 2 rows) — ['arencia-fresh-green-mochi-cleanser', 'arencia-fresh-rosehip-mochi-cleanser-copy']
### REVIEW_possible_clone_conservative_spare

- `ck_04aeb130fbd202492a028eefc6bf1480` (external_seed, 2 rows) — ['hydra-melt', 'hydra-melt-dewy-skin-balm']
- `ck_0c994d3725a268985da803cb83905a3d` (external_seed, 2 rows) — ['liquid-fairy-lights', 'liquid-fairy-lights-all-shades']
- `ck_13e6a09bc30dfa2b2e3b6baa760f2a92` (external_seed, 3 rows) — ['brow-duo', 'the-brow-duo-eu']
- `ck_17d5106e1b5c8be5757a5eb2337217a6` (external_seed, 2 rows) — ['red-collection-velvet-blur-mini-lipstick-balm-lava', 'velvet-blur-matte-mini-lipstick-balm-lava']
- `ck_444583b65297ed041793fce468c7ceef` (external_seed, 2 rows) — ['glossier-lash-slick-mascara', 'lash-slick']
- `ck_45fdf8ec41411f570d6a7cc049d65cfa` (external_seed, 4 rows) — ['the-skin-tint-blur-complexion-duo', 'the-skin-tint-blur-complexion-duo-ca', 'the-skin-tint-blur-complexion-duo-gb']
- `ck_47355a47f4338666323091197d2b4872` (external_seed, 3 rows) — ['active-pureness-toner', 'essential-toner', 'remedy-toner']
- `ck_47847d038aa726ec0876ba970b0249ea` (external_seed, 3 rows) — ['body-strategist-oil', 'renight-oil', 'tranquillity-oil']
- `ck_536d0ec9ae348f0bb59766ad994a7fe7` (external_seed, 3 rows) — ['ilia-limitless-lash-mascara', 'limitless-lash-lengthening-mascara']
- `ck_57491440e9dd53a3e9d526b06afa0283` (external_seed, 7 rows) — ['color-duo', 'color-duo-eu', 'color-duo-uk', 'the-color-duo', 'the-color-duo-eu', 'the-color-duo-uk']
- `ck_605f7f7d0ce288d75f1afa38c79a76e6` (external_seed, 4 rows) — ['the-glow-set-1', 'the-glow-set-2', 'the-glow-set-blc']
- `ck_7c5f7ff6b5aaafadb362882a891d98f4` (external_seed, 2 rows) — ['maybelline-the-falsies-lash-lift-mascara', 'the-falsies-lash-lift-washable-mascara-eye-makeup']
- `ck_807277038ecd54b0b21932cbd2242635` (external_seed, 2 rows) — ['tranquillity-blend', 'tranquillity-blend-refill']
- `ck_853727046367309ef6f875cf7d69d57d` (external_seed, 3 rows) — ['pure-hand-cream', 'pure-hand-cream-4', 'pure-hand-cream-b-w']
- `ck_8839882ddf25cab55b180a716de712f2` (external_seed, 2 rows) — ['hydramemory-body-lotion', 'tranquillity-body-lotion']
- `ck_89f7b60c0373063440361150dec26e08` (external_seed, 3 rows) — ['lip-blush-trio-ukeu', 'the-lip-blush-trio']
- `ck_8c5af2d85a23d7ec4bd71858130a40d9` (external_seed, 2 rows) — ['active-pureness-mask', 'renight-mask']
- `ck_8eb60cc13e2cf78686ed2293cf9b3a52` (external_seed, 3 rows) — ['makewaves-mascara', 'makewaves®-mascara-gift-with-purchase']
- `ck_946d3f1a00d51a5c9b823e1b9e5fa4d3` (external_seed, 4 rows) — ['signature-lip', 'signature-lip-uk', 'signature-lipstick-eu']
- `ck_9973df953049b5c6e0795727a6c43b18` (external_seed, 2 rows) — ['divine-rose-ii-collection', 'pat-mcgrath-labs-mothership-viii-divine-rose-ii-palette']
- `ck_9e19fc2cc91db6148d48a7818df46179` (external_seed, 2 rows) — ['rouge-g-the-customizable-ultra-care-lipstick-S000050.html', 'rouge-g-the-customizable-ultra-care-lipstick-S000070.html']
- `ck_ba6334ab1e42312bf0a566fa635ce287` (external_seed, 2 rows) — ['luminant-serum', 'remedy-serum']
- `ck_c16508d15628d445345d12e50b6a68d8` (external_seed, 2 rows) — ['matte-slick-invisible-lip-balm', 'matte-slick-mini-invisible-balm']
- `ck_c7fa43cca080b9bd6de4dbb2ed6303ac` (external_seed, 2 rows) — ['red-collection-velvet-blur-mini-lipstick-balm-windburn', 'velvet-blur-matte-mini-lipstick-balm-windburn']
- `ck_cc25910c4206f15657901ac8c8cb205f` (external_seed, 2 rows) — ['glam-eyeshadow-palette', 'natasha-denona-glam-eyeshadow-palette']
- `ck_cdf9cba4841a9a3308900275ee136848` (external_seed, 5 rows) — ['color-trio', 'the-color-trio', 'the-color-trio-eu', 'the-color-trio-uk']
- `ck_e65ab07927cc46c62b02f442208c702e` (external_seed, 2 rows) — ['mediheal-vitamide-brightening-pad-refill', 'vitamide-brightening-pad-refill']
- `ck_e74ed58e788d22d287ab3a6d105b4339` (external_seed, 3 rows) — ['glossybounce-duo', 'glossybounce-duo-2025']
- `ck_eb9bd29f0e9ee49c90fc1675cb34774b` (external_seed, 2 rows) — ['honey-orange-blossom-hand-cream-1', 'honey-orange-hand-cream']
- `ck_f6043e61c3c4a716704b0e2bdc669296` (external_seed, 2 rows) — ['collagen-ampoule-pad-refill', 'mediheal-collagen-ampoule-pad-refill']
- `ck_ff5b458019ad1fa9409c4b61c786e1e3` (external_seed, 2 rows) — ['honeyed-grapefruit-body-cream', 'honeyed-grapefruit-whipped-body-cream-1']
### SUPPRESS_seed_twin_pending_signoff

- `ck_07993a56a5d6a5704623bdb12a4d68e0` (cross, 2 rows) — ['https://ownist.com/products/triple-collagen-garden-gift-set', 'https://ownist.com/products/triple-collagen-garden-gift-set']
- `ck_207b488833d1d30063ef58ea7492bce8` (cross, 2 rows) — ['https://ownist.com/products/triple-shine-1-box', 'https://ownist.com/products/triple-shine-1-box']
- `ck_6ade2fac142ad3a1cac48c2e7d0540c8` (cross, 2 rows) — ['https://ownist.com/products/triple-shine-garden-gift-set', 'https://ownist.com/products/triple-shine-garden-gift-set']
- `ck_b6c694cf4003f3f2f91a8a10a37ea94f` (cross, 2 rows) — ['https://ownist.com/products/ownist-triple-collagen-1-box', 'https://ownist.com/products/ownist-triple-collagen-1-box']
