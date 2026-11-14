"""Unit tests for Spond MCP server."""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

import server
from conftest import (
    MOCK_EVENT_CANCELLED,
    MOCK_EVENT_DETAIL,
    MOCK_EVENTS_FJORDVIK,
    MOCK_GROUPS,
    MOCK_POSTS,
)
from server import (
    TOOLS,
    format_event_detail,
    format_event_summary,
    format_group_summary,
    format_post_summary,
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
        # UTC+1 in February (CET) → 18:00 local
        assert dt.hour == 18
        assert dt.day == 23

    def test_z_suffix(self):
        dt = parse_timestamp("2026-02-23T17:00:00Z")
        assert dt is not None
        # UTC+1 in February (CET) → 18:00 local
        assert dt.hour == 18

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
        # UTC 17:00-18:30 → CET 18:00-19:30
        assert "18:00-19:30" in result
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

    def test_rsvp_date_shown(self):
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[1])
        assert "Svarfrist:" in result
        assert "24.02" in result
        # UTC 12:00 → CET 13:00
        assert "13:00" in result

    def test_no_rsvp_date(self):
        result = format_event_summary(MOCK_EVENTS_FJORDVIK[0])
        assert "Svarfrist:" not in result


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

    def test_includes_rsvp_date(self):
        result = format_event_detail(MOCK_EVENT_DETAIL)
        assert "Svarfrist:" in result
        assert "24.02" in result


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
    async def test_events_different_from_days_not_cached(self, service):
        await service.get_events(days=14)
        await service.get_events(days=14, from_days=7)
        assert service._client.get_events.await_count == 2

    @pytest.mark.asyncio
    async def test_from_days_shifts_min_start(self, service):
        await service.get_events(days=56, from_days=7)
        call_kwargs = service._client.get_events.call_args
        min_start = call_kwargs.kwargs["min_start"]
        max_end = call_kwargs.kwargs["max_end"]
        diff = (max_end - min_start).days
        assert diff == 56  # days is the window size

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
        result = await handle_tool_call(service, "get_upcoming_events", {"kid_name": "Oliver"})
        assert "Oliver" in result
        assert "Seriekamp" in result

    @pytest.mark.asyncio
    async def test_filter_by_group(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {"group_name": "Solvik"})
        assert "Nordvik minibane" in result

    @pytest.mark.asyncio
    async def test_unknown_kid(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {"kid_name": "Ukjent"})
        assert "Ukjent barn" in result

    @pytest.mark.asyncio
    async def test_unknown_group(self, service):
        result = await handle_tool_call(service, "get_upcoming_events", {"group_name": "Nonexistent"})
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
        result = await handle_tool_call(service, "get_event_details", {"event_id": "EVT_KAMP_1"})
        assert "Seriekamp" in result
        assert "Havnes" in result
        assert "59.8765" in result

    @pytest.mark.asyncio
    async def test_missing_event_id(self, service):
        result = await handle_tool_call(service, "get_event_details", {})
        assert "påkrevd" in result

    @pytest.mark.asyncio
    async def test_includes_attendance_stats(self, service):
        result = await handle_tool_call(service, "get_event_details", {"event_id": "EVT_KAMP_1"})
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
        service._client.change_response.assert_awaited_once_with("EVT_KAMP_1", "MEMBER_OLIVER", {"accepted": "true"})

    @pytest.mark.asyncio
    async def test_decline_with_message(self, service):
        service._client.change_response.return_value = {"acceptedIds": [], "declinedIds": ["MEMBER_OLIVER"]}
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

    @pytest.mark.asyncio
    async def test_api_error(self, service):
        service._client.change_response.return_value = {"error": "Unauthorized"}
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": True},
        )
        assert "feil" in result.lower()
        assert "Unauthorized" not in result

    @pytest.mark.asyncio
    async def test_exception_hides_upstream_text(self, service):
        service._client.change_response.side_effect = RuntimeError("401 body={secret-token}")
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": True},
        )
        assert "secret-token" not in result
        assert "Kunne ikke sende svar for Oliver" in result

    @pytest.mark.asyncio
    async def test_response_not_confirmed(self, service):
        service._client.change_response.return_value = {"acceptedIds": [], "declinedIds": []}
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": True},
        )
        assert "ikke bekrefte" in result.lower() or "sjekk" in result.lower()

    @pytest.mark.asyncio
    async def test_decline_not_confirmed(self, service):
        """Phantom decline: API returns empty declinedIds — must NOT claim success."""
        service._client.change_response.return_value = {"acceptedIds": [], "declinedIds": []}
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": False},
        )
        assert "avslått" not in result.lower() or "ikke bekrefte" in result.lower()
        assert "sjekk" in result.lower()

    @pytest.mark.asyncio
    async def test_decline_without_message(self, service):
        """Decline without decline_message should still verify via declinedIds."""
        service._client.change_response.return_value = {"acceptedIds": [], "declinedIds": ["MEMBER_OLIVER"]}
        result = await handle_tool_call(
            service,
            "respond_to_event",
            {"event_id": "EVT_KAMP_1", "kid_name": "Oliver", "accept": False},
        )
        assert "avslått" in result.lower()
        assert "sjekk" not in result.lower()


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


