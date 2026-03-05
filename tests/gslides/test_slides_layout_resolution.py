"""
Unit tests for Google Slides layout resolution logic.

Tests _get_presentation_layouts and _resolve_create_slide_layouts which handle
presentations with custom themes where predefined layouts (e.g. BLANK) may not exist.
"""

import pytest
from unittest.mock import Mock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gslides.slides_tools import _get_presentation_layouts, _resolve_create_slide_layouts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_service(layouts):
    """Create a mock Slides service that returns the given layouts."""
    mock_service = Mock()
    mock_service.presentations().get().execute.return_value = {"layouts": layouts}
    return mock_service


def _make_layout(object_id, display_name):
    """Create a layout dict matching Slides API shape."""
    return {
        "objectId": object_id,
        "layoutProperties": {"displayName": display_name},
    }


def _create_slide_req(predefined_layout):
    """Create a minimal createSlide request with a predefinedLayout."""
    return {
        "createSlide": {
            "slideLayoutReference": {
                "predefinedLayout": predefined_layout,
            }
        }
    }


# ---------------------------------------------------------------------------
# _get_presentation_layouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_layouts_maps_blank():
    """A layout named 'Blank' resolves to the BLANK predefined name."""
    service = _make_mock_service([_make_layout("layout_001", "Blank")])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result["BLANK"] == "layout_001"


@pytest.mark.asyncio
async def test_get_layouts_maps_multiple():
    """Multiple standard layouts are resolved correctly."""
    service = _make_mock_service([
        _make_layout("l1", "Blank"),
        _make_layout("l2", "Title Slide"),
        _make_layout("l3", "Section Header"),
    ])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result["BLANK"] == "l1"
    assert result["TITLE"] == "l2"
    assert result["SECTION_HEADER"] == "l3"


@pytest.mark.asyncio
async def test_get_layouts_empty_presentation():
    """A presentation with no layouts returns an empty map."""
    service = _make_mock_service([])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result == {}


@pytest.mark.asyncio
async def test_get_layouts_no_layouts_key():
    """A presentation response missing 'layouts' returns an empty map."""
    mock_service = Mock()
    mock_service.presentations().get().execute.return_value = {}
    result = await _get_presentation_layouts(mock_service, "pres_123")
    assert result == {}


@pytest.mark.asyncio
async def test_get_layouts_custom_theme_no_standard_names():
    """Custom theme layouts with non-standard names don't match any predefined."""
    service = _make_mock_service([
        _make_layout("c1", "Corporate Hero"),
        _make_layout("c2", "Data Viz"),
        _make_layout("c3", "Team Photo"),
    ])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result == {}


@pytest.mark.asyncio
async def test_get_layouts_case_insensitive():
    """Layout name matching is case-insensitive."""
    service = _make_mock_service([_make_layout("l1", "BLANK")])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result["BLANK"] == "l1"


@pytest.mark.asyncio
async def test_get_layouts_big_number_not_stolen_by_main_point():
    """BIG_NUMBER and MAIN_POINT don't collide on 'big number' hint."""
    service = _make_mock_service([
        _make_layout("l1", "Main Point"),
        _make_layout("l2", "Big Number"),
    ])
    result = await _get_presentation_layouts(service, "pres_123")
    assert result["MAIN_POINT"] == "l1"
    assert result["BIG_NUMBER"] == "l2"


@pytest.mark.asyncio
async def test_get_layouts_uses_fields_parameter():
    """Verify the API call uses the fields parameter to limit response size."""
    mock_service = Mock()
    mock_service.presentations().get().execute.return_value = {"layouts": []}
    await _get_presentation_layouts(mock_service, "pres_123")

    # Check that get() was called with fields param
    get_call = mock_service.presentations().get
    call_kwargs = get_call.call_args
    assert call_kwargs is not None
    # The fields param should be in kwargs
    assert "fields" in (call_kwargs.kwargs or {}) or any(
        "layouts" in str(arg) for arg in (call_kwargs.args or ())
    )


# ---------------------------------------------------------------------------
# _resolve_create_slide_layouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_no_create_slide_skips_api_call():
    """Requests without createSlide skip the layout fetch entirely."""
    mock_service = Mock()
    requests = [{"deleteObject": {"objectId": "obj1"}}]
    result = await _resolve_create_slide_layouts(mock_service, "pres_123", requests)

    # Should return the same list, no API call made
    assert result is requests
    mock_service.presentations().get.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_create_slide_without_predefined_skips_api_call():
    """createSlide with layoutObjectId (not predefinedLayout) skips the fetch."""
    mock_service = Mock()
    requests = [{
        "createSlide": {
            "slideLayoutReference": {"layoutObjectId": "existing_layout_id"}
        }
    }]
    result = await _resolve_create_slide_layouts(mock_service, "pres_123", requests)
    assert result is requests


