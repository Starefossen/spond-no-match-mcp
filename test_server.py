"""Unit tests for Spond MCP server."""

import time

import pytest

from conftest import (
    MOCK_EVENT_CANCELLED,
    MOCK_EVENT_DETAIL,
    MOCK_EVENTS_FJORDVIK,
    MOCK_GROUPS,
)
from server import (
    TOOLS,
    format_event_detail,
    format_event_summary,
    format_group_summary,
    fuzzy_match_group,
    handle_tool_call,
    norwegian_weekday,
    parse_timestamp,
)

# --- Utility functions ---


class TestParseTimestamp:
    def test_iso_format(self):
        dt = parse_timestamp("2026-02-23T17:00:00.000+00:00")
        assert dt is not None
        assert dt.hour == 17
        assert dt.day == 23

    def test_z_suffix(self):
        dt = parse_timestamp("2026-02-23T17:00:00Z")
        assert dt is not None
        assert dt.hour == 17

    def test_empty(self):
        assert parse_timestamp("") is None

    def test_invalid(self):
        assert parse_timestamp("not-a-date") is None

    def test_none(self):
        assert parse_timestamp(None) is None


class TestNorwegianWeekday:
    def test_monday(self):
        assert norwegian_weekday(0) == "mandag"

    def test_friday(self):
        assert norwegian_weekday(4) == "fredag"

    def test_sunday(self):
        assert norwegian_weekday(6) == "søndag"


class TestFuzzyMatchGroup:
    def test_exact_match(self):
        assert fuzzy_match_group("Fjordvik FK G2013", ["Fjordvik FK G2013"])

    def test_partial_match(self):
        assert fuzzy_match_group("Fjordvik FK G2013", ["Fjordvik"])

    def test_case_insensitive(self):
        assert fuzzy_match_group("fjordvik fk g2013", ["Fjordvik FK G2013"])

    def test_reverse_partial(self):
        assert fuzzy_match_group("Solvik", ["Solvik IL 2017"])

    def test_no_match(self):
        assert not fuzzy_match_group("Fjordvik FK G2013", ["Solvik IL 2017"])

    def test_empty_list(self):
        assert not fuzzy_match_group("Fjordvik", [])

    def test_norwegian_chars(self):
        assert fuzzy_match_group("Nordvik skole kull 2014", ["Nordvik skole"])


# --- Formatting ---


class TestFormatEventSummary:
    def test_basic_event(self):
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[0])
        assert "Trening" in result
        assert "23.02" in result
        assert "17:00-18:30" in result
        assert "Fjordvik kunstgress" in result

    def test_with_family_rsvp(self):
        family = {"MEMBER_OLIVER": "Oliver"}
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[0], family)
        assert "Oliver: bekreftet" in result

    def test_unanswered_rsvp(self):
        family = {"MEMBER_OLIVER": "Oliver"}
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[1], family)
        assert "Oliver: ikke svart" in result

    def test_cancelled_event(self):
        result = format_event_summary(MOCK_EVENT_CANCELLED)
        assert "AVLYST" in result

    def test_no_location(self):
        event = {**MOCK_EVENTS_FJORDVIK[0], "location": {}}
        result = format_event_summary(event)
        assert "Sted:" not in result

    def test_no_timestamp(self):
        event = {**MOCK_EVENTS_FJORDVIK[0], "startTimestamp": "", "endTimestamp": ""}
        result = format_event_summary(event)
        assert result.startswith("Trening")

    def test_description_included(self):
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[0])
        assert "Vanlig trening" in result

    def test_long_description_truncated(self):
        event = {**MOCK_EVENTS_FJORDVIK[0], "description": "x" * 300}
        result = format_event_summary(event)
        assert "..." in result
        assert len([line for line in result.split("\n") if "x" in line][0]) < 210


class TestFormatEventDetail:
    def test_includes_address(self):
        result = format_event_detail(MOCK_EVENT_DETAIL)
        assert "Havnes" in result

    def test_includes_coordinates(self):
        result = format_event_detail(MOCK_EVENT_DETAIL)
        assert "59.8765" in result

    def test_includes_attendance_count(self):
        result = format_event_detail(MOCK_EVENT_DETAIL)
        assert "Påmelding:" in result
        assert "1 ikke svart" in result


