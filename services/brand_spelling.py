"""Brand spelling: the measured brand families and the comparison keys that read them.

Moved here from services/curated_brand_feed.py (RETAILER_BRAND_SPELLINGS, RETAILER_BRAND_CANONICAL,
_brand_key) and services/pdp_matcher/retailer_match.py (match_brand) so that pure matchers
(services/product_match_normalizer.py) can use ONE copy without importing the crawl module (httpx,
crawl pacing) or the pdp_matcher package (its __init__ pulls the LLM lane, databases and config).
Both old modules re-export these names, so every existing import and test keeps working.

This module must stay free of I/O imports: tests/test_product_match_normalizer.py pins that importing
the normalizer loads no httpx / databases / asyncpg / sqlalchemy / config module.
"""

from __future__ import annotations

from typing import Optional

from services.catalog_identity import normalize_brand


def brand_key(value: Optional[str]) -> str:
    """Alphanumeric-only comparison form of a brand/vendor label.

    Tighter than curated_brand_feed's `_vendor_token` (which only casefolds) because the comparison here is
    "are these two labels the same BRAND", and the spellings that must compare equal
    differ by punctuation and spacing: `A'PIEU`/`Apieu`, `MISSHA US`/`Missha`.
    `_vendor_token` is left alone — it backs `filter_products_by_vendor`, where exact
    selection is the point and loose matching is the documented hazard.

    "&" is read as "and" before stripping: us.sandandsky.com publishes both `Sand & Sky US`
    and `Sand and Sky INT` (measured 2026-09-26), which stripped to `sandskyus` and
    `sandandskyint` and so could never be one brand under any job brand.
    """
    return "".join(c for c in str(value or "").casefold().replace("&", "and") if c.isalnum())


# Storefront descriptor tokens some vendors append to the brand name
# ("COSRX Official", "... Flagship") that aren't part of the brand identity.
# Conservative on purpose: dropping "shop"/"beauty"/"store" would corrupt real
# brands ("The Face Shop", "Huda Beauty"). A brand-alias map (services/brand_alias)
# is the general fix; this handles the common storefront-vendor case.
_BRAND_DESCRIPTOR_TOKENS = frozenset({"official", "flagship"})


def match_brand(brand: Optional[str]) -> str:
    """normalize_brand + drop a trailing storefront descriptor. So a Shopify
    vendor "COSRX Official" and a retailer/catalog "COSRX" both key as "cosrx".
    Never strips to empty (keeps at least one token)."""
    toks = normalize_brand(brand).split()
    while len(toks) > 1 and toks[-1] in _BRAND_DESCRIPTOR_TOKENS:
        toks.pop()
    return " ".join(toks)


