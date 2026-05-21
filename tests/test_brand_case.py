from services.text_normalization.brand_case import proper_case_brand


class TestProperCaseBrand:
    def test_titlecases_fully_lowercase(self):
        assert proper_case_brand("fenty beauty") == "Fenty Beauty"
        assert proper_case_brand("huda beauty") == "Huda Beauty"
        assert proper_case_brand("rare beauty") == "Rare Beauty"

    def test_leaves_already_cased_untouched(self):
        assert proper_case_brand("Fenty Beauty") == "Fenty Beauty"
        assert proper_case_brand("ColourPop") == "ColourPop"
        assert proper_case_brand("L'Oréal Paris") == "L'Oréal Paris"

    def test_leaves_all_caps_untouched(self):
        assert proper_case_brand("NARS") == "NARS"
        assert proper_case_brand("MAC") == "MAC"
        assert proper_case_brand("GLAMGLOW") == "GLAMGLOW"

    def test_allow_list_overrides(self):
        assert proper_case_brand("colourpop") == "ColourPop"
        assert proper_case_brand("kvd beauty") == "KVD Beauty"
        assert proper_case_brand("kvd vegan beauty") == "KVD Vegan Beauty"
        assert proper_case_brand("mac") == "MAC"
        assert proper_case_brand("nars") == "NARS"

    def test_whitespace_and_empty(self):
        assert proper_case_brand("") == ""
        assert proper_case_brand(None) == ""
        assert proper_case_brand("   ") == ""
        assert proper_case_brand("  fenty beauty  ") == "Fenty Beauty"

    def test_single_word_lowercase(self):
        assert proper_case_brand("glossier") == "Glossier"
        assert proper_case_brand("sephora") == "Sephora"