# --- Posts ---


class TestFormatPostSummary:
    def test_basic_post(self):
        post = MOCK_POSTS[0]
        group_map = {"GROUP_FJORDVIK": "Fjordvik FK G2013"}
        result = format_post_summary(post, group_map, {"PROFILE_COACH": "Trener Hansen"})
        assert "Trener" in result
        assert "Drikkeflasker" in result
        assert "Fjordvik FK G2013" in result
        assert "drikkebokser" in result
        assert "1 kommentar" in result

    def test_post_without_comments(self):
        post = MOCK_POSTS[1]
        result = format_post_summary(post, {}, {"PROFILE_COACH": "Trener Hansen"})
        assert "Trener" in result
        assert "innendørs" in result
        assert "kommentar" not in result

    def test_post_with_multiple_comments(self):
        post = MOCK_POSTS[2]
        result = format_post_summary(post, {})
        assert "2 kommentarer" in result

    def test_long_body_truncated(self):
        post = {**MOCK_POSTS[0], "body": "x" * 400}
        result = format_post_summary(post, {})
        assert len([line for line in result.split("\n") if "x" in line][0]) <= 240

    def test_no_maps(self):
        result = format_post_summary(MOCK_POSTS[0], None)
        assert "Ukjent" in result
        assert "[id: POST_1]" in result


class TestGetPosts:
    @pytest.mark.asyncio
    async def test_all_posts(self, service):
        result = await handle_tool_call(service, "get_posts", {"since_days": 36500})
        assert "Innlegg (3 stk)" in result
        assert "drikkebokser" in result
        assert "innendørs" in result
        assert "Velkommen" in result

    @pytest.mark.asyncio
    async def test_filter_by_group(self, service):
        result = await handle_tool_call(service, "get_posts", {"group_name": "Fjordvik", "since_days": 36500})
        assert "drikkebokser" in result
        assert "Velkommen" not in result

    @pytest.mark.asyncio
    async def test_unknown_group(self, service):
        result = await handle_tool_call(service, "get_posts", {"group_name": "Finnesikke"})
        assert "Fant ingen gruppe" in result

    @pytest.mark.asyncio
    async def test_max_posts(self, service):
        result = await handle_tool_call(service, "get_posts", {"max_posts": 1, "since_days": 36500})
        assert "Innlegg (1 stk)" in result

    @pytest.mark.asyncio
    async def test_posts_cached(self, service):
        await service.get_posts()
        await service.get_posts()
        assert service._client.get_posts.await_count == 1

    @pytest.mark.asyncio
    async def test_posts_include_group_name(self, service):
        result = await handle_tool_call(service, "get_posts", {"since_days": 36500})
        assert "Fjordvik FK G2013" in result

    @pytest.mark.asyncio
    async def test_since_days_default_drops_old(self, service):
        assert "Ingen innlegg siste 14 dager" in await handle_tool_call(service, "get_posts", {})

    @pytest.mark.asyncio
    async def test_since_days_keeps_recent(self, service):
        fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        service._client.get_posts.side_effect = None
        service._client.get_posts.return_value = [{**MOCK_POSTS[0], "timestamp": fresh}, MOCK_POSTS[1]]
        result = await handle_tool_call(service, "get_posts", {})
        assert "Innlegg (1 stk)" in result


