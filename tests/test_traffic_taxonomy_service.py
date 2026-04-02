from __future__ import annotations


def test_build_traffic_taxonomy_prefers_explicit_traffic_fields() -> None:
    from services.traffic_taxonomy_service import build_traffic_taxonomy

    taxonomy = build_traffic_taxonomy(
        {
            "source": "legacy-source",
            "traffic": {
                "source_channel": "shopping-agent-ui",
                "query_source": "cache_multi_intent",
                "protocol_name": "mcp",
                "llm_provider": "openai",
                "llm_model": "gpt-5.4",
            },
        },
        authenticated_agent_id="agent_123",
        caller_id="agent_123",
        default_protocol_name="rest",
        default_commerce_surface="agent_api",
    )

    assert taxonomy["source_channel"] == "shopping-agent-ui"
    assert taxonomy["query_source"] == "cache_multi_intent"
    assert taxonomy["protocol_name"] == "mcp"
    assert taxonomy["agent_id"] == "agent_123"
    assert taxonomy["source_family"] == "internal"
    assert taxonomy["llm_provider"] == "openai"
    assert taxonomy["llm_model"] == "gpt-5.4"


def test_build_traffic_taxonomy_normalizes_protocol_aliases_and_unknowns() -> None:
    from services.traffic_taxonomy_service import build_traffic_taxonomy

    taxonomy = build_traffic_taxonomy(
        {"protocol": "X402"},
        default_source_channel="partner-foo",
        default_commerce_surface="agent_api",
    )

    assert taxonomy["protocol_name"] == "x-402"
    assert taxonomy["source_channel"] == "partner-foo"
    assert taxonomy["source_family"] == "partner"
    assert taxonomy["agent_id"] == "unknown"


def test_attach_traffic_taxonomy_writes_nested_and_top_level_fields() -> None:
    from services.traffic_taxonomy_service import attach_traffic_taxonomy

    metadata = attach_traffic_taxonomy(
        {"source": "legacy"},
        {
            "source_channel": "shopping-agent-ui",
            "source_family": "internal",
            "query_source": "pivot_semantic_core_multi",
            "agent_id": "agent_1",
            "protocol_name": "rest",
            "commerce_surface": "agent_api",
            "llm_provider": "openai",
            "llm_model": "gpt-5.4",
            "caller_id": "agent_1",
        },
    )

    assert metadata["traffic"]["source_channel"] == "shopping-agent-ui"
    assert metadata["source_channel"] == "shopping-agent-ui"
    assert metadata["protocol_name"] == "rest"
    assert metadata["commerce_surface"] == "agent_api"
    assert metadata["source"] == "legacy"


def test_build_traffic_taxonomy_normalizes_creator_source_aliases() -> None:
    from services.traffic_taxonomy_service import build_traffic_taxonomy

    taxonomy = build_traffic_taxonomy(
        {"source": "creator-agent-ui"},
        default_query_source="catalog_search",
        default_protocol_name="rest",
        default_commerce_surface="agent_api",
    )

    assert taxonomy["source_channel"] == "creator-agent"
    assert taxonomy["source_family"] == "internal"