class TestFormatGroupSummary:
    def test_group_with_members(self):
        result = format_group_summary(MOCK_GROUPS[0])
        assert "Fjordvik FK G2013" in result
        assert "3 medlemmer" in result

    def test_empty_group(self):
        result = format_group_summary({"name": "Tomgruppe", "members": []})
        assert "0 medlemmer" in result


# --- SpondService caching ---


class TestSpondServiceCache:
    @pytest.mark.asyncio
    async def test_groups_cached(self, service):
        await service.get_groups()
        await service.get_groups()
        # Should only call the API once
        service._client.get_groups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_groups_cache_expires(self, service):
        await service.get_groups()
        # Expire the cache
        service._cache["groups"] = (MOCK_GROUPS, time.monotonic() - 7200)
        await service.get_groups()
        assert service._client.get_groups.await_count == 2

    @pytest.mark.asyncio
    async def test_events_cached(self, service):
        await service.get_events(days=7)
        await service.get_events(days=7)
        service._client.get_events.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_events_different_params_not_cached(self, service):
        await service.get_events(days=7)
        await service.get_events(days=14)
        assert service._client.get_events.await_count == 2

    @pytest.mark.asyncio
    async def test_clear_cache(self, service):
        await service.get_groups()
        await service.resolve_family_members()
        service.clear_cache()
        assert service._cache == {}
        assert service._family_members is None
        assert service._group_map is None


# --- SpondService family resolution ---


class TestFamilyResolution:
    @pytest.mark.asyncio
    async def test_resolves_oliver(self, service):
        family = await service.resolve_family_members()
        assert "MEMBER_OLIVER" in family
        assert family["MEMBER_OLIVER"] == "Oliver"

    @pytest.mark.asyncio
    async def test_resolves_emma(self, service):
        family = await service.resolve_family_members()
        assert "MEMBER_EMMA" in family
        assert family["MEMBER_EMMA"] == "Emma"

    @pytest.mark.asyncio
    async def test_does_not_resolve_coaches(self, service):
        family = await service.resolve_family_members()
        assert "MEMBER_COACH" not in family

    @pytest.mark.asyncio
    async def test_no_kids_config_returns_empty(self, service_no_kids):
        family = await service_no_kids.resolve_family_members()
        assert family == {}

    @pytest.mark.asyncio
    async def test_family_cached(self, service):
        await service.resolve_family_members()
        await service.resolve_family_members()
        # Groups only fetched once (family resolution uses groups)
        service._client.get_groups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_find_member_id(self, service):
        member_id = await service.find_member_id("Oliver")
        assert member_id == "MEMBER_OLIVER"

    @pytest.mark.asyncio
    async def test_find_member_id_case_insensitive(self, service):
        member_id = await service.find_member_id("oliver")
        assert member_id == "MEMBER_OLIVER"

    @pytest.mark.asyncio
    async def test_find_member_id_unknown(self, service):
        member_id = await service.find_member_id("Ukjent")
        assert member_id is None


# --- SpondService group matching ---


class TestGroupMatching:
    @pytest.mark.asyncio
    async def test_find_group_id_exact(self, service):
        groups = await service.get_groups()
        gid = service.find_group_id("Fjordvik FK G2013", groups)
        assert gid == "GROUP_FJORDVIK"

    @pytest.mark.asyncio
    async def test_find_group_id_partial(self, service):
        groups = await service.get_groups()
        gid = service.find_group_id("Solvik", groups)
        assert gid == "GROUP_SOLVIK"

    @pytest.mark.asyncio
    async def test_find_group_id_no_match(self, service):
        groups = await service.get_groups()
        gid = service.find_group_id("Nonexistent", groups)
        assert gid is None

    def test_get_kid_groups(self, service):
        groups = service.get_kid_group_names("Oliver")
        assert "Fjordvik FK G2013" in groups
        assert "Nordvik skole kull 2014" in groups

    def test_get_kid_groups_case_insensitive(self, service):
        groups = service.get_kid_group_names("emma")
        assert "Solvik IL 2017" in groups

    def test_get_kid_groups_unknown(self, service):
        groups = service.get_kid_group_names("Ukjent")
        assert groups == []


# --- Tool handlers via handle_tool_call ---


