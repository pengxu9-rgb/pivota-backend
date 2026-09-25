"""`scripts/ops/reap_spec_diff.py`: the differ, on small in-memory specs.

NO NETWORK, NO FIXTURE. The differ's job is to notice a partner moving a contract without moving
a version; these tests hand it two documents that differ in one known way and assert on the
lines it prints. The three cases were each a real miss on 2026-09-25:

  * a change hidden behind a `$ref` (the enrollment owner's `email` became required inside
    `components.schemas.ClientReferenceOwner`; the old script compared the reference STRING);
  * a wording-only edit (BinSponsor/ReapCard descriptions now start "Coming soon."; must NOT
    be reported, or the diff cries wolf and gets muted);
  * a response code appearing (409s on three operations; the old script compared the 200 only).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os

import pytest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "ops", "reap_spec_diff.py")


@pytest.fixture(scope="module")
def differ():
    spec = importlib.util.spec_from_file_location("reap_spec_diff_unit", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec() -> dict:
    """A miniature of the real document: one `oneOf` request whose EXTERNAL branch reaches the
    owner through a `$ref`, one 200 that reaches it through a second `$ref`, one 400."""
    return {
        "info": {"title": "Reap API", "version": "1.0.0"},
        "servers": [{"url": "https://api.reap.global"}],
        "paths": {
            "/agentic/enrollments": {
                "post": {
                    "operationId": "createEnrollment_agentic",
                    "summary": "Create an enrollment",
                    "parameters": [
                        {"name": "Idempotency-Key", "in": "header", "required": True,
                         "schema": {"type": "string"}, "description": "unique per attempt"},
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"oneOf": [
                            {"type": "object", "title": "Reap card",
                             "description": "Enroll an existing card",
                             "properties": {"source": {"type": "string", "const": "REAP_CARD"},
                                            "cardId": {"type": "string", "format": "uuid"}},
                             "required": ["source", "cardId"]},
                            {"type": "object", "title": "External",
                             "description": "Capture the card on a hosted page",
                             "properties": {
                                 "source": {"type": "string", "const": "EXTERNAL"},
                                 "owner": {"$ref": "#/components/schemas/ClientReferenceOwner"},
                                 "presentation": {
                                     "type": "object",
                                     "properties": {"type": {"type": "string", "const": "REDIRECT"},
                                                    "returnUrl": {"type": "string", "format": "uri"}},
                                     "required": ["type", "returnUrl"]},
                             },
                             "required": ["source", "owner", "presentation"]},
                        ]}}},
                    },
                    "responses": {
                        "200": {"description": "Created", "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/EnrollmentCreateResponse"}}}},
                        "400": {"description": "Rejected", "content": {"application/json": {
                            "schema": {"type": "object", "properties": {"error": {
                                "type": "object",
                                "properties": {"code": {"type": "string",
                                                        "const": "AGENTIC_REQUEST_REJECTED"}},
                                "required": ["code"]}}, "required": ["error"]}}}},
                    },
                },
            },
            "/not-agentic": {"get": {"responses": {"200": {"description": "ignored"}}}},
        },
        "components": {"schemas": {
            "ClientReferenceOwner": {
                "type": "object", "title": "Client reference",
                "description": "The client customer that owns this enrollment",
                "properties": {"type": {"type": "string", "const": "CLIENT_REFERENCE"},
                               "id": {"type": "string", "minLength": 1},
                               "email": {"type": "string", "format": "email",
                                         "description": "prefilled on the hosted page"}},
                "required": ["type", "id"],
            },
            "EnrollmentCreateResponse": {
                "type": "object",
                "properties": {"id": {"type": "string", "format": "uuid"},
                               "owner": {"$ref": "#/components/schemas/ClientReferenceOwner"}},
                "required": ["id", "owner"],
            },
        }},
    }


def _subset(differ, spec):
    return differ.extract_agentic(spec)


# --- the three misses -----------------------------------------------------------------------------


def test_a_required_field_added_inside_a_referenced_schema_is_reported_with_its_path(differ):
    """THE 2026-09-25 MISS. `email` became required inside `ClientReferenceOwner`; the request
    body reaches it through `$ref`, and the reference string did not change. The old differ
    compared the string."""
    pinned = _spec()
    live = _spec()
    live["components"]["schemas"]["ClientReferenceOwner"]["required"].append("email")

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert ("POST /agentic/enrollments: request.oneOf[1].properties.owner.required + email"
            in problems), problems
    # The 200 reaches the same schema through a SECOND ref (EnrollmentCreateResponse.owner), and
    # it is reported there too: every path that dereferences to the change names the change.
    assert ("POST /agentic/enrollments: responses.200.content.application/json"
            ".properties.owner.required + email" in problems), problems
    # Nothing else: the two lines above are the whole diff.
    assert len(problems) == 2, problems


def test_a_wording_only_change_is_not_a_difference(differ):
    """BinSponsor/ReapCard descriptions now start "Coming soon."; a title and a summary moved
    too. None of it is a thing a client can get wrong, and a diff that fires on prose is a diff
    that gets muted -- which is how a real change goes unread."""
    pinned = _spec()
    live = _spec()
    op = live["paths"]["/agentic/enrollments"]["post"]
    op["summary"] = "Coming soon. Create an enrollment"
    op["parameters"][0]["description"] = "reworded"
    branch = op["requestBody"]["content"]["application/json"]["schema"]["oneOf"][0]
    branch["description"] = "Coming soon. Enroll an existing card"
    branch["title"] = "Reap card (renamed)"
    branch["properties"]["cardId"]["example"] = "0f0f0f0f-0000-0000-0000-000000000000"
    live["components"]["schemas"]["ClientReferenceOwner"]["description"] = "reworded owner"
    live["components"]["schemas"]["ClientReferenceOwner"]["properties"]["email"]["description"] = "x"
    op["responses"]["400"]["description"] = "Response for status 400"

    assert differ.diff(_subset(differ, pinned), _subset(differ, live)) == []


def test_a_new_response_code_is_reported(differ):
    """The old differ compared the 200 and nothing else, so three new 409s were invisible. A
    response code that appears is a response the client has never classified."""
    pinned = _spec()
    live = _spec()
    live["paths"]["/agentic/enrollments"]["post"]["responses"]["409"] = {
        "description": "A request with this idempotency key is currently in progress",
        "content": {"application/json": {"schema": {
            "type": "object", "title": "IdempotencyRequestInProgressError",
            "properties": {"error": {"type": "object", "properties": {
                "code": {"type": "string", "const": "IDEMPOTENCY_REQUEST_IN_PROGRESS"}},
                "required": ["code"]}},
            "required": ["error"]}}},
    }

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert len(problems) == 1, problems
    assert problems[0].startswith("POST /agentic/enrollments: responses + 409"), problems
    # The line names what is INSIDE the new response, so the operator does not have to open it.
    assert "IDEMPOTENCY_REQUEST_IN_PROGRESS" in problems[0], problems


# --- the shape of the comparison -------------------------------------------------------------------


def test_an_error_object_becoming_an_anyOf_is_one_line_naming_both_sides(differ):
    """Twelve operations did this on 2026-09-25. Key by key it is four lines each, none of which
    says what the alternatives are."""
    pinned = _spec()
    live = _spec()
    old_400 = live["paths"]["/agentic/enrollments"]["post"]["responses"]["400"]
    single = old_400["content"]["application/json"]["schema"]
    other = copy.deepcopy(single)
    other["properties"]["error"]["properties"]["code"]["const"] = "IDEMPOTENT_PARAMETER_MISMATCH"
    old_400["content"]["application/json"]["schema"] = {"anyOf": [single, other]}

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert len(problems) == 1, problems
    line = problems[0]
    assert line.startswith("POST /agentic/enrollments: responses.400.content.application/json: ")
    assert "AGENTIC_REQUEST_REJECTED" in line and "IDEMPOTENT_PARAMETER_MISMATCH" in line
    assert "anyOf[" in line


def test_an_inserted_branch_does_not_misalign_the_branches_after_it(differ):
    """Branches are paired by what they ARE (property names and consts), not by position: a
    partner inserting an enrollment source at the front must not read as a change to every
    branch behind it."""
    pinned = _spec()
    live = _spec()
    schema = live["paths"]["/agentic/enrollments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["oneOf"].insert(0, {"type": "object",
                               "properties": {"source": {"type": "string", "const": "BIN_SPONSOR"},
                                              "cardId": {"type": "string"}},
                               "required": ["source", "cardId"]})

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert problems == ['POST /agentic/enrollments: request.oneOf[0] ADDED: {cardId,source}="BIN_SPONSOR"'], problems


def test_a_new_parameter_is_reported_by_name_and_a_required_flip_by_path(differ):
    """`X-Simulate-Checkout` on 2026-09-25 (sandbox-only header). Named, not `parameters[2]`."""
    pinned = _spec()
    live = _spec()
    params = live["paths"]["/agentic/enrollments"]["post"]["parameters"]
    params.insert(0, {"name": "X-Simulate-Checkout", "in": "header", "required": False,
                      "schema": {"type": "string", "enum": ["COMPLETED"]}})
    params[1]["required"] = False

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert "POST /agentic/enrollments: parameters[1] ADDED: header:X-Simulate-Checkout" in problems, problems
    assert "POST /agentic/enrollments: parameters[0].required: true -> false" in problems, problems
    assert len(problems) == 2, problems


def test_a_removed_required_field_and_an_enum_change_are_reported_as_set_deltas(differ):
    """`required` and `enum` are sets, not sequences: a reorder is nothing, a member is a line."""
    pinned = _spec()
    live = _spec()
    schema = live["paths"]["/agentic/enrollments"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    external = schema["oneOf"][1]
    external["required"] = ["presentation", "source"]           # `owner` dropped, order changed
    external["properties"]["presentation"]["properties"]["type"] = {
        "type": "string", "enum": ["REDIRECT", "EMBED"]}      # was a const

    problems = differ.diff(_subset(differ, pinned), _subset(differ, live))

    assert "POST /agentic/enrollments: request.oneOf[1].required - owner" in problems, problems
    assert any(p.startswith("POST /agentic/enrollments: request.oneOf[1].properties.presentation"
                            ".properties.type") for p in problems), problems
    assert not any("presentation.required" in p for p in problems), "a reorder is not a change"


def test_the_control_an_unchanged_spec_diffs_clean(differ):
    """Without this, "it reported a problem" means nothing: a differ that reported everything
    would pass every positive test above."""
    spec = _spec()
    assert differ.diff(_subset(differ, spec), _subset(differ, json.loads(json.dumps(spec)))) == []


# --- dereferencing --------------------------------------------------------------------------------


def test_dereference_guards_a_cycle_and_still_expands_siblings(differ):
    schemas = {
        "Node": {"type": "object",
                 "properties": {"next": {"$ref": "#/components/schemas/Node"},
                                "owner": {"$ref": "#/components/schemas/Owner"},
                                "other": {"$ref": "#/components/schemas/Owner"}}},
        "Owner": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    out = differ.dereference({"$ref": "#/components/schemas/Node"}, schemas)
    assert out["properties"]["next"] == {"$cycle": "Node"}
    # Both sibling refs expand; a cycle guard keyed on "seen anywhere" would have collapsed the
    # second one and made the two positions incomparable.
    assert out["properties"]["owner"] == schemas["Owner"]
    assert out["properties"]["other"] == schemas["Owner"]


def test_dereference_keeps_a_missing_target_comparable_rather_than_crashing(differ):
    out = differ.dereference({"$ref": "#/components/schemas/Gone"}, {})
    assert out == {"$missing": "Gone"}


def test_a_change_to_the_referenced_schema_is_not_hidden_by_the_pin_keeping_refs(differ):
    """The FIXTURE keeps `$ref`s as written (so a human can read it); the comparison must not.
    A pinned document whose component changed while its paths did not is exactly the 09-25
    fixture, and it must still diff."""
    pinned = _subset(differ, _spec())
    live = json.loads(json.dumps(pinned))
    live["components"]["schemas"]["ClientReferenceOwner"]["properties"]["email"]["pattern"] = "^.+@.+$"
    assert json.dumps(pinned["paths"]) == json.dumps(live["paths"]), "control: paths are byte-equal"
    problems = differ.diff(pinned, live)
    assert ("POST /agentic/enrollments: request.oneOf[1].properties.owner.properties.email"
            " + pattern" in problems), problems