class TestPostComments:
    async def test_threaded_comments(self, service):
        result = await handle_tool_call(service, "get_post_comments", {"post_id": "POST_3"})
        assert "Hurra!" in result
        assert "↳" in result and "Trener Hansen: Gleder oss!" in result

    async def test_no_comments(self, service):
        result = await handle_tool_call(service, "get_post_comments", {"post_id": "POST_2"})
        assert "Ingen kommentarer" in result

    async def test_unknown_post(self, service):
        result = await handle_tool_call(service, "get_post_comments", {"post_id": "NOPE"})
        assert "Fant ikke innlegget" in result

    async def test_missing_id(self, service):
        assert "påkrevd" in await handle_tool_call(service, "get_post_comments", {})


class TestChats:
    async def test_chat_list(self, service):
        result = await handle_tool_call(service, "get_chats", {})
        assert "Trener Hansen, Ola Nordmann (privat" in result
        assert "ulest" in result
        assert "Trener Hansen: Kan du kjøre?" in result
        assert "Foreldre G2013 (gruppe" in result

    async def test_messages_oldest_first(self, service):
        result = await handle_tool_call(service, "get_chat_messages", {"chat_id": "CHAT_DM"})
        assert result.index("Hei!") < result.index("Kan du kjøre?")
        assert "Ola Nordmann: Hei!" in result

    async def test_limit_capped(self, service):
        await handle_tool_call(service, "get_chat_messages", {"chat_id": "CHAT_DM", "limit": 500})
        assert service._api_get.await_args.args[1] == {"max": "10"}

    async def test_limit_keeps_newest(self, service):
        result = await handle_tool_call(service, "get_chat_messages", {"chat_id": "CHAT_DM", "limit": 1})
        assert "Kan du kjøre?" in result and "Hei!" not in result

    async def test_api_error_propagates(self, service):
        with pytest.raises(RuntimeError, match="404"):
            await handle_tool_call(service, "get_chat_messages", {"chat_id": "NOPE"})


class TestUpdates:
    async def test_skips_reactions_and_old(self, service):
        result = await handle_tool_call(service, "get_updates", {})
        assert "nytt innlegg i Fjordvik FK G2013 av Trener Hansen [post_id: POST_1]" in result
        assert "ny aktivitet i Solvik IL 2017 av Ola Nordmann [event_id: EVENT_1]" in result
        assert "reaction" not in result
        assert "CHAT_DM" not in result

    async def test_dedupes_same_kind_target_actor(self, service):
        result = await handle_tool_call(service, "get_updates", {})
        assert result.count("[post_id: POST_1]") == 1
        assert "[post_id: POST_1] (×2)" in result
        assert "(2 stk" in result

    async def test_since_hours(self, service):
        result = await handle_tool_call(service, "get_updates", {"since_hours": 200})
        assert "nevnt i chat" in result

    async def test_group_filter(self, service):
        result = await handle_tool_call(service, "get_updates", {"group_name": "Solvik"})
        assert "EVENT_1" in result and "POST_1" not in result

    async def test_nothing_new(self, service):
        result = await handle_tool_call(service, "get_updates", {"since_hours": 0})
        assert "Ingen nye hendelser" in result


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
        assert len(TOOLS) == 12

    def test_tool_names(self):
        names = {t["name"] for t in TOOLS}
        assert names == {
            "list_groups",
            "get_upcoming_events",
            "get_event_details",
            "get_attendance",
            "respond_to_event",
            "search_events",
            "get_posts",
            "get_chats",
            "get_chat_messages",
            "get_post_comments",
            "get_updates",
            "get_attachment",
        }


# --- Service lifecycle ---


class TestServiceLifecycle:
    @pytest.mark.asyncio
    async def test_close(self, service):
        await service.close()
        service._client.clientsession.close.assert_awaited_once()


# --- Bearer auth middleware ---


@pytest.fixture
def callers_dir(tmp_path):
    (tmp_path / "read").mkdir()
    (tmp_path / "read" / "varden").write_text("test-secret-token-xyz\n")
    return tmp_path