class TestListGroups:
    @pytest.mark.asyncio
    async def test_lists_all_groups(self, service):
        result = await handle_tool_call(service, "list_groups", {})
        assert "3 stk" in result
        assert "Fjordvik FK G2013" in result
        assert "Solvik IL 2017" in result
        assert "Nordvik skole kull 2014" in result

    @pytest.mark.asyncio
    async def test_member_counts(self, service):
        result = await handle_tool_call(service, "list_groups", {})
        assert "3 medlemmer" in result  # Fjordvik
        assert "2 medlemmer" in result  # Solvik
        assert "1 medlemmer" in result  # Nordvik


class TestGetUpcomingEvents:
    @pytest.mark.asyncio
    async def test_all_events(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {"days": 7})
        assert "3 stk" in result
        assert "Trening" in result
        assert "Seriekamp" in result

    @pytest.mark.asyncio
    async def test_filter_by_kid(self, service):
        result = await handle_tool_call(
            service, "get_upcoming_events", {"kid_name": "Oliver"}
        )
        assert "Oliver" in result
        assert "Seriekamp" in result

    @pytest.mark.asyncio
    async def test_filter_by_group(self, service):
        result = await handle_tool_call(
            service, "get_upcoming_events", {"group_name": "Solvik"}
        )
        assert "Nordvik minibane" in result

    @pytest.mark.asyncio
    async def test_unknown_kid(self, service):
        result = await handle_tool_call(
            service, "get_upcoming_events", {"kid_name": "Ukjent"}
        )
        assert "Ukjent barn" in result

    @pytest.mark.asyncio
    async def test_unknown_group(self, service):
        result = await handle_tool_call(
            service, "get_upcoming_events", {"group_name": "Nonexistent"}
        )
        assert "Fant ingen gruppe" in result

    @pytest.mark.asyncio
    async def test_includes_rsvp(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {"days": 7})
        assert "bekreftet" in result or "ikke svart" in result

    @pytest.mark.asyncio
    async def test_default_days(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {})
        assert "7 dager" in result


class TestGetEventDetails:
    @pytest.mark.asyncio
    async def test_returns_details(self, service):
        result = await handle_tool_call(
            service, "get_event_details", {"event_id": "EVT_KAMP_1"}
        )
        assert "Seriekamp" in result
        assert "Havnes" in result
        assert "59.8765" in result

    @pytest.mark.asyncio
    async def test_missing_event_id(self, service):
        result = await handle_tool_call(service, "get_event_details", {})
        assert "påkrevd" in result

    @pytest.mark.asyncio
    async def test_includes_attendance_stats(self, service):
        result = await handle_tool_call(
            service, "get_event_details", {"event_id": "EVT_KAMP_1"}
        )
        assert "Påmelding:" in result


class TestGetAttendance:
    @pytest.mark.asyncio
    async def test_shows_unanswered(self, service):
        result = await handle_tool_call(service, "get_attendance", {"kid_name": "Oliver"})
        assert "Seriekamp" in result
        assert "mangler svar" in result
        assert "Oliver" in result

    @pytest.mark.asyncio
    async def test_all_answered(self, service):
        result = await handle_tool_call(service, "get_attendance", {"kid_name": "Emma"})
        assert "besvart" in result

    @pytest.mark.asyncio
    async def test_unknown_kid(self, service):
        result = await handle_tool_call(service, "get_attendance", {"kid_name": "Ukjent"})
        assert "Ukjent barn" in result


class TestRespondToEvent:
    @pytest.mark.asyncio
    async def test_accept(self, service):
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": True},
        )
        assert "akseptert" in result
        assert "Oliver" in result
        service._client.change_response.assert_awaited_once_with(
            "EVT_KAMP_1", "MEMBER_OLIVER", {"accepted": "true"}
        )

    @pytest.mark.asyncio
    async def test_decline_with_message(self, service):
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {
                "event_id": "EVT_KAMP_1",
                "kid_name": "Oliver",
                "accept": False,
                "decline_message": "Syk",
            },
        )
        assert "avslått" in result
        service._client.change_response.assert_awaited_once_with(
            "EVT_KAMP_1", "MEMBER_OLIVER", {"accepted": "false", "declineMessage": "Syk"}
        )

    @pytest.mark.asyncio
    async def test_unknown_kid(self, service):
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Ukjent", "accept": True},
        )
        assert "Fant ikke" in result

    @pytest.mark.asyncio
    async def test_missing_params(self, service):
        result = await handle_tool_call(service, "respond_to_event", {})
        assert "påkrevd" in result


