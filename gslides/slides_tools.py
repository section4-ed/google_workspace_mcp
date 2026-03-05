"""
Google Slides MCP Tools

This module provides MCP tools for interacting with Google Slides API.
"""

import asyncio
import copy
import logging
from typing import List, Dict, Any


from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors
from core.comments import create_comment_tools

logger = logging.getLogger(__name__)


# Mapping from Google Slides predefined layout enum names to common layout name
# patterns found in master slides.  Used as fallback heuristics when the exact
# predefined layout is not present in a custom theme.
_LAYOUT_NAME_HINTS: Dict[str, List[str]] = {
    "BLANK": ["blank"],
    "CAPTION_ONLY": ["caption"],
    "TITLE": ["title slide", "title"],
    "TITLE_AND_BODY": ["title and body", "title, body"],
    "TITLE_AND_TWO_COLUMNS": ["two column", "title and two columns", "two col"],
    "TITLE_ONLY": ["title only"],
    "SECTION_HEADER": ["section header", "section title", "section"],
    "ONE_COLUMN_TEXT": ["one column"],
    "MAIN_POINT": ["main point"],
    "BIG_NUMBER": ["big number"],
}


async def _get_presentation_layouts(
    service, presentation_id: str
) -> Dict[str, str]:
    """Return a mapping of predefined layout enum name (e.g. "BLANK") to layout objectId.

    Fetches the presentation's actual layouts and matches them to predefined
    names using display-name heuristics from ``_LAYOUT_NAME_HINTS``.
    """
    presentation = await asyncio.to_thread(
        service.presentations()
        .get(
            presentationId=presentation_id,
            fields="layouts(objectId,layoutProperties/displayName)",
        )
        .execute
    )

    layout_by_predefined: Dict[str, str] = {}
    layout_by_name: Dict[str, str] = {}

    for layout in presentation.get("layouts", []):
        layout_id = layout.get("objectId", "")
        props = layout.get("layoutProperties", {})
        display_name = props.get("displayName", "")
        name_lower = display_name.lower().strip()

        if name_lower:
            layout_by_name[name_lower] = layout_id

    for predefined_name, hints in _LAYOUT_NAME_HINTS.items():
        for hint in hints:
            if hint in layout_by_name:
                layout_by_predefined[predefined_name] = layout_by_name[hint]
                break

    return layout_by_predefined