# MEASURED RENAMES that retailers still sell under the old and new names. In retailer mode a
# vendor is written under the operator's --brand only when the two are the SAME brand; exact
# alphanumeric equality decides that everywhere except here. Containment cannot: it would merge
# "Purito" with "Purito Seoul" (a rename -- correct) and equally "A'PIEU" with "A'PIEU Plus"
# (a distinct vendor -- tests/services/test_retailer_adversarial_acceptance.py pins that it is
# NOT overridden). So each family is listed, with the evidence that it is one brand.
#
# Measured 2026-09-18 over every vendor at eyurs.com, ohlolly.com and sokoglam.com: these are
# the ONLY cross-host spellings that exact equality leaves split.
RETAILER_BRAND_SPELLINGS = {
    # Manyo Factory renamed to ma:nyo: sokoglam "MANYO FACTORY", ohlolly "ma:nyo", eyurs "manyo".
    "manyofactory": "manyo",
    "manyo": "manyo",
    # Purito renamed to Purito Seoul: eyurs "Purito SEOUL", sokoglam "Purito Seoul", ohlolly "Purito".
    "puritoseoul": "purito",
    "purito": "purito",
    # US top-100 wave, measured 2026-09-24 (prod catalog_products + store vendors). A store's spelling
    # of a prestige brand is a different brand key otherwise -- words (Tom Ford / Tom Ford Beauty,
    # Christian Dior / Dior, Yves Saint Laurent / YSL) or ACCENTS, which _brand_key and
    # normalize_brand both keep (perfumania "Lancome" vs the catalog's "Lancôme").
    "tomford": "tomford", "tomfordbeauty": "tomford",                        # tomfordbeauty.com 145 rows
    "dior": "dior", "christiandior": "dior", "diorbeauty": "dior",           # perfumania "Christian Dior"
    "ysl": "ysl", "yslbeauty": "ysl", "yvessaintlaurent": "ysl", "yvessaintlaurentbeauty": "ysl",
    "jomalone": "jomalone", "jomalonelondon": "jomalone",                    # bluemercury "Jo Malone London"
    "lancome": "lancome", "lancôme": "lancome",
    "esteelauder": "esteelauder", "estéelauder": "esteelauder",
    "kiehls": "kiehls", "kiehlssince1851": "kiehls",
    "dolceandgabbana": "dolcegabbana", "dolcegabbana": "dolcegabbana",
    "lorealparis": "lorealparis", "loréalparis": "lorealparis",
    "tresemme": "tresemme", "tresemmé": "tresemme",
    "avene": "avene", "avène": "avene",                                      # bluemercury Avène, bigelow Avene
    "kerastase": "kerastase", "kérastase": "kerastase",
    # Measured 2026-09-24 over every vendor at 400+ Japanese/Australian beauty storefronts (JP/AU
    # coverage census): the same maker spelt with and without its accent. `_brand_key` keeps accented
    # letters (isalnum), and so does normalize_brand, so each spelling below was a separate brand
    # identity -- e.g. "Kose" at ichibanm.com (26), goodsania.com (185) vs "Kosé" at japanesetaste.com
    # (47), japanwithlovestore.com (93). Generic-word names that happen to split the same way
    # ("Elegance"/"Elégance", "Naive"/"Naïve") are deliberately NOT listed: an unrelated vendor
    # called "Elegance" would be relabelled.
    "kose": "kose", "kosé": "kose",
    "biore": "biore", "bioré": "biore",
    "curel": "curel", "curél": "curel",
    # Decorté was "Cosme Decorté" until 2020: a rename sold under both names (sasa.com "Cosme Decorte",
    # wafuu.com "COSME DECORTÉ", ichibanm.com "Decorte", decortecosmetics.com "Decorté").
    "decorte": "decorte", "decorté": "decorte", "cosmedecorte": "decorte", "cosmedecorté": "decorte",
    # sasa.com and nanamall.com shorten it to "Cle de Peau" / "Clé de Peau".
    "cledepeaubeaute": "cledepeaubeaute", "clédepeaubeauté": "cledepeaubeaute",
    "cledepeau": "cledepeaubeaute", "clédepeau": "cledepeaubeaute",
    "naturaglace": "naturaglace", "naturaglacé": "naturaglace",
    "visee": "visee", "visée": "visee",
    "fiancee": "fiancee", "fiancée": "fiancee",
    # lamourlife.com writes the Roman-numeral character "SK-Ⅱ" (U+2161), which casefolds to "ⅱ".
    "skii": "skii", "skⅱ": "skii",
    # AU: the brand's own store says "Lük Beautifood"; five US retailers say "Luk Beautifood".
    "lukbeautifood": "lukbeautifood", "lükbeautifood": "lukbeautifood",
    # wet n wild's own store (www.wetnwildbeauty.com, read 2026-09-26) files 500 of its 580 products
    # under the CORPORATE vendor "Markwins WNW" (Markwins Beauty Brands owns wet n wild; WNW = wet n
    # wild) and the other 80 under "wet n wild Beauty". Without this family a brand_official crawl
    # branded 500 products "Markwins WNW" and the domain review held the job (the store's name does
    # not start with "Markwins WNW"). Markwins' OTHER labels (e.g. "Markwins Physicians Formula") are
    # deliberately not listed: only the WNW code names this brand.
    "wetnwild": "wetnwild", "wetnwildbeauty": "wetnwild", "markwinswnw": "wetnwild",
    # ETUDE HOUSE renamed to ETUDE (2023) and retailers sell under both, measured in prod 2026-09-28: 293 retailer
    # rows "ETUDE HOUSE" (luxiface.com 129, holiholic.com 106, openthebeauty.com 28, ...) against 184 "ETUDE" /
    # "Etude" (dodoskin.com 77, moidaus.com 33, kbeautymakeup.com 24, ...) -- one product at two retailers never
    # grouped. The existing rows were relabelled FIRST (scripts/relabel_retailer_brand.py, run relabel_07651af59606,
    # 2026-09-29: 303 rows, 0 held; a re-crawl never updates a stored brand) -- this family must not reach the drain
    # while an old-spelling row is live, or intake re-keys that row into the new key's group and apply refuses it.
    "etudehouse": "etude", "etude": "etude",
    # _brand_key keeps accents (isalnum), so the accented spellings are keys of their own (review of #2440).
    "étudehouse": "etude", "étude": "etude",
    # K-beauty / US wave, measured in prod 2026-09-30 over the 13,337 rows ingested 09-23..09-29: each brand
    # below was stored under two or three spellings that differ ONLY in case (normalize_brand lowercases, so
    # every spelling of one brand already shares a content_key -- these families move no identity, and so,
    # unlike ETUDE, may reach the drain before the old rows are relabelled). The vendor's case decided the
    # display brand: dodoskin.com/holiholic.com "MISSHA" (413 retailer rows) vs misshaus.com "Missha" (149);
    # eyurs.com "ROUND LAB" vs roundlab.com "Round Lab"; skin1004.com itself writes both "SKIN1004" and
    # "Skin1004"; cocomo.sg "ANUA"/"ISNTREE"/"GENABELLE"/"MOLVANY"/"ARENCIA". "Centellian 24" (one merchant
    # row, not a crawl lane) splits by a space; the crawl lanes only ever wrote "Centellian24"/"CENTELLIAN24",
    # so that family moves no stored crawl row's identity either.
    # NOT here yet, because they DO move identity (see services/brand_relabel.py ORDER): "O HUI (오휘)" /
    # "Jung Saem Mool" (smkoreabeauty.com retailer rows -- relabel first), "APIEU" (misshaus.com brand rows --
    # the product_key moves too), "Supergoop" (supergoop.com brand rows -- no relabel tool reaches them).
    "missha": "missha", "mixsoon": "mixsoon", "roundlab": "roundlab", "anua": "anua", "arencia": "arencia",
    "skin1004": "skin1004", "medicube": "medicube", "beautyofjoseon": "beautyofjoseon",
    "theordinary": "theordinary", "isntree": "isntree", "centellian24": "centellian24",
    "makeupforever": "makeupforever", "genabelle": "genabelle", "maybelline": "maybelline",
    "bobbibrown": "bobbibrown", "molvany": "molvany", "byterry": "byterry",
    # Identity-changing, added only AFTER its rows were relabelled (services/brand_relabel.py ORDER): smkoreabeauty.com
    # wrote "Jung Saem Mool" (5 retailer rows; normalize_brand keeps the spaces, so its content_key differed from
    # jsmbeauty.sg's "JUNGSAEMMOOL"). Relabelled by scripts/relabel_retailer_brand.py --family jungsaemmool (run id
    # in this change's PR); a re-crawl now writes "JUNGSAEMMOOL" onto the relabelled key instead of the old one.
    "jungsaemmool": "jungsaemmool",
    # Same ORDER: smkoreabeauty.com wrote "O HUI (오휘)" on 20 retailer rows (_brand_key keeps the Hangul, so its key
    # differs from "O HUI"). 18 were relabelled (relabel_retailer_brand.py --family ohui, run relabel_fad2cefd5223);
    # the 2 the relabel held -- a 2-piece and a 3-piece Extreme White set sharing one GTIN, hence one content_key --
    # were withdrawn first (withdraw_catalog_rows.py, reason ohui_set_gtin_collision). Neither had a product group,
    # so a re-crawl that re-keys them cannot trip _ensure_primary_retailer_group.
    "ohui": "ohui", "ohui오휘": "ohui",
}
# The spelling each family is WRITTEN as -- in retailer AND brand-official mode, with or without
# --brand. content_key is
# built from normalize_brand(brand), which keeps punctuation ("ma:nyo" != "manyo"), so writing
# the operator's string would still split a family whenever two runs spelled --brand
# differently. The canonical spelling is the one the catalog ALREADY carries, measured in prod
# 2026-09-18: 78 brand-official rows as "Ma:nyo" (manyo.us), and "Purito SEOUL"
# (purito-seoul.com). Any other spelling would split retailer rows from those.
RETAILER_BRAND_CANONICAL = {
    "manyo": "Ma:nyo",
    "purito": "Purito SEOUL",
    # The catalog's own spelling, measured 2026-09-24 (rows): Tom Ford Beauty 145 (vs "Tom Ford" 4),
    # Dior 23, YSL 6, Jo Malone 4, Lancôme 12, Estée Lauder 14 (vs "Estee Lauder" 2), Kiehl's Since
    # 1851 10, Dolce and Gabbana 38 (vs "Dolce & Gabbana" 1), L'Oreal Paris 12. No rows yet for
    # TRESemmé, Avène, Kérastase: the brand's own spelling.
    "tomford": "Tom Ford Beauty",
    "dior": "Dior",
    "ysl": "YSL",
    "jomalone": "Jo Malone",
    "lancome": "Lancôme",
    "esteelauder": "Estée Lauder",
    "kiehls": "Kiehl's Since 1851",
    "dolcegabbana": "Dolce and Gabbana",
    "lorealparis": "L'Oreal Paris",
    "tresemme": "TRESemmé",
    "avene": "Avène",
    "kerastase": "Kérastase",
    # 2026-09-24: prod carried none of these as a brand (only two seed-mirror rows, "Biore UV" and
    # "Clé de Peau"), so each family is written as the maker's own spelling on its own storefront.
    "kose": "Kosé",
    "biore": "Bioré",
    "curel": "Curél",
    "decorte": "Decorté",
    "cledepeaubeaute": "Clé de Peau Beauté",
    "naturaglace": "Naturaglacé",
    "visee": "Visée",
    "fiancee": "Fiancée",
    "skii": "SK-II",
    # The brand's own lowercase spelling, as on wetnwildbeauty.com.
    "wetnwild": "wet n wild",
    "lukbeautifood": "Lük Beautifood",
    # The brand's own current name (etude.com). Every family member is relabelled to it (see the spellings note).
    "etude": "ETUDE",
    # 2026-09-30 wave. Rule: the spelling the catalog already carries on the rows the retailer relabel tool
    # cannot reach (brand-official / merchant rows, not ext:retailer:), so repairing the existing rows is the
    # reviewed relabel of retailer rows alone. Rows per spelling outside ext:retailer: in parentheses. It
    # agrees with the brand's own styling where that is clear (SKIN1004, The Ordinary, Beauty of Joseon).
    "missha": "Missha",                     # misshaus.com 149 (vs "MISSHA" 0; 413 retailer rows)
    "mixsoon": "Mixsoon",                   # 225 (vs "MIXSOON" 4, mixsoon.us)
    "roundlab": "Round Lab",                # roundlab.com 88 (vs "ROUND LAB" 42)
    "anua": "Anua",                         # anua.com/anua.us 86 (vs "ANUA" 6, all suppressed)
    "arencia": "Arencia",                   # 126 (vs "ARENCIA" 21)
    "skin1004": "SKIN1004",                 # skin1004.com 93 (vs "Skin1004" 48)
    "medicube": "MEDICUBE",                 # 66 (vs "Medicube" 17, medicube.us)
    "beautyofjoseon": "Beauty of Joseon",   # beautyofjoseon.com 103 (vs "BEAUTY OF JOSEON" 2, suppressed)
    "theordinary": "The Ordinary",          # 109 (vs "the ordinary" 5)
    "isntree": "Isntree",                   # isntree-global.com 58 (vs "ISNTREE" 5, suppressed)
    "centellian24": "Centellian24",         # centellian24usa.com 106 (vs "Centellian 24" 1, "CENTELLIAN24" 1)
    "makeupforever": "MAKE UP FOR EVER",    # makeupforever.sg 74 (vs "Make Up For Ever" 0; 4 retailer rows)
    "genabelle": "Genabelle",               # 31 (vs "GENABELLE" 8, suppressed)
    "maybelline": "Maybelline",             # 25 (vs "MAYBELLINE" 0; 12 retailer rows)
    "bobbibrown": "Bobbi Brown",            # 9 (vs "BOBBI BROWN" 0; 17 retailer rows)
    "molvany": "Molvany",                   # 10 (vs "MOLVANY" 4, suppressed)
    "byterry": "BY TERRY",                  # retailer rows only (11 vs "By Terry" 4): the brand's own styling
    "jungsaemmool": "JUNGSAEMMOOL",         # jsmbeauty.sg 171 (vs smkoreabeauty.com "Jung Saem Mool" 5, relabelled)
    "ohui": "O HUI",                        # 409 retailer rows (vs smkoreabeauty.com "O HUI (오휘)" 20: 18 relabelled, 2 withdrawn)
}
