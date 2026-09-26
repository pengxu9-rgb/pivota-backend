"""The employee portal's agent buttons must land on a mounted route.

pivota-employee-portal lib/api-client.ts reactivateAgent() posts to
/employee/agents/{id}/reactivate, while the backend handler was only ever mounted at
/activate, so the portal's Reactivate button 404'd.
"""

from fastapi import FastAPI

from routes import employee_agent_mgmt


def _post_endpoint(path):
    app = FastAPI()
    app.include_router(employee_agent_mgmt.router)
    matches = [
        route
        for route in app.routes
        if getattr(route, "path", "") == path and "POST" in getattr(route, "methods", set())
    ]
    assert len(matches) == 1, f"{path}: {len(matches)} POST routes"
    return matches[0].endpoint


def test_portal_reactivate_path_is_served_by_the_activate_handler():
    assert _post_endpoint("/employee/agents/{agent_id}/reactivate") is employee_agent_mgmt.activate_agent
    assert _post_endpoint("/employee/agents/{agent_id}/activate") is employee_agent_mgmt.activate_agent


def test_portal_deactivate_path_is_served():
    assert _post_endpoint("/employee/agents/{agent_id}/deactivate") is employee_agent_mgmt.deactivate_agent