class TestBearerAuth:
    @pytest.fixture
    def client(self, callers_dir):
        from starlette.testclient import TestClient

        import mcpauth
        from main import build_app

        return TestClient(build_app(mcpauth.load(str(callers_dir))), raise_server_exceptions=False)

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

    def test_sse_rejected_with_query_token(self, client):
        assert client.get("/sse?token=test-secret-token-xyz").status_code == 401

    def test_sse_rejected_with_malformed_header(self, client):
        resp = client.get("/sse", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_messages_rejected_without_token(self, client):
        resp = client.post("/messages/")
        assert resp.status_code == 401

    def test_accepted_with_valid_token(self, client):
        resp = client.post("/messages/", headers={"Authorization": "Bearer test-secret-token-xyz"})
        assert resp.status_code != 401


def test_load_refuses_missing_config(tmp_path):
    import mcpauth

    with pytest.raises(ValueError):
        mcpauth.load(None)
    with pytest.raises(ValueError):
        mcpauth.load(str(tmp_path))


class TestPostBodyTruncation:
    LONG = "Husk " + "ord " * 100 + "SLUTT"

    def test_list_marks_truncated(self):
        out = format_post_summary({"id": "p", "body": self.LONG})
        assert "(avkortet, se get_post_comments)" in out
        assert "SLUTT" not in out

    def test_short_body_unmarked(self):
        assert "avkortet" not in format_post_summary({"id": "p", "body": "kort"})

    def test_full_body(self):
        out = format_post_summary({"id": "p", "body": self.LONG}, full=True)
        assert "SLUTT" in out
        assert "avkortet" not in out


class TestAttachments:
    PDF_URL = "https://spond.com/storage/upload/ABC/Terminliste.pdf"
    POST = {
        "id": "POST_M",
        "title": "Kampbilder",
        "timestamp": "2026-02-22T10:30:00.000Z",
        "media": [
            {"type": "IMAGE", "url": "https://spond.com/storage/upload/X/kamp.jpg", "width": 800, "height": 600},
            {"type": "VIDEO", "url": "https://spond.com/storage/upload/Y/maal.m3u8", "thumbnailUrl": "t"},
        ],
        "comments": [{"id": "C1", "text": "Se bilde", "image": {"url": "https://spond.com/storage/b/bilde.jpg"}}],
    }
    MSG = {
        "msgNum": 7,
        "user": "X",
        "text": "Terminliste",
        "clubMessage": {
            "clubImage": "https://spond.com/storage/logo.png",
            "attachments": [{"id": "A1", "title": "Terminliste.pdf", "type": 2, "media": PDF_URL}],
        },
        "images": [{"url": "https://spond.com/storage/c/foto.jpg", "width": 1, "height": 1}],
    }

    @pytest.fixture
    def att_service(self, service):
        service.get_post = AsyncMock(side_effect=lambda pid: self.POST if pid == "POST_M" else None)
        service.get_chat_messages = AsyncMock(return_value=[self.MSG])
        return service

    @pytest.fixture
    def mock_http(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path == "/v1/convert/file":
                return httpx.Response(200, json={"document": {"text_content": "x" * 15010}})
            return httpx.Response(200, content=b"%PDF-1.4")

        monkeypatch.setattr(server, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return calls

    def test_formatting_lines(self):
        post = format_post_summary(self.POST)
        assert "📎 kamp.jpg [vedlegg: post:POST_M:0]" in post
        assert "📎 maal.m3u8 [vedlegg: post:POST_M:1]" in post
        lines = server.format_comment(self.POST["comments"][0], {}, post_id="POST_M")
        assert lines[1] == "  📎 bilde.jpg [vedlegg: comment:POST_M:C1:0]"
        msg = server.format_message_summary(self.MSG, {}, "CH")
        assert "  📎 Terminliste.pdf [vedlegg: chat:CH:7:0]" in msg
        assert "  📎 foto.jpg [vedlegg: chat:CH:7:1]" in msg
        assert "logo" not in msg
        assert "📎" not in server.format_message_summary({"text": "hei"}, {}, "CH")

    async def test_chat_pdf_converted_and_capped(self, att_service, mock_http, monkeypatch):
        monkeypatch.setenv("DOCLING_URL", "https://docling.test")
        result = await handle_tool_call(att_service, "get_attachment", {"id": "chat:CH:7:0"})
        assert result.startswith("📎 Terminliste.pdf (PDF)")
        assert result.endswith("… (avkortet, 10 tegn til)")
        assert [c.url.host for c in mock_http] == ["spond.com", "docling.test"]

    async def test_no_docling(self, att_service, mock_http, monkeypatch):
        monkeypatch.delenv("DOCLING_URL", raising=False)
        result = await handle_tool_call(att_service, "get_attachment", {"id": "chat:CH:7:0"})
        assert "PDF-lesing er ikke satt opp" in result
        assert f"Lenke: {self.PDF_URL}" in result
        assert not mock_http

    async def test_images_and_video_link_only(self, att_service, mock_http):
        result = await handle_tool_call(att_service, "get_attachment", {"id": "comment:POST_M:C1:0"})
        assert result == "📎 bilde.jpg (bilde)\nLenke: https://spond.com/storage/b/bilde.jpg"
        assert "(video)" in await handle_tool_call(att_service, "get_attachment", {"id": "post:POST_M:1"})
        assert "(bilde)" in await handle_tool_call(att_service, "get_attachment", {"id": "chat:CH:7:1"})
        assert not mock_http

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com/x.pdf",
            "http://spond.com/storage/x.pdf",
            "https://spond.com@evil/x.pdf",
            "https://evil/spond.com",
            "https://spond.com.evil/x.pdf",
            "https://spond.com:8443/x.pdf",
            "file:///etc/passwd",
        ],
    )
    async def test_fetch_denies_foreign_urls(self, url):
        calls = []
        transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, content=b"x"))
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(server.AttachmentRejected):
                await server.fetch_attachment(http, url)
        assert not calls

    async def test_fetch_redirects(self):
        def handler(r):
            if r.url.path == "/storage/a.pdf":
                return httpx.Response(302, headers={"location": "/storage/ok.pdf"})
            if r.url.path == "/storage/b.pdf":
                return httpx.Response(302, headers={"location": "https://evil.com/c.pdf"})
            return httpx.Response(200, content=b"secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await server.fetch_attachment(http, "https://spond.com/storage/a.pdf") == b"secret"
            with pytest.raises(server.AttachmentRejected):
                await server.fetch_attachment(http, "https://spond.com/storage/b.pdf")

    async def test_fetch_size_cap_and_ok(self, monkeypatch):
        monkeypatch.setattr(server, "ATTACHMENT_MAX_BYTES", 10)
        body = {"/storage/big.pdf": b"x" * 11, "/storage/x.pdf": b"%PDF-1.4"}
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=body[r.url.path]))
        async with httpx.AsyncClient(transport=transport) as http:
            assert await server.fetch_attachment(http, "https://spond.com/storage/x.pdf") == b"%PDF-1.4"
            with pytest.raises(server.AttachmentRejected):
                await server.fetch_attachment(http, "https://spond.com/storage/big.pdf")

    async def test_foreign_pdf_link_not_fetched(self, att_service, mock_http, monkeypatch):
        monkeypatch.setenv("DOCLING_URL", "https://docling.test")
        msg = {**self.MSG, "clubMessage": {"attachments": [{"title": "a.pdf", "type": 2, "media": "https://evil.com/a.pdf"}]}}
        att_service.get_chat_messages = AsyncMock(return_value=[msg])
        result = await handle_tool_call(att_service, "get_attachment", {"id": "chat:CH:7:0"})
        assert "Kan ikke lese filen" in result and "Lenke: https://evil.com/a.pdf" in result
        assert not mock_http

    async def test_bad_ids(self, att_service):
        assert "Ugyldig" in await handle_tool_call(att_service, "get_attachment", {"id": "foo"})
        assert "Fant ikke vedlegg" in await handle_tool_call(att_service, "get_attachment", {"id": "post:POST_M:5"})
        assert "påkrevd" in await handle_tool_call(att_service, "get_attachment", {})
