"""Shared pytest fixtures with realistic mock Spond data."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from server import SpondService

# --- Mock data: groups ---

MOCK_GROUPS = [
    {
        "id": "GROUP_FJORDVIK",
        "name": "Fjordvik FK G2013",
        "members": [
            {
                "id": "MEMBER_OLIVER",
                "firstName": "Oliver",
                "lastName": "Nordmann",
                "roles": ["member"],
            },
            {
                "id": "MEMBER_PARENT",
                "firstName": "Ola",
                "lastName": "Nordmann",
                "email": "ola@example.com",
                "roles": ["admin"],
            },
            {
                "id": "MEMBER_COACH",
                "profile": {"id": "PROFILE_COACH"},
                "firstName": "Trener",
                "lastName": "Hansen",
                "roles": ["admin"],
            },
        ],
        "subGroups": [],
    },
    {
        "id": "GROUP_SOLVIK",
        "name": "Solvik IL 2017",
        "members": [
            {
                "id": "MEMBER_EMMA",
                "firstName": "Emma",
                "lastName": "Nordmann",
                "roles": ["member"],
            },
            {
                "id": "MEMBER_PARENT_B",
                "firstName": "Ola",
                "lastName": "Nordmann",
                "email": "ola@example.com",
                "roles": ["admin"],
            },
        ],
        "subGroups": [],
    },
    {
        "id": "GROUP_NORDVIK",
        "name": "Nordvik skole kull 2014",
        "members": [
            {
                "id": "MEMBER_OLIVER",
                "firstName": "Oliver",
                "lastName": "Nordmann",
                "roles": ["member"],
            },
        ],
        "subGroups": [],
    },
]

# --- Mock data: events ---

MOCK_EVENTS_FJORDVIK = [
    {
        "id": "EVT_TRENING_1",
        "heading": "Trening",
        "type": "RECURRING",
        "startTimestamp": "2026-02-23T17:00:00.000+00:00",
        "endTimestamp": "2026-02-23T18:30:00.000+00:00",
        "cancelled": False,
        "description": "Vanlig trening, ta med drikke",
        "location": {
            "feature": "Fjordvik kunstgress",
            "address": "Idrettsvegen 12, 0001 Fjordvik",
            "latitude": 59.9127,
            "longitude": 10.7461,
        },
        "responses": {
            "acceptedIds": ["MEMBER_OLIVER"],
            "declinedIds": [],
            "unansweredIds": ["MEMBER_COACH"],
            "waitinglistIds": [],
            "unconfirmedIds": [],
        },
        "recipients": {"group": {"id": "GROUP_FJORDVIK"}},
    },
    {
        "id": "EVT_KAMP_1",
        "heading": "Seriekamp vs Havnes",
        "type": "EVENT",
        "startTimestamp": "2026-02-25T18:00:00.000+00:00",
        "endTimestamp": "2026-02-25T19:30:00.000+00:00",
        "rsvpDate": "2026-02-24T12:00:00.000+00:00",
        "cancelled": False,
        "description": "Oppmøte 17:30. Husk drakt og leggskinn.",
        "location": {
            "feature": "Havnes Arena",
            "address": "Sjøgata 5, 0002 Havnes",
            "latitude": 59.8765,
            "longitude": 10.8123,
        },
        "responses": {
            "acceptedIds": [],
            "declinedIds": [],
            "unansweredIds": ["MEMBER_OLIVER"],
            "waitinglistIds": [],
            "unconfirmedIds": [],
        },
        "recipients": {"group": {"id": "GROUP_FJORDVIK"}},
    },
]

MOCK_EVENTS_SOLVIK = [
    {
        "id": "EVT_FOTBALL_1",
        "heading": "Trening",
        "type": "RECURRING",
        "startTimestamp": "2026-02-24T16:30:00.000+00:00",
        "endTimestamp": "2026-02-24T17:30:00.000+00:00",
        "cancelled": False,
        "description": "",
        "location": {
            "feature": "Nordvik minibane",
        },
        "responses": {
            "acceptedIds": ["MEMBER_EMMA"],
            "declinedIds": [],
            "unansweredIds": [],
            "waitinglistIds": [],
            "unconfirmedIds": [],
        },
        "recipients": {"group": {"id": "GROUP_SOLVIK"}},
    },
]

MOCK_EVENT_CANCELLED = {
    "id": "EVT_CANCELLED",
    "heading": "Trening",
    "type": "RECURRING",
    "startTimestamp": "2026-02-26T17:00:00.000+00:00",
    "endTimestamp": "2026-02-26T18:30:00.000+00:00",
    "cancelled": True,
    "description": "Avlyst pga. snø",
    "location": {"feature": "Fjordvik kunstgress"},
    "responses": {
        "acceptedIds": [],
        "declinedIds": [],
        "unansweredIds": [],
        "waitinglistIds": [],
        "unconfirmedIds": [],
    },
    "recipients": {"group": {"id": "GROUP_FJORDVIK"}},
}

MOCK_EVENT_DETAIL = {
    **MOCK_EVENTS_FJORDVIK[1],
    "description": "Oppmøte 17:30. Husk drakt og leggskinn.\n\nRød drakt, hvite shorts.",
    "rsvpDate": "2026-02-24T12:00:00.000+00:00",
}

MOCK_ALL_EVENTS = MOCK_EVENTS_FJORDVIK + MOCK_EVENTS_SOLVIK

KIDS_CONFIG = [
    {"name": "Oliver", "groups": ["Fjordvik FK G2013", "Nordvik skole kull 2014"]},
    {"name": "Emma", "groups": ["Solvik IL 2017"]},
]

MOCK_POSTS = [
    {
        "id": "POST_1",
        "title": "Drikkeflasker",
        "body": "Husk å ta med egne drikkebokser til kampen på lørdag. Vi har ikke engangskopper.",
        "timestamp": "2026-02-22T10:30:00.000Z",
        "groupId": "GROUP_FJORDVIK",
        "ownerId": "PROFILE_COACH",
        "comments": [
            {"id": "COMMENT_1", "fromProfileId": "MEMBER_PARENT", "text": "Notert!",
             "timestamp": "2026-02-22T11:00:00.000Z", "children": []},
        ],
    },
    {
        "id": "POST_2",
        "body": "Treningene flyttes innendørs fra neste uke pga. vær. Møt opp i hallen kl 17:00.",
        "timestamp": "2026-02-21T14:00:00.000Z",
        "groupId": "GROUP_FJORDVIK",
        "ownerId": "PROFILE_COACH",
    },
    {
        "id": "POST_3",
        "body": "Velkommen til ny sesong! Første trening er mandag.",
        "timestamp": "2026-02-20T09:00:00.000Z",
        "groupId": "GROUP_SOLVIK",
        "ownerId": "MEMBER_PARENT_B",
        "comments": [
            {"id": "COMMENT_2", "fromProfileId": "MEMBER_PARENT", "text": "Hurra!",
             "timestamp": "2026-02-20T09:30:00.000Z",
             "children": [{"id": "COMMENT_3", "fromProfileId": "PROFILE_COACH", "text": "Gleder oss!",
                           "timestamp": "2026-02-20T10:00:00.000Z"}]},
        ],
    },
]

MOCK_CHATS = [
    {"id": "CHAT_DM", "type": "DM", "participants": ["PROFILE_COACH", "MEMBER_PARENT"], "unread": True,
     "message": {"chatId": "CHAT_DM", "msgNum": 2, "user": "PROFILE_COACH", "text": "Kan du kjøre?",
                 "type": "TEXT", "timestamp": "2026-02-22T18:00:00.000Z"}},
    {"id": "CHAT_GROUP", "name": "Foreldre G2013", "type": "GROUP", "participants": [], "unread": False,
     "message": {"chatId": "CHAT_GROUP", "msgNum": 7, "type": "SPOND", "timestamp": "2026-02-21T08:00:00.000Z"}},
]

MOCK_MESSAGES = [
    {"chatId": "CHAT_DM", "msgNum": 2, "user": "PROFILE_COACH", "text": "Kan du kjøre?", "type": "TEXT",
     "timestamp": "2026-02-22T18:00:00.000Z"},
    {"chatId": "CHAT_DM", "msgNum": 1, "user": "MEMBER_PARENT", "text": "Hei!", "type": "TEXT",
     "timestamp": "2026-02-22T17:00:00.000Z"},
]


def mock_activities():
    from datetime import UTC, datetime, timedelta

    def ago(hours):
        return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    return [
        {"id": "A1", "type": "new_post", "timestamp": ago(1), "post": {"id": "POST_1"},
         "group": {"id": "GROUP_FJORDVIK", "name": "Fjordvik FK G2013"},
         "user": {"id": "PROFILE_COACH", "name": "Trener Hansen"}},
        {"id": "A2", "type": "post_reaction", "timestamp": ago(2), "post": {"id": "POST_1"},
         "group": {"id": "GROUP_FJORDVIK"}, "user": {"id": "MEMBER_PARENT"}},
        {"id": "A3", "type": "schedule_event", "timestamp": ago(3), "spond": {"id": "EVENT_1"},
         "group": {"id": "GROUP_SOLVIK"}, "user": {"id": "MEMBER_PARENT_B"}},
        {"id": "A4", "type": "chat_mention", "timestamp": ago(100), "chat": {"id": "CHAT_DM", "chatNum": 1},
         "user": {"id": "PROFILE_COACH"}},
    ]


@pytest.fixture
def mock_spond_client():
    """Mock Spond client with pre-configured responses."""
    client = AsyncMock()
    client.get_groups = AsyncMock(return_value=MOCK_GROUPS)

    async def mock_get_events(min_start=None, max_end=None, group_id=None, **kwargs):
        if group_id == "GROUP_FJORDVIK":
            return MOCK_EVENTS_FJORDVIK
        elif group_id == "GROUP_SOLVIK":
            return MOCK_EVENTS_SOLVIK
        elif group_id == "GROUP_NORDVIK":
            return []
        return MOCK_ALL_EVENTS

    client.get_events = AsyncMock(side_effect=mock_get_events)
    client.get_event = AsyncMock(return_value=MOCK_EVENT_DETAIL)
    client.change_response = AsyncMock(return_value={"acceptedIds": ["MEMBER_OLIVER"], "declinedIds": []})

    async def mock_get_posts(group_id=None, max_posts=20, include_comments=True):
        posts = MOCK_POSTS
        if group_id:
            posts = [p for p in posts if p.get("groupId") == group_id]
        return posts[:max_posts]

    client.get_posts = AsyncMock(side_effect=mock_get_posts)
    client.get_messages = AsyncMock(return_value=MOCK_CHATS)
    client.clientsession = MagicMock()
    client.clientsession.close = AsyncMock()
    return client


@pytest.fixture
def service(mock_spond_client):
    """SpondService with mock client and kids config."""
    svc = SpondService(client=mock_spond_client, kids_config=KIDS_CONFIG)

    async def api_get(path, params=None, chat=False):
        if path == "activities":
            return mock_activities()
        if path == "chats/CHAT_DM/messages":
            return MOCK_MESSAGES
        raise RuntimeError(f"Spond svarte 404 på {path}")

    svc._api_get = AsyncMock(side_effect=api_get)
    return svc


@pytest.fixture
def service_no_kids(mock_spond_client):
    """SpondService with mock client but no kids config."""
    return SpondService(client=mock_spond_client, kids_config=[])