async def _resolve_create_slide_layouts(
    service, presentation_id: str, requests: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Resolve predefinedLayout references in createSlide requests to actual layout IDs.

    Presentations with custom themes may not have standard predefined layouts.
    This function fetches the presentation's actual layouts and replaces
    predefinedLayout with a layoutObjectId where possible.
    """
    # Early return if no createSlide requests use predefinedLayout
    if not any(
        "predefinedLayout" in req.get("createSlide", {}).get("slideLayoutReference", {})
        for req in requests
    ):
        return requests

    layout_map = await _get_presentation_layouts(service, presentation_id)

    resolved = []
    for req in requests:
        if "createSlide" not in req:
            resolved.append(req)
            continue

        create_slide = req["createSlide"]
        layout_ref = create_slide.get("slideLayoutReference", {})
        predefined = layout_ref.get("predefinedLayout")

        if not predefined or "layoutObjectId" in layout_ref:
            resolved.append(req)
            continue

        if predefined in layout_map:
            new_req = copy.deepcopy(req)
            new_ref = new_req["createSlide"]["slideLayoutReference"]
            del new_ref["predefinedLayout"]
            new_ref["layoutObjectId"] = layout_map[predefined]
            resolved.append(new_req)
            logger.info(
                f"[batch_update_presentation] Resolved predefinedLayout '{predefined}' "
                f"to layoutObjectId '{layout_map[predefined]}'"
            )
        else:
            # Layout not found — pass through unchanged and let the Slides API
            # surface the real error rather than silently mutating the request.
            resolved.append(req)
            logger.warning(
                f"[batch_update_presentation] predefinedLayout '{predefined}' not found "
                f"in presentation masters. Available: {list(layout_map.keys())}. "
                f"Passing request as-is."
            )

    return resolved


@server.tool()
@handle_http_errors("create_presentation", service_type="slides")
@require_google_service("slides", "slides")
async def create_presentation(
    service, user_google_email: str, title: str = "Untitled Presentation"
) -> str:
    """
    Create a new Google Slides presentation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        title (str): The title for the new presentation. Defaults to "Untitled Presentation".

    Returns:
        str: Details about the created presentation including ID and URL.
    """
    logger.info(
        f"[create_presentation] Invoked. Email: '{user_google_email}', Title: '{title}'"
    )

    body = {"title": title}

    result = await asyncio.to_thread(service.presentations().create(body=body).execute)

    presentation_id = result.get("presentationId")
    presentation_url = f"https://docs.google.com/presentation/d/{presentation_id}/edit"

    confirmation_message = f"""Presentation Created Successfully for {user_google_email}:
- Title: {title}
- Presentation ID: {presentation_id}
- URL: {presentation_url}
- Slides: {len(result.get("slides", []))} slide(s) created"""

    logger.info(f"Presentation created successfully for {user_google_email}")
    return confirmation_message


@server.tool()
@handle_http_errors("get_presentation", is_read_only=True, service_type="slides")
@require_google_service("slides", "slides_read")
async def get_presentation(
    service, user_google_email: str, presentation_id: str
) -> str:
    """
    Get details about a Google Slides presentation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        presentation_id (str): The ID of the presentation to retrieve.

    Returns:
        str: Details about the presentation including title, slides count, and metadata.
    """
    logger.info(
        f"[get_presentation] Invoked. Email: '{user_google_email}', ID: '{presentation_id}'"
    )

    result = await asyncio.to_thread(
        service.presentations().get(presentationId=presentation_id).execute
    )

    title = result.get("title", "Untitled")
    slides = result.get("slides", [])
    page_size = result.get("pageSize", {})

    slides_info = []
    for i, slide in enumerate(slides, 1):
        slide_id = slide.get("objectId", "Unknown")
        page_elements = slide.get("pageElements", [])

        # Collect text from the slide whose JSON structure is very complicated
        # https://googleapis.github.io/google-api-python-client/docs/dyn/slides_v1.presentations.html#get
        slide_text = ""
        try:
            texts_from_elements = []
            for page_element in slide.get("pageElements", []):
                shape = page_element.get("shape", None)
                if shape and shape.get("text", None):
                    text = shape.get("text", None)
                    if text:
                        text_elements_in_shape = []
                        for text_element in text.get("textElements", []):
                            text_run = text_element.get("textRun", None)
                            if text_run:
                                content = text_run.get("content", None)
                                if content:
                                    start_index = text_element.get("startIndex", 0)
                                    text_elements_in_shape.append(
                                        (start_index, content)
                                    )

                        if text_elements_in_shape:
                            # Sort text elements within a single shape
                            text_elements_in_shape.sort(key=lambda item: item[0])
                            full_text_from_shape = "".join(
                                [item[1] for item in text_elements_in_shape]
                            )
                            texts_from_elements.append(full_text_from_shape)

            # cleanup text we collected
            slide_text = "\n".join(texts_from_elements)
            slide_text_rows = slide_text.split("\n")
            slide_text_rows = [row for row in slide_text_rows if len(row.strip()) > 0]
            if slide_text_rows:
                slide_text_rows = ["    > " + row for row in slide_text_rows]
                slide_text = "\n" + "\n".join(slide_text_rows)
            else:
                slide_text = ""
        except Exception as e:
            logger.warning(f"Failed to extract text from the slide {slide_id}: {e}")
            slide_text = f"<failed to extract text: {type(e)}, {e}>"

        slides_info.append(
            f"  Slide {i}: ID {slide_id}, {len(page_elements)} element(s), text: {slide_text if slide_text else 'empty'}"
        )

    confirmation_message = f"""Presentation Details for {user_google_email}:
- Title: {title}
- Presentation ID: {presentation_id}
- URL: https://docs.google.com/presentation/d/{presentation_id}/edit
- Total Slides: {len(slides)}
- Page Size: {page_size.get("width", {}).get("magnitude", "Unknown")} x {page_size.get("height", {}).get("magnitude", "Unknown")} {page_size.get("width", {}).get("unit", "")}

Slides Breakdown:
{chr(10).join(slides_info) if slides_info else "  No slides found"}"""

    logger.info(f"Presentation retrieved successfully for {user_google_email}")
    return confirmation_message


@server.tool()
@handle_http_errors("batch_update_presentation", service_type="slides")
@require_google_service("slides", "slides")
async def batch_update_presentation(
    service,
    user_google_email: str,
    presentation_id: str,
    requests: List[Dict[str, Any]],
) -> str:
    """
    Apply batch updates to a Google Slides presentation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        presentation_id (str): The ID of the presentation to update.
        requests (List[Dict[str, Any]]): List of update requests to apply.

    Returns:
        str: Details about the batch update operation results.
    """
    logger.info(
        f"[batch_update_presentation] Invoked. Email: '{user_google_email}', ID: '{presentation_id}', Requests: {len(requests)}"
    )

    # Resolve predefinedLayout references to actual layout IDs for
    # presentations that use custom themes/masters.
    requests = await _resolve_create_slide_layouts(service, presentation_id, requests)

    body = {"requests": requests}

    result = await asyncio.to_thread(
        service.presentations()
        .batchUpdate(presentationId=presentation_id, body=body)
        .execute
    )

    replies = result.get("replies", [])

    confirmation_message = f"""Batch Update Completed for {user_google_email}:
- Presentation ID: {presentation_id}
- URL: https://docs.google.com/presentation/d/{presentation_id}/edit
- Requests Applied: {len(requests)}
- Replies Received: {len(replies)}"""

    if replies:
        confirmation_message += "\n\nUpdate Results:"
        for i, reply in enumerate(replies, 1):
            if "createSlide" in reply:
                slide_id = reply["createSlide"].get("objectId", "Unknown")
                confirmation_message += (
                    f"\n  Request {i}: Created slide with ID {slide_id}"
                )
            elif "createShape" in reply:
                shape_id = reply["createShape"].get("objectId", "Unknown")
                confirmation_message += (
                    f"\n  Request {i}: Created shape with ID {shape_id}"
                )
            else:
                confirmation_message += f"\n  Request {i}: Operation completed"

    logger.info(f"Batch update completed successfully for {user_google_email}")
    return confirmation_message


@server.tool()
@handle_http_errors("get_page", is_read_only=True, service_type="slides")
@require_google_service("slides", "slides_read")
async def get_page(
    service, user_google_email: str, presentation_id: str, page_object_id: str
) -> str:
    """
    Get details about a specific page (slide) in a presentation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        presentation_id (str): The ID of the presentation.
        page_object_id (str): The object ID of the page/slide to retrieve.

    Returns:
        str: Details about the specific page including elements and layout.
    """
    logger.info(
        f"[get_page] Invoked. Email: '{user_google_email}', Presentation: '{presentation_id}', Page: '{page_object_id}'"
    )

    result = await asyncio.to_thread(
        service.presentations()
        .pages()
        .get(presentationId=presentation_id, pageObjectId=page_object_id)
        .execute
    )

    page_type = result.get("pageType", "Unknown")
    page_elements = result.get("pageElements", [])

    elements_info = []
    for element in page_elements:
        element_id = element.get("objectId", "Unknown")
        if "shape" in element:
            shape_type = element["shape"].get("shapeType", "Unknown")
            elements_info.append(f"  Shape: ID {element_id}, Type: {shape_type}")
        elif "table" in element:
            table = element["table"]
            rows = table.get("rows", 0)
            cols = table.get("columns", 0)
            elements_info.append(f"  Table: ID {element_id}, Size: {rows}x{cols}")
        elif "line" in element:
            line_type = element["line"].get("lineType", "Unknown")
            elements_info.append(f"  Line: ID {element_id}, Type: {line_type}")
        else:
            elements_info.append(f"  Element: ID {element_id}, Type: Unknown")

    confirmation_message = f"""Page Details for {user_google_email}:
- Presentation ID: {presentation_id}
- Page ID: {page_object_id}
- Page Type: {page_type}
- Total Elements: {len(page_elements)}

Page Elements:
{chr(10).join(elements_info) if elements_info else "  No elements found"}"""

    logger.info(f"Page retrieved successfully for {user_google_email}")
    return confirmation_message


@server.tool()
@handle_http_errors("get_page_thumbnail", is_read_only=True, service_type="slides")
@require_google_service("slides", "slides_read")
async def get_page_thumbnail(
    service,
    user_google_email: str,
    presentation_id: str,
    page_object_id: str,
    thumbnail_size: str = "MEDIUM",
) -> str:
    """
    Generate a thumbnail URL for a specific page (slide) in a presentation.

    Args:
        user_google_email (str): The user's Google email address. Required.
        presentation_id (str): The ID of the presentation.
        page_object_id (str): The object ID of the page/slide.
        thumbnail_size (str): Size of thumbnail ("LARGE", "MEDIUM", "SMALL"). Defaults to "MEDIUM".

    Returns:
        str: URL to the generated thumbnail image.
    """
    logger.info(
        f"[get_page_thumbnail] Invoked. Email: '{user_google_email}', Presentation: '{presentation_id}', Page: '{page_object_id}', Size: '{thumbnail_size}'"
    )

    result = await asyncio.to_thread(
        service.presentations()
        .pages()
        .getThumbnail(
            presentationId=presentation_id,
            pageObjectId=page_object_id,
            thumbnailProperties_thumbnailSize=thumbnail_size,
            thumbnailProperties_mimeType="PNG",
        )
        .execute
    )

    thumbnail_url = result.get("contentUrl", "")

    confirmation_message = f"""Thumbnail Generated for {user_google_email}:
- Presentation ID: {presentation_id}
- Page ID: {page_object_id}
- Thumbnail Size: {thumbnail_size}
- Thumbnail URL: {thumbnail_url}

You can view or download the thumbnail using the provided URL."""

    logger.info(f"Thumbnail generated successfully for {user_google_email}")
    return confirmation_message


# Create comment management tools for slides
_comment_tools = create_comment_tools("presentation", "presentation_id")
list_presentation_comments = _comment_tools["list_comments"]
manage_presentation_comment = _comment_tools["manage_comment"]

# Aliases for backwards compatibility and intuitive naming
list_slide_comments = list_presentation_comments
manage_slide_comment = manage_presentation_comment