class TestSearchEvents:
    @pytest.mark.asyncio
    async def test_search_by_heading(self, service):
        result = await handle_tool_call(service, "search_events", {"query": "kamp"})
        assert "Seriekamp" in result
        assert "1 treff" in result

    @pytest.mark.asyncio
    async def test_search_by_description(self, service):
        result = await handle_tool_call(service, "search_events", {"query": "drikke"})
        assert "Trening" in result

    @pytest.mark.asyncio
    async def test_search_no_results(self, service):
        result = await handle_tool_call(service, "search_events", {"query": "dugnad"})
        assert "Ingen aktiviteter" in result

    @pytest.mark.asyncio
    async def test_case_insensitive_search(self, service):
        result = await handle_tool_call(service, "search_events", {"query": "KAMP"})
        assert "Seriekamp" in result

    @pytest.mark.asyncio
    async def test_missing_query(self, service):
        result = await handle_tool_call(service, "search_events", {})
        assert "påkrevd" in result


class TestUnknownTool:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, service):
        result = await handle_tool_call(service, "nonexistent", {})
        assert "Ukjent verktøy" in result


# --- Tool definitions ---


class TestToolDefinitions:
    def test_all_tools_have_names(self):
        for tool in TOOLS:
            assert "name" in tool
            assert tool["name"]

    def test_all_tools_have_descriptions(self):
        for tool in TOOLS:
            assert "description" in tool
            assert tool["description"]

    def test_all_tools_have_schemas(self):
        for tool in TOOLS:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_required_params_listed(self):
        event_detail = next(t for t in TOOLS if t["name"] == "get_event_details")
        assert "event_id" in event_detail["inputSchema"]["required"]

        respond = next(t for t in TOOLS if t["name"] == "respond_to_event")
        assert "event_id" in respond["inputSchema"]["required"]
        assert "kid_name" in respond["inputSchema"]["required"]
        assert "accept" in respond["inputSchema"]["required"]

    def test_expected_tool_count(self):
        assert len(TOOLS) == 6

    def test_tool_names(self):
        names = {t["name"] for t in TOOLS}
        assert names == {
            "list_groups",
            "get_upcoming_events",
            "get_event_details",
            "get_attendance",
            "respond_to_event",
            "search_events",
        }


# --- Service lifecycle ---


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_close(self, service):
        await service.close()
        service._client.clientsession.close.assert_awaited_once()


# --- Bearer auth middleware ---


class TestBearerAuth:
    @pytest.fixture(autouse=True)
    def _patch_token(self, monkeypatch):
        monkeypatch.setattr("main.AUTH_TOKEN", "test-secret-token")

    @pytest.fixture
    def client(self):
        from starlette.testclient import TestClient
        from main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_health_no_auth_required(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_sse_rejected_without_token(self, client):
        resp = client.get("/sse")
        assert resp.status_code == 401
        assert resp.text == "unauthorized"

    def test_sse_rejected_with_wrong_token(self, client):
        resp = client.get("/sse", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_sse_rejected_with_malformed_header(self, client):
        resp = client.get("/sse", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_messages_rejected_without_token(self, client):
        resp = client.post("/messages/")
        assert resp.status_code == 401

    def test_accepted_with_valid_token(self, client):
        # Test that valid token passes through middleware (not 401)
        # Use /messages/ since /sse is a long-lived streaming endpoint
        resp = client.post("/messages/", headers={"Authorization": "Bearer test-secret-token"})
        assert resp.status_code != 401


class TestBearerAuthDisabled:
    """When MCP_AUTH_TOKEN is empty, all requests pass through."""

    @pytest.fixture(autouse=True)
    def _patch_token(self, monkeypatch):
        monkeypatch.setattr("main.AUTH_TOKEN", "")

    @pytest.fixture
    def client(self):
        from starlette.testclient import TestClient
        from main import app
        return TestClient(app, raise_server_exceptions=False)

    def test_messages_allowed_without_token(self, client):
        resp = client.post("/messages/")
        assert resp.status_code != 401

    def test_health_still_works(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
