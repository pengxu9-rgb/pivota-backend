"""
Configuration settings for Pivota Infrastructure
"""
import os
from typing import Optional
from urllib.parse import urlparse
from pydantic_settings import BaseSettings

DEFAULT_PUBLIC_API_BASE_URL = "https://api.pivota.cc"
LEGACY_PUBLIC_API_HOSTS = {
    "web-production-fedb.up.railway.app",
    "pivota-backend-production.up.railway.app",
}


def _normalize_public_url(value: Optional[str]) -> str:
    return str(value or "").strip().rstrip("/")


def _is_local_public_host(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "0.0.0.0", "localhost"}


def _is_legacy_public_api_host(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").strip().lower()
    except Exception:
        return False
    return host in LEGACY_PUBLIC_API_HOSTS


def resolve_public_api_base_url() -> str:
    for candidate in (
        os.getenv("PUBLIC_API_BASE_URL"),
        os.getenv("PUBLIC_BASE_URL"),
        os.getenv("APP_URL"),
        os.getenv("BASE_URL"),
        DEFAULT_PUBLIC_API_BASE_URL,
    ):
        normalized = _normalize_public_url(candidate)
        if (
            normalized
            and not _is_local_public_host(normalized)
            and not _is_legacy_public_api_host(normalized)
        ):
            return normalized
    return DEFAULT_PUBLIC_API_BASE_URL

class Settings(BaseSettings):
    """Application settings"""
    
    # Database
    # PostgreSQL only - no SQLite fallback
    database_url: str = os.getenv("DATABASE_URL", "")
    
    # Redis (optional, for shared rate limiting)
    redis_url: Optional[str] = os.getenv("REDIS_URL")

    # Rate limiting
    rate_limit_rpm: int = int(os.getenv("RATE_LIMIT_RPM", "1000"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    
    # API Keys
    stripe_secret_key: Optional[str] = os.getenv("STRIPE_SECRET_KEY")
    stripe_webhook_secret: Optional[str] = os.getenv("STRIPE_WEBHOOK_SECRET")
    stripe_billing_webhook_secret: Optional[str] = os.getenv("STRIPE_BILLING_WEBHOOK_SECRET")
    
    adyen_api_key: Optional[str] = os.getenv("ADYEN_API_KEY")
    adyen_merchant_account: Optional[str] = os.getenv("ADYEN_MERCHANT_ACCOUNT", "WoopayECOM")
    adyen_webhook_secret: Optional[str] = os.getenv("ADYEN_WEBHOOK_SECRET")
    adyen_webhook_username: Optional[str] = os.getenv("ADYEN_WEBHOOK_USERNAME", "adyen_webhook_user")
    adyen_webhook_password: Optional[str] = os.getenv("ADYEN_WEBHOOK_PASSWORD")
    
    # Shopify
    shopify_access_token: Optional[str] = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shopify_store_url: Optional[str] = os.getenv("SHOPIFY_STORE_URL")
    shopify_client_id: Optional[str] = os.getenv("SHOPIFY_CLIENT_ID")
    shopify_client_secret: Optional[str] = os.getenv("SHOPIFY_CLIENT_SECRET")
    shopify_redirect_uri: Optional[str] = os.getenv("SHOPIFY_REDIRECT_URI")
    # Signing key for no-login install links (one-time signed tokens).
    # If empty, we fall back to JWT_SECRET_KEY (not ideal, but keeps deployments working).
    shopify_install_link_signing_key: Optional[str] = os.getenv("SHOPIFY_INSTALL_LINK_SIGNING_KEY")
    # Needed for: product sync + creating manual sale/refund records on orders
    # + read-only Shopify discount node sync.
    # Note: webhook subscription requires `write_webhooks`.
    shopify_scopes: str = os.getenv(
        "SHOPIFY_SCOPES",
        "read_products,read_orders,read_fulfillments,write_orders,write_webhooks,read_discounts",
    )
    # App Store (public) distribution scope set: read-only sync + optimization +
    # AI-readiness. MUST exactly match the scopes declared in shopify.app.toml
    # ([access_scopes] read_discounts,read_fulfillments,read_orders,read_products).
    # Requesting any scope NOT declared in the app config (e.g. write_webhooks)
    # makes Shopify reject the OAuth authorize request with a 400, which fails the
    # "authenticates after install" automated review check. Compliance/GDPR
    # webhooks are declared in the toml and managed by Shopify, so they do NOT
    # need write_webhooks here. Deliberately EXCLUDES write_orders too, so the
    # public listing stays a read-only merchant tool. The full `shopify_scopes`
    # (with write_orders + write_webhooks) stays for custom/headless installs.
    shopify_appstore_scopes: str = os.getenv(
        "SHOPIFY_APPSTORE_SCOPES",
        "read_products,read_orders,read_fulfillments,read_discounts",
    )

    # --- Dual Shopify app credentials -------------------------------------
    # App A = "Pivota" (public / App Store distribution): read-only merchant tool.
    # App B = "Pivota Merchant" (custom / headless): keeps write_orders.
    # All default to the existing single creds, so behavior is unchanged until
    # the SHOPIFY_HEADLESS_* envs are set and App B is repointed.
    shopify_appstore_client_id: Optional[str] = os.getenv(
        "SHOPIFY_APPSTORE_CLIENT_ID", os.getenv("SHOPIFY_CLIENT_ID")
    )
    shopify_appstore_client_secret: Optional[str] = os.getenv(
        "SHOPIFY_APPSTORE_CLIENT_SECRET", os.getenv("SHOPIFY_CLIENT_SECRET")
    )
    shopify_appstore_redirect_uri: Optional[str] = os.getenv(
        "SHOPIFY_APPSTORE_REDIRECT_URI", os.getenv("SHOPIFY_REDIRECT_URI")
    )
    shopify_headless_client_id: Optional[str] = os.getenv(
        "SHOPIFY_HEADLESS_CLIENT_ID", os.getenv("SHOPIFY_CLIENT_ID")
    )
    shopify_headless_client_secret: Optional[str] = os.getenv(
        "SHOPIFY_HEADLESS_CLIENT_SECRET", os.getenv("SHOPIFY_CLIENT_SECRET")
    )
    shopify_headless_redirect_uri: Optional[str] = os.getenv(
        "SHOPIFY_HEADLESS_REDIRECT_URI", os.getenv("SHOPIFY_REDIRECT_URI")
    )
    shopify_headless_scopes: str = os.getenv(
        "SHOPIFY_HEADLESS_SCOPES",
        "read_products,read_orders,read_fulfillments,read_discounts,write_webhooks,write_orders",
    )

    # Wix
    wix_api_key: Optional[str] = os.getenv("WIX_API_KEY")
    wix_store_url: Optional[str] = os.getenv("WIX_STORE_URL")

    # Checkout.com
    checkout_success_url: str = os.getenv("CHECKOUT_SUCCESS_URL", "https://agents.pivota.cc/checkout/success")
    checkout_cancel_url: str = os.getenv("CHECKOUT_CANCEL_URL", "https://agents.pivota.cc/checkout/cancel")
    
    # Metrics feature flag
    metrics_query_version: str = os.getenv("METRICS_QUERY_VERSION", "legacy")  # legacy | new
    partner_rev_share_use_v2: bool = (
        os.getenv("PARTNER_REV_SHARE_USE_V2", "false").lower() == "true"
    )

    # Nightly backfill guard
    enable_nightly_psp_id_backfill: bool = os.getenv("ENABLE_NIGHTLY_PSP_ID_BACKFILL", "false").lower() == "true"
    
    # Platform Onboarding v2 (EPIC-1/2/3)
    platform_onboarding_v2_enabled: bool = os.getenv("FEATURE_PLATFORM_ONBOARDING_V2", "false").lower() == "true"
    
    # Agent Center V1 (shared runtime for Demand Test / SKU Match / Offer
    # Execution / Checkout Verification / GMV Attribution agents).
    # state_backend is reserved for a future swap from "db" to a separate
    # store; "db" is the only supported value today.
    agent_center_state_backend: str = os.getenv("AGENT_CENTER_STATE_BACKEND", "db")
    enable_internal_demo_fixtures: bool = (
        os.getenv("ENABLE_INTERNAL_DEMO_FIXTURES", "false").lower() == "true"
    )
    enable_internal_production_validation: bool = (
        os.getenv("ENABLE_INTERNAL_PRODUCTION_VALIDATION", "false").lower() == "true"
    )
    # When true, demand-test runs that would normally call Gemini via
    # PIVOTA-Agent fall back to deterministic stub responses. V1 ships with
    # this on by default; flip to "false" once GEMINI_API_KEY is wired in
    # PIVOTA-Agent and prompts have been calibrated.
    pivota_agent_center_mock_gemini: bool = (
        os.getenv("PIVOTA_AGENT_CENTER_MOCK_GEMINI", "true").lower() == "true"
    )
    # PIVOTA-Agent /internal/agent-center/llm-probe endpoint URL +
    # shared-secret. Required in production; in dev with the secret unset
    # the demand-test runner falls back to local mock findings without
    # making any HTTP call.
    pivota_agent_internal_url: str = os.getenv(
        "PIVOTA_AGENT_INTERNAL_URL",
        "https://pivota-agent-production.up.railway.app",
    )
    # Shared secret with PIVOTA-Agent's /internal/agent-center/llm-probe.
    # Three candidate env vars in priority order:
    #
    #   1. PROMOTIONS_ADMIN_KEY — already set on both pivota-backend and
    #      PIVOTA-Agent for admin/ops routes (utils/auth.py +
    #      migrate_promotions_to_backend.js). Reusing it eliminates any
    #      operational burden — the V1 BD-report path just works as
    #      soon as the new code ships.
    #   2. AGENT_API_KEY        — gateway-proxy + agent-commerce convention.
    #   3. PIVOTA_AGENT_INTERNAL_API_KEY — V1 dedicated name; backward compat.
    #
    # Mirrors the priority chain in PIVOTA-Agent's requireInternalKey
    # middleware so a key set on either name on the upstream + the same
    # name on the backend results in matching auth.
    pivota_agent_internal_api_key: Optional[str] = (
        os.getenv("PROMOTIONS_ADMIN_KEY")
        or os.getenv("AGENT_API_KEY")
        or os.getenv("PIVOTA_AGENT_INTERNAL_API_KEY")
    )
    # P5.8.5: Pivota backend's own base URL, used by the
    # pivota_internal_retrieval verifier (P5.3) to hit the
    # GET /products/{sig_id} resolver from within the worker.
    # Unlike pivota_agent_internal_url (which points OUT to the
    # upstream PIVOTA-Agent), this points back into THIS service
    # so the verifier can confirm canonical-sig consistency from
    # the same instance / pod.
    #
    # On Railway use http://${RAILWAY_PRIVATE_DOMAIN}:8000 (private
    # networking; cheaper + lower latency than going through the
    # public domain). Setting to empty string DISABLES the verifier
    # — the run_pivota_internal_retrieval verifier returns
    # blocked:not_configured rather than retry-storming against a
    # bogus URL.
    pivota_backend_internal_url: Optional[str] = (
        os.getenv("PIVOTA_BACKEND_INTERNAL_URL")
    )

    # Grounded buyer-intent probes run 2 sequential searches and can take
    # ~60-110s. 120s gives headroom; async fire-and-poll means this no longer
    # affects a user-facing request. Env-overridable.
    agent_center_llm_probe_timeout_s: float = float(
        os.getenv("AGENT_CENTER_LLM_PROBE_TIMEOUT_S", "120")
    )

    # Deepseek V4 multi-LLM probe (PR-3a). When DEEPSEEK_API_KEY is
    # configured, the backend can probe Deepseek directly (bypassing
    # the upstream PIVOTA-Agent codex stack) for any audit that
    # passes provider="deepseek". Lets us ship multi-LLM coverage
    # in backend Python without waiting for codex round-trip.
    deepseek_api_key: Optional[str] = os.getenv("DEEPSEEK_API_KEY")
    deepseek_api_base_url: str = os.getenv(
        "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com",
    )
    # Default model — override per-environment if Deepseek releases a
    # newer V4 successor. V3.1 is currently the most-capable Deepseek
    # chat model with native function-calling + structured-output
    # support, which the probe relies on for predictable JSON outputs.
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # Strategic brief synthesis (per-SKU Wave 1). Default generator = Gemini
    # Flash (founder call 2026-07-03): far better instruction adherence than
    # DeepSeek on the brief's strict grounding rules (no forbidden words, no
    # competitor-lack claims, only evidence entities), which is what drove the
    # rich↔deterministic-fallback flip. Key-aware: falls back to DeepSeek when
    # the Gemini key is absent (see _resolve_brief_provider).
    strategic_brief_provider: str = os.getenv("STRATEGIC_BRIEF_PROVIDER", "gemini")
    strategic_brief_model: str = os.getenv("STRATEGIC_BRIEF_MODEL", "")

    # Stage-1 LLM prompt generation (extract_winnable_prompts): ON by default —
    # deterministic templates alone can't produce the specific value-prop
    # discovery prompts a long-tail brand can actually win. Kill switch per env.
    # Provider/model are SEPARATE from the strategic brief so prompt-generation
    # models can be A/B compared in prod without touching brief quality
    # (fallback chain: PROMPT_GEN_* -> STRATEGIC_BRIEF_* -> provider default).
    prompt_gen_enabled: bool = (
        os.getenv("AUDIT_LLM_PROMPT_GEN_ENABLED", "true").lower() == "true"
    )
    # Default generator = Gemini Flash (founder call 2026-07-03): stronger
    # Korean + more consistent output language than deepseek on K-beauty PDPs,
    # cheap, and the key is already provisioned (content-brief agent). When the
    # Gemini key is missing, extract_winnable_prompts falls back to the
    # strategic-brief provider chain rather than silently disabling.
    prompt_gen_provider: str = os.getenv("PROMPT_GEN_PROVIDER", "gemini")
    prompt_gen_model: str = os.getenv("PROMPT_GEN_MODEL", "")

    # Gemini for backend-direct (ungrounded) synthesis — shares the key the
    # content-brief executor already uses; model is the low-cost flash tier.
    gemini_api_key: str = (
        os.getenv("GEMINI_API_KEY", "") or os.getenv("PIVOTA_GEMINI_API_KEY", "")
    )
    gemini_synthesis_model: str = os.getenv(
        "GEMINI_SYNTHESIS_MODEL", "gemini-2.5-flash"
    )
    strategic_brief_enabled: bool = (
        os.getenv("STRATEGIC_BRIEF_ENABLED", "false").lower() == "true"
    )
    # Phase 2b — LLM attribute extractor (services/llm_attribute_extractor.py).
    # OFF by default: a staged rollout. When on, it runs ONLY for SKUs the
    # lexicon can't serve (profile.attribute_strategy == llm_extractor, i.e.
    # electronics/generic, or a lexicon miss) — the beauty happy path never calls
    # it. Provider/model fall back to the prompt-gen chain when unset.
    attribute_extractor_enabled: bool = (
        os.getenv("AUDIT_ATTRIBUTE_EXTRACTOR_ENABLED", "false").lower() == "true"
    )
    attribute_extractor_provider: str = os.getenv("ATTRIBUTE_EXTRACTOR_PROVIDER", "")
    attribute_extractor_model: str = os.getenv("ATTRIBUTE_EXTRACTOR_MODEL", "")
    # Raised 900 -> 4000: the extractor emits {"attributes":[{class_name,value,
    # span}...]} where each span is a verbatim page excerpt, so a rich
    # electronics/audio SKU overflows 900 tokens, the JSON truncates, and the
    # parser silently returns [] (the extractor contributes nothing). A real
    # HaptiFit SKU needed ~2000 output tokens; 4000 gives headroom.
    attribute_extractor_max_tokens: int = int(
        os.getenv("ATTRIBUTE_EXTRACTOR_MAX_TOKENS", "4000")
    )
    # Per-merchant scoping for the extractor rollout. Comma-separated merchant_ids.
    # NON-EMPTY -> the extractor runs ONLY for these merchants (even with the flag
    # on) — the safe way to pilot on one merchant (Mojawa) without touching prod
    # electronics/thin traffic. EMPTY -> no merchant restriction (the flag alone
    # governs), preserving Phase-2b behavior.
    attribute_extractor_merchants_raw: str = os.getenv(
        "AUDIT_ATTRIBUTE_EXTRACTOR_MERCHANTS", ""
    )

    @property
    def attribute_extractor_merchants(self) -> set:
        return {
            m.strip()
            for m in (self.attribute_extractor_merchants_raw or "").split(",")
            if m.strip()
        }
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    anthropic_model: str = os.getenv(
        "ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"
    )

    # SerpAPI — deterministic web search for the BD social-intel probes.
    # The 3 social probes (own presence / KOL / competitive) replaced
    # Gemini's discretionary `google_search` grounding (which a 2026-05-14
    # diagnostic proved returns 0 chunks 12/15 calls) with a real search
    # API + Gemini-as-extractor. When SERPAPI_API_KEY is unset the social
    # search client returns no results and the probes degrade to
    # "ungrounded" — they never fabricate. See services/social_search_client.py.
    serpapi_api_key: Optional[str] = os.getenv("SERPAPI_API_KEY")
    serpapi_base_url: str = os.getenv("SERPAPI_BASE_URL", "https://serpapi.com")

    # Platform Orders ACP Integration
    enable_platform_orders_acp: bool = os.getenv("FEATURE_PLATFORM_ORDERS_ACP", "false").lower() == "true"
    platform_orders_acp_url: str = os.getenv(
        "PLATFORM_ORDERS_ACP_URL",
        "https://pivota-acp-production.up.railway.app",
    )
    # Bearer token for service-to-service auth between pivota-backend and the
    # pivota-acp service. Required in production; missing in dev falls back
    # to the placeholder "test" token so local end-to-end flows keep working.
    platform_orders_acp_token: Optional[str] = os.getenv("PLATFORM_ORDERS_ACP_TOKEN")
    # Shared HMAC-SHA256 secret signing inbound ACP order-completed webhooks.
    # Required in production; missing in dev allows the webhook through with
    # a warning so local end-to-end flows still work.
    platform_orders_acp_webhook_secret: Optional[str] = os.getenv(
        "PLATFORM_ORDERS_ACP_WEBHOOK_SECRET"
    )

    # --- P-T2.2: Tier-2 in-chat protocol checkout kill-switch --------------
    # Two fail-closed gates on the real charge path for the agentic-commerce
    # protocol lane (ACP/UCP/AP2). They ONLY affect protocol-tier charges; the
    # live redirect floor + existing REST/hosted flows (protocol_name="rest")
    # are never touched. See services/agent_checkout_kill_switch.py.
    #
    # agent_checkout_strict: the protective guard. ON by default and stays on
    #   unless explicitly disabled — an absent/blank env is treated as ON
    #   (fail-closed). When ON, a protocol-tier charge is allowed only if
    #   submit_payment is also enabled. When explicitly OFF (dev only), the
    #   guard is bypassed.
    # agent_submit_payment_enabled: the ceiling. OFF by default — a protocol
    #   charge cannot execute until this is explicitly turned on (P-T2.3
    #   canary), and only while strict is ON.
    agent_checkout_strict: bool = (
        os.getenv("AGENT_CHECKOUT_STRICT", "true").strip().lower()
        not in {"false", "0", "off", "no"}
    )
    agent_submit_payment_enabled: bool = (
        os.getenv("SUBMIT_PAYMENT", "false").strip().lower()
        in {"true", "1", "on", "yes"}
    )
    # agent_submit_payment_merchants: the canary scope. Comma-separated merchant
    # ids; when NON-EMPTY, a protocol-tier charge is allowed only for a merchant
    # on this list (even with submit_payment ON) — so the first live flip opens
    # exactly one merchant by construction, not everyone. EMPTY (default) = no
    # per-merchant restriction (submit_payment governs globally). Never widens
    # access on its own: it can only further restrict an already-enabled ceiling.
    agent_submit_payment_merchants: frozenset[str] = frozenset(
        m.strip() for m in os.getenv("SUBMIT_PAYMENT_MERCHANTS", "").split(",") if m.strip()
    )
    # P-T2.3.2 test-mode capture canary. When ON, an ACP-tier charge that the
    # kill-switch already permits may run against a TEST-MODE PSP by bypassing the
    # live-readiness gate — but ONLY in this lane, and ONLY up to a hard amount
    # cap. Default OFF: production ACP charges still require a live PSP. This is
    # what lets a test-mode Stripe key transact for the canary without weakening
    # any real flow (the "system blocks test keys" unblock).
    agent_acp_test_capture: bool = (
        os.getenv("AGENT_ACP_TEST_CAPTURE", "false").strip().lower()
        in {"true", "1", "on", "yes"}
    )
    agent_acp_test_max_cents: int = int(
        os.getenv("AGENT_ACP_TEST_MAX_CENTS", "500") or "500"
    )
    # P-T2.3.5 live-capture canary. Graduates the proven test-mode ACP capture to
    # a REAL live-money off-session charge. Deliberately a SEPARATE, stricter gate
    # from the test lane — its own default-off master switch, its own required
    # per-merchant allowlist, and its own (low) amount cap — so turning on the test
    # canary can never imply live money. A live capture engages ONLY when the
    # kill-switch already permits, allow_live is ON, AND the merchant is on
    # AGENT_ACP_LIVE_CAPTURE_MERCHANTS (empty = no merchant can go live; the master
    # switch alone is inert). Requires a real live PSP + a real buyer payment token
    # (the test pm_card_visa default is refused on the live lane).
    agent_acp_allow_live_capture: bool = (
        os.getenv("AGENT_ACP_ALLOW_LIVE_CAPTURE", "false").strip().lower()
        in {"true", "1", "on", "yes"}
    )
    agent_acp_live_capture_merchants: frozenset[str] = frozenset(
        m.strip() for m in os.getenv("AGENT_ACP_LIVE_CAPTURE_MERCHANTS", "").split(",") if m.strip()
    )
    agent_acp_live_max_cents: int = int(
        os.getenv("AGENT_ACP_LIVE_MAX_CENTS", "200") or "200"
    )

    # Readiness audit / thin-slice flags
    feature_readiness_audit: bool = os.getenv("FEATURE_READINESS_AUDIT", "false").lower() == "true"
    feature_readiness_ucp_thin_slice: bool = os.getenv("FEATURE_READINESS_UCP_THIN_SLICE", "false").lower() == "true"
    feature_readiness_real_merchant_alpha: bool = os.getenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "false").lower() == "true"
    feature_readiness_source_of_truth_v1: bool = os.getenv("FEATURE_READINESS_SOURCE_OF_TRUTH_V1", "false").lower() == "true"
    feature_readiness_canonical_checkout_alpha: bool = os.getenv("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA", "false").lower() == "true"
    readiness_alpha_merchant_id: Optional[str] = os.getenv("READINESS_ALPHA_MERCHANT_ID")
    readiness_internal_api_key: Optional[str] = os.getenv("READINESS_INTERNAL_API_KEY")
    readiness_allow_unauthenticated_dev: bool = os.getenv("READINESS_ALLOW_UNAUTHED_DEV", "false").lower() == "true"
    
    # Amazon SP-API Integration
    amazon_sp_api_client_id: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_ID")
    amazon_sp_api_client_secret: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_SECRET")
    amazon_sp_api_region: str = os.getenv("AMAZON_SP_API_REGION", "na")  # na, eu, fe
    enable_amazon_sp_api: bool = os.getenv("FEATURE_AMAZON_SP_API", "false").lower() == "true"
    
    # Email / notifications
    sendgrid_api_key: Optional[str] = os.getenv("SENDGRID_API_KEY")
    smtp2go_api_key: Optional[str] = os.getenv("SMTP2GO_API_KEY")
    smtp2go_email_api_key: Optional[str] = os.getenv("SMTP2GO_EMAIL_API_KEY")
    from_email: str = os.getenv("FROM_EMAIL", "noreply@pivota.ai")
    support_email: str = os.getenv("SUPPORT_EMAIL", "support@pivota.ai")
    # Merchant Portal base URL (used for password reset links)
    merchant_portal_base_url: str = os.getenv("MERCHANT_PORTAL_BASE_URL", "https://merchant.pivota.cc")
    # Merchant signup URL used by partner invite links.
    merchant_signup_base_url: str = os.getenv("MERCHANT_SIGNUP_BASE_URL", "https://merchant.pivota.cc/signup")
    # Employee Portal base URL (used for password reset links for employee accounts)
    employee_portal_base_url: str = os.getenv("EMPLOYEE_PORTAL_BASE_URL", "https://employee.pivota.cc")
    # Agent/Developer Portal base URL (used for password reset links)
    # Note: developer portal is the canonical hostname (e.g., https://developer.pivota.cc)
    agent_portal_base_url: str = os.getenv("AGENT_PORTAL_BASE_URL", "https://developer.pivota.cc")
    # Canonical public backend/API hostname for developer-facing contracts.
    public_api_base_url: str = os.getenv("PUBLIC_API_BASE_URL", "")
    
    # AP2 Protocol (Phase 4++)
    enable_ap2_routes: bool = os.getenv("ENABLE_AP2_ROUTES", "false").lower() == "true"
    enable_admin_auth: bool = os.getenv("ENABLE_ADMIN_AUTH", "false").lower() == "true"
    admin_api_token: Optional[str] = os.getenv("ADMIN_API_TOKEN")  # Admin authentication token
    platform_signing_key: Optional[str] = os.getenv("PLATFORM_SIGNING_KEY")  # For receipt signing
    
    # JWT
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "your-super-secret-key")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60 * 24  # 24 hours
    
    # Supabase
    supabase_url: Optional[str] = os.getenv("SUPABASE_URL")
    supabase_anon_key: Optional[str] = os.getenv("SUPABASE_ANON_KEY")
    supabase_service_role_key: Optional[str] = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    # CORS
    dev_mode: bool = os.getenv("DEV_MODE", "false").lower() == "true"
    # Additional CORS settings
    cors_allow_credentials: bool = os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower() == "true"
    cors_expose_headers: list = ["X-Request-Id", "X-Total-Count"]

    # Agent catalog quality thresholds (for gating/boosting in Agent product APIs)
    cq_min_for_agent: float = float(os.getenv("CQ_MIN_FOR_AGENT", "0"))
    mr_min_for_agent: float = float(os.getenv("MR_MIN_FOR_AGENT", "0"))

    # Agent ranking weights (search / find_products)
    ranking_w_rel: float = float(os.getenv("AGENT_RANK_W_REL", "0.6"))
    ranking_w_quality: float = float(os.getenv("AGENT_RANK_W_QUALITY", "0.2"))
    ranking_w_enrichment: float = float(os.getenv("AGENT_RANK_W_ENRICHMENT", "0.2"))
    ranking_w_business: float = float(os.getenv("AGENT_RANK_W_BUSINESS", "0.0"))

    # Phase C — multi-market audit support (scaffolding).
    # When the flag is OFF (default), audits run single-market against
    # audit_default_market_locale. When ON, audits also run probes
    # for each market in phase_c_enabled_markets and surface per-
    # market scores in merchant_view.receipts.markets + a
    # "Localize for market X" action when gap > threshold.
    #
    # DO NOT enable in production until:
    #   1. Concurrency caps from #384 are confirmed stable under
    #      multi-market load (caps multiply: 3 markets × 5 products
    #      × 3 probes = 45 probes per audit, bounded by per-merchant
    #      semaphore)
    #   2. Staging load test confirms upstream LLM provider doesn't
    #      hit quota (per feedback_llm_call_multipliers.md)
    audit_default_market_locale: str = os.getenv(
        "AUDIT_DEFAULT_MARKET_LOCALE", "en-US",
    )
    phase_c_multi_market_enabled: bool = (
        os.getenv("PHASE_C_MULTI_MARKET_ENABLED", "false").lower() == "true"
    )
    # Comma-separated locale list, e.g. "en-US,en-GB,ja-JP". Default
    # mirrors single-market behavior so flipping the enable flag with
    # an unset list doesn't accidentally fire a 3-locale audit.
    phase_c_enabled_markets_raw: str = os.getenv(
        "PHASE_C_ENABLED_MARKETS", "en-US",
    )

    @property
    def phase_c_enabled_markets(self) -> list:
        return [
            m.strip()
            for m in (self.phase_c_enabled_markets_raw or "").split(",")
            if m.strip()
        ]

    # BD cold-start audit — catalog-intelligence integration.
    # The Pivota-catalog-intelligence service (separate repo, Express +
    # Puppeteer) does the heavy lifting for product discovery against
    # cold-target brand sites. When configured, the cold-start endpoint
    # calls it as the primary discovery path; falls back to in-process
    # brand_product_discovery when unreachable / not configured.
    catalog_intelligence_base_url: str = os.getenv(
        "CATALOG_INTELLIGENCE_BASE_URL", "",
    )
    catalog_intelligence_api_key: str = os.getenv(
        "CATALOG_INTELLIGENCE_API_KEY", "",
    )
    # Puppeteer extraction can take 30-90s for large catalogs; this is
    # a hard ceiling, not a target. Discovery is best-effort — if it
    # exceeds this, we fall back to lightweight regex discovery.
    catalog_intelligence_timeout_s: float = float(
        os.getenv("CATALOG_INTELLIGENCE_TIMEOUT_S", "90"),
    )

    # LLM probe concurrency caps (Phase C prerequisite).
    # Per feedback_llm_call_multipliers.md: PR #278 took backend down
    # when uncapped concurrent probes saturated the upstream LLM
    # provider. Phase C multi-market would multiply this 3-6x; both
    # caps must be in place before that lands.
    #   - global: total in-flight LLM probes across all merchants;
    #     caps the backend's overall LLM-provider load.
    #   - per_merchant: in-flight probes for a single merchant_id;
    #     prevents one merchant's audit from starving others.
    # Defaults sized for current single-market traffic. Phase C's
    # multi-market work should re-tune AFTER staging load test.
    llm_probe_global_max_concurrent: int = int(
        os.getenv("LLM_PROBE_GLOBAL_MAX_CONCURRENT", "30")
    )
    llm_probe_per_merchant_max_concurrent: int = int(
        os.getenv("LLM_PROBE_PER_MERCHANT_MAX_CONCURRENT", "5")
    )

    # Phase D wire-up: GSC OAuth + Indexing API + URL Inspection API.
    # Feature flag: stays OFF until creds are configured + a smoke
    # test confirms the OAuth callback round-trips. Once flipped on,
    # the audit's "Grant GSC access" CTA links into the OAuth flow
    # and the audit pipeline starts consuming gsc_url_submissions
    # state.
    gsc_integration_enabled: bool = (
        os.getenv("GSC_INTEGRATION_ENABLED", "false").lower() == "true"
    )
    google_oauth_client_id: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    google_oauth_client_secret: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    google_oauth_redirect_uri: str = os.getenv(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://web-production-fedb.up.railway.app/api/gsc/oauth/callback",
    )

    # ADR-006: Pivota-owned GSC indexing. Distinct principal from the
    # per-merchant OAuth flow above — a single Pivota-owned service
    # account that is the *verified owner* of the canonical PDP property
    # (agent.pivota.cc), so Pivota can submit its OWN canonical URLs to
    # the Indexing API. Flag is separate from gsc_integration_enabled so
    # the two principals flip independently (ADR-006 open-question #2).
    # Stays OFF until the Phase-1 validation spike proves Google honors
    # product-URL submissions under the Pivota credential.
    gsc_pivota_submit_enabled: bool = (
        os.getenv("GSC_PIVOTA_SUBMIT_ENABLED", "false").lower() == "true"
    )
    # Raw service-account credentials JSON (the file Google hands you for
    # a service account), passed as an env string. Empty = not configured.
    gsc_pivota_service_account_json: str = os.getenv(
        "GSC_PIVOTA_SERVICE_ACCOUNT_JSON", ""
    )
    # The verified Search Console property for the canonical PDPs, used as
    # `siteUrl` for URL Inspection read-back (e.g. "https://agent.pivota.cc/").
    gsc_pivota_property_url: str = os.getenv("GSC_PIVOTA_PROPERTY_URL", "")

    # Master kill-switch for the audit executor-agent dispatch layer (content
    # brief, canonical-PDP enrichment, competitor insights, GSC submission,
    # sitemap freshness). Default ON = current behavior. Set to false to
    # globally disable all post-audit agent side-effects (Gemini spend, Google
    # API calls, merchant_tasks creation) in one place, e.g. during an incident
    # or before a merchant-consent model ships. Per-agent flags (GSC, Gemini
    # key) still apply on top of this.
    audit_executor_dispatch_enabled: bool = (
        os.getenv("AUDIT_EXECUTOR_DISPATCH_ENABLED", "true").lower() == "true"
    )

    # IndexNow: notify Bing (which powers ChatGPT search), Yandex, and other
    # participating engines that a canonical PDP became newly citable so it gets
    # crawled. Unlike the GSC Indexing API above (gated off pending proof Google
    # honors product URLs), IndexNow IS honored for ordinary content pages, so
    # this is our live discovery trigger. Best-effort. OFF by default (mirrors the
    # GSC flags — an external call shouldn't fire from tests/local); set
    # INDEXNOW_ENABLED=true in prod to activate ongoing auto-submit. The key
    # default matches the public key file hosted at https://{host}/{key}.txt.
    indexnow_enabled: bool = (
        os.getenv("INDEXNOW_ENABLED", "false").lower() == "true"
    )
    indexnow_host: str = os.getenv("INDEXNOW_HOST", "agent.pivota.cc")
    indexnow_key: str = os.getenv(
        "INDEXNOW_KEY", "a4951ce31e0fa749463bc9d0cfe0f352"
    )
    indexnow_endpoint: str = os.getenv(
        "INDEXNOW_ENDPOINT", "https://api.indexnow.org/indexnow"
    )

    @property
    def cors_origins(self) -> list:
        """Parse comma-separated origins from environment variable"""
        origins_str = os.getenv("ALLOWED_ORIGINS", "")
        if origins_str:
            return [origin.strip() for origin in origins_str.split(",") if origin.strip()]
        return []
    
    class Config:
        env_file = ".env"

# Global settings instance
settings = Settings()