@pytest.mark.asyncio
async def test_resolve_blank_layout_found():
    """BLANK predefinedLayout is resolved to the actual layout ID."""
    service = _make_mock_service([_make_layout("layout_blank", "Blank")])
    requests = [_create_slide_req("BLANK")]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    assert len(result) == 1
    ref = result[0]["createSlide"]["slideLayoutReference"]
    assert "predefinedLayout" not in ref
    assert ref["layoutObjectId"] == "layout_blank"


@pytest.mark.asyncio
async def test_resolve_does_not_mutate_original():
    """Resolution creates a copy; the original request is not modified."""
    service = _make_mock_service([_make_layout("layout_blank", "Blank")])
    original_req = _create_slide_req("BLANK")
    requests = [original_req]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    # Original should still have predefinedLayout
    assert original_req["createSlide"]["slideLayoutReference"]["predefinedLayout"] == "BLANK"
    # Result should have layoutObjectId
    assert result[0]["createSlide"]["slideLayoutReference"]["layoutObjectId"] == "layout_blank"


@pytest.mark.asyncio
async def test_resolve_layout_not_found_passes_through():
    """When the layout isn't found, the original request passes through unchanged."""
    service = _make_mock_service([_make_layout("c1", "Corporate Hero")])
    original_req = _create_slide_req("BLANK")
    requests = [original_req]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    assert len(result) == 1
    # Should be the original request object (not a copy)
    assert result[0] is original_req
    assert result[0]["createSlide"]["slideLayoutReference"]["predefinedLayout"] == "BLANK"


@pytest.mark.asyncio
async def test_resolve_mixed_requests():
    """A mix of createSlide (with and without predefinedLayout) and other requests."""
    service = _make_mock_service([_make_layout("l_blank", "Blank")])
    requests = [
        {"deleteObject": {"objectId": "obj1"}},
        _create_slide_req("BLANK"),
        {
            "createSlide": {
                "slideLayoutReference": {"layoutObjectId": "already_set"}
            }
        },
        {"insertText": {"objectId": "obj2", "text": "hello"}},
    ]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    assert len(result) == 4
    # First request unchanged
    assert result[0] == {"deleteObject": {"objectId": "obj1"}}
    # Second request resolved
    assert result[1]["createSlide"]["slideLayoutReference"]["layoutObjectId"] == "l_blank"
    assert "predefinedLayout" not in result[1]["createSlide"]["slideLayoutReference"]
    # Third request unchanged (already had layoutObjectId)
    assert result[2]["createSlide"]["slideLayoutReference"]["layoutObjectId"] == "already_set"
    # Fourth request unchanged
    assert result[3] == {"insertText": {"objectId": "obj2", "text": "hello"}}


@pytest.mark.asyncio
async def test_resolve_create_slide_no_layout_ref():
    """createSlide without slideLayoutReference passes through."""
    service = _make_mock_service([_make_layout("l1", "Blank")])
    requests = [
        _create_slide_req("BLANK"),  # this one triggers the API call
        {"createSlide": {}},  # no slideLayoutReference at all
    ]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    assert len(result) == 2
    # First resolved
    assert result[0]["createSlide"]["slideLayoutReference"]["layoutObjectId"] == "l1"
    # Second passed through as-is
    assert result[1] == {"createSlide": {}}


@pytest.mark.asyncio
async def test_resolve_preserves_other_create_slide_fields():
    """Resolution preserves other fields in the createSlide request."""
    service = _make_mock_service([_make_layout("l1", "Blank")])
    requests = [{
        "createSlide": {
            "objectId": "my_custom_id",
            "insertionIndex": 2,
            "slideLayoutReference": {
                "predefinedLayout": "BLANK",
            },
            "placeholderIdMappings": [],
        }
    }]

    result = await _resolve_create_slide_layouts(service, "pres_123", requests)

    create_slide = result[0]["createSlide"]
    assert create_slide["objectId"] == "my_custom_id"
    assert create_slide["insertionIndex"] == 2
    assert create_slide["placeholderIdMappings"] == []
    assert create_slide["slideLayoutReference"]["layoutObjectId"] == "l1"
