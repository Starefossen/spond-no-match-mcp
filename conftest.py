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
}

MOCK_ALL_EVENTS = MOCK_EVENTS_FJORDVIK + MOCK_EVENTS_SOLVIK

KIDS_CONFIG = [
    {"name": "Oliver", "groups": ["Fjordvik FK G2013", "Nordvik skole kull 2014"]},
    {"name": "Emma", "groups": ["Solvik IL 2017"]},
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
    client.change_response = AsyncMock()
    client.clientsession = MagicMock()
    client.clientsession.close = AsyncMock()
    return client


@pytest.fixture
def service(mock_spond_client):
    """SpondService with mock client and kids config."""
    return SpondService(client=mock_spond_client, kids_config=KIDS_CONFIG)


@pytest.fixture
def service_no_kids(mock_spond_client):
    """SpondService with mock client but no kids config."""
    return SpondService(client=mock_spond_client, kids_config=[])
