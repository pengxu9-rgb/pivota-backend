from __future__ import annotations

from sqlalchemy import (
    Table,
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    JSON,
    DateTime,
    Boolean,
    Float,
    SmallInteger,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from db.database import metadata


_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

review_group = Table(
    "review_group",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("group_type", String(32), nullable=False),
    Column("group_key", Text, nullable=False, unique=True),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_by", String(16), nullable=False, server_default="system"),
    Column("created_by_employee_id", String(64), nullable=True),
    Column("featured_frozen", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


review_group_membership = Table(
    "review_group_membership",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("group_id", _ID_TYPE, ForeignKey("review_group.id", ondelete="CASCADE"), nullable=False),
    Column("product_key", Text, nullable=False),
    Column("sku_key", Text, nullable=False),
    Column("merchant_id", String(64), nullable=False),
    Column("platform", String(64), nullable=False),
    Column("platform_product_id", String(256), nullable=False),
    Column("variant_id", String(256), nullable=True),
    Column("match_type", String(32), nullable=False),
    Column("evidence", _JSON_TYPE, nullable=True),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_by", String(16), nullable=False, server_default="system"),
    Column("created_by_employee_id", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_review_group_membership_group_id", review_group_membership.c.group_id)
Index("idx_review_group_membership_product_key", review_group_membership.c.product_key)
Index(
    "ux_review_group_membership_active_sku",
    review_group_membership.c.sku_key,
    unique=True,
    postgresql_where=review_group_membership.c.status == "active",
)


external_identities = Table(
    "external_identities",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("merchant_id", String(64), nullable=False),
    Column("source_system", String(64), nullable=False),
    Column("external_user_id", Text, nullable=True),
    Column("author_fingerprint", Text, nullable=True),
    Column("display_name", Text, nullable=True),
    Column("status", String(16), nullable=False, server_default="unclaimed"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "ux_external_identities_external_user",
    external_identities.c.merchant_id,
    external_identities.c.source_system,
    external_identities.c.external_user_id,
    unique=True,
    postgresql_where=(external_identities.c.external_user_id.isnot(None) & (external_identities.c.external_user_id != "")),
)
Index("idx_external_identities_merchant_source", external_identities.c.merchant_id, external_identities.c.source_system)


product_reviews = Table(
    "product_reviews",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("product_key", Text, nullable=False),
    Column("sku_key", Text, nullable=False),
    Column("merchant_id", String(64), nullable=False),
    Column("platform", String(64), nullable=False),
    Column("platform_product_id", String(256), nullable=False),
    Column("variant_id", String(256), nullable=True),
    Column("group_id", _ID_TYPE, ForeignKey("review_group.id", ondelete="SET NULL"), nullable=True),
    Column("author_user_id", _ID_TYPE, ForeignKey("external_identities.id", ondelete="SET NULL"), nullable=True),
    Column("source_type", String(16), nullable=False, server_default="imported"),
    Column("source_system", String(64), nullable=True),
    Column("external_review_id", Text, nullable=True),
    Column("dedupe_key", Text, nullable=True),
    Column("verification", String(32), nullable=False, server_default="unverified"),
    Column("rating", SmallInteger, nullable=True),
    Column("title", Text, nullable=True),
    Column("body", Text, nullable=True),
    Column("body_redacted", Text, nullable=True),
    Column("redaction", _JSON_TYPE, nullable=True),
    Column("editor_note", Text, nullable=True),
    Column("media_count", Integer, nullable=False, server_default="0"),
    Column("risk_flags", _JSON_TYPE, nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_product_reviews_sku_created", product_reviews.c.sku_key, product_reviews.c.created_at.desc())
Index("idx_product_reviews_group_created", product_reviews.c.group_id, product_reviews.c.created_at.desc())
Index("idx_product_reviews_merchant_created", product_reviews.c.merchant_id, product_reviews.c.created_at.desc())
Index("idx_product_reviews_status", product_reviews.c.status)
Index(
    "ux_product_reviews_merchant_source_external",
    product_reviews.c.merchant_id,
    product_reviews.c.source_system,
    product_reviews.c.external_review_id,
    unique=True,
    postgresql_where=(
        product_reviews.c.external_review_id.isnot(None)
        & (product_reviews.c.external_review_id != "")
        & product_reviews.c.source_system.isnot(None)
        & (product_reviews.c.source_system != "")
    ),
)


media_assets = Table(
    "media_assets",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("public_id", Text, nullable=True),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False),
    Column("type", String(16), nullable=False),
    Column("url", Text, nullable=False),
    Column("file_path", Text, nullable=True),
    Column("width", Integer, nullable=True),
    Column("height", Integer, nullable=True),
    Column("file_hash", Text, nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_media_assets_review_id", media_assets.c.review_id)
Index(
    "ux_media_assets_public_id",
    media_assets.c.public_id,
    unique=True,
    postgresql_where=(media_assets.c.public_id.isnot(None) & (media_assets.c.public_id != "")),
)


review_replies = Table(
    "review_replies",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False),
    Column("replier_type", String(16), nullable=False),
    Column("replier_id", String(128), nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_review_replies_review_id_created", review_replies.c.review_id, review_replies.c.created_at)


review_interactions = Table(
    "review_interactions",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("type", String(16), nullable=False),
    Column("value", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("review_id", "user_id", "type", name="ux_review_interactions_user"),
)


seller_feedback = Table(
    "seller_feedback",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("merchant_id", String(64), nullable=False),
    Column("order_ref", _JSON_TYPE, nullable=True),
    Column("rating_overall", SmallInteger, nullable=True),
    Column("dims_json", _JSON_TYPE, nullable=True),
    Column("body", Text, nullable=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_seller_feedback_merchant_created", seller_feedback.c.merchant_id, seller_feedback.c.created_at.desc())


review_featured = Table(
    "review_featured",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("group_id", _ID_TYPE, ForeignKey("review_group.id", ondelete="CASCADE"), nullable=False),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False),
    Column("rank", Integer, nullable=False, server_default="0"),
    Column("score", Float, nullable=False, server_default="0"),
    Column("reason_tags", _JSON_TYPE, nullable=True),
    Column("generated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("is_pinned", Boolean, nullable=False, server_default="false"),
    UniqueConstraint("group_id", "review_id", name="ux_review_featured_group_review"),
)

Index("idx_review_featured_group_rank", review_featured.c.group_id, review_featured.c.rank)


import_batches = Table(
    "import_batches",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("merchant_id", String(64), nullable=False),
    Column("source_system", String(64), nullable=False),
    Column("status", String(16), nullable=False, server_default="created"),
    Column("created_by_employee_id", String(64), nullable=True),
    Column("reviews_file_path", Text, nullable=True),
    Column("media_zip_path", Text, nullable=True),
    Column("report_json", _JSON_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_import_batches_merchant_created", import_batches.c.merchant_id, import_batches.c.created_at.desc())


import_items = Table(
    "import_items",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("batch_id", _ID_TYPE, ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False),
    Column("merchant_id", String(64), nullable=False),
    Column("source_system", String(64), nullable=False),
    Column("external_review_id", Text, nullable=True),
    Column("external_user_id", Text, nullable=True),
    Column("payload_json", _JSON_TYPE, nullable=False),
    Column("match_product_key", Text, nullable=True),
    Column("match_sku_key", Text, nullable=True),
    Column("match_confidence", Float, nullable=False, server_default="0"),
    Column("group_id", _ID_TYPE, ForeignKey("review_group.id", ondelete="SET NULL"), nullable=True),
    Column("group_confidence", Float, nullable=False, server_default="0"),
    Column("dedupe_key", Text, nullable=True),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("error_reason", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


# ---------------------------------------------------------------------------
# Buyer review submission (idempotency + replay prevention)
# ---------------------------------------------------------------------------

buyer_review_submission_jtis = Table(
    "buyer_review_submission_jtis",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("merchant_id", String(64), nullable=False),
    Column("jti_hash", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("ux_buyer_review_submission_jtis_jti_hash", buyer_review_submission_jtis.c.jti_hash, unique=True)
Index("idx_buyer_review_submission_jtis_expires", buyer_review_submission_jtis.c.expires_at)


buyer_review_idempotency_keys = Table(
    "buyer_review_idempotency_keys",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("merchant_id", String(64), nullable=False),
    Column("idempotency_key_hash", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="SET NULL"), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "ux_buyer_review_idempotency_keys_merchant_key",
    buyer_review_idempotency_keys.c.merchant_id,
    buyer_review_idempotency_keys.c.idempotency_key_hash,
    unique=True,
)


buyer_review_ownership = Table(
    "buyer_review_ownership",
    metadata,
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), primary_key=True),
    Column("token_jti_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_buyer_review_ownership_token", buyer_review_ownership.c.token_jti_hash)

buyer_review_user_subject = Table(
    "buyer_review_user_subject",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("user_id", String(128), nullable=False),
    Column("subject_type", String(32), nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("order_id", String(128), nullable=True),
    Column("review_id", _ID_TYPE, ForeignKey("product_reviews.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("user_id", "subject_type", "subject_id", "order_id", name="ux_buyer_review_user_subject_order"),
)

Index("idx_buyer_review_user_subject_user", buyer_review_user_subject.c.user_id)
Index("idx_buyer_review_user_subject_subject", buyer_review_user_subject.c.subject_type, buyer_review_user_subject.c.subject_id)
Index("idx_buyer_review_user_subject_order_id", buyer_review_user_subject.c.order_id)
Index(
    "ux_buyer_review_user_subject_legacy_null_order",
    buyer_review_user_subject.c.user_id,
    buyer_review_user_subject.c.subject_type,
    buyer_review_user_subject.c.subject_id,
    unique=True,
    postgresql_where=buyer_review_user_subject.c.order_id.is_(None),
)

ugc_questions = Table(
    "ugc_questions",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("user_id", String(128), nullable=False),
    Column("subject_type", String(32), nullable=False),
    Column("subject_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_ugc_questions_user_created", ugc_questions.c.user_id, ugc_questions.c.created_at.desc())
Index("idx_ugc_questions_subject_created", ugc_questions.c.subject_type, ugc_questions.c.subject_id, ugc_questions.c.created_at.desc())

ugc_question_replies = Table(
    "ugc_question_replies",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("question_id", _ID_TYPE, ForeignKey("ugc_questions.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index(
    "idx_ugc_question_replies_question_created",
    ugc_question_replies.c.question_id,
    ugc_question_replies.c.created_at.desc(),
)
Index(
    "idx_ugc_question_replies_user_created",
    ugc_question_replies.c.user_id,
    ugc_question_replies.c.created_at.desc(),
)

Index("idx_import_items_batch_status", import_items.c.batch_id, import_items.c.status)
Index(
    "ux_import_items_merchant_source_external_review",
    import_items.c.merchant_id,
    import_items.c.source_system,
    import_items.c.external_review_id,
    unique=True,
    postgresql_where=(
        import_items.c.external_review_id.isnot(None)
        & (import_items.c.external_review_id != "")
        & (import_items.c.source_system != "")
    ),
)


employee_audit_logs = Table(
    "employee_audit_logs",
    metadata,
    Column("id", _ID_TYPE, primary_key=True, autoincrement=True),
    Column("actor_employee_id", String(64), nullable=True),
    Column("actor_email", String(255), nullable=True),
    Column("action", String(128), nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(256), nullable=False),
    Column("reason", Text, nullable=True),
    Column("before_json", _JSON_TYPE, nullable=True),
    Column("after_json", _JSON_TYPE, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

Index("idx_employee_audit_logs_target", employee_audit_logs.c.target_type, employee_audit_logs.c.target_id)
Index("idx_employee_audit_logs_created", employee_audit_logs.c.created_at.desc())
