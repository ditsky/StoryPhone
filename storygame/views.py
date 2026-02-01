from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseBadRequest
from .models import Title, StoryPage
import base64
import logging
from django.views.decorators.http import require_POST
import secrets
from .models import Lobby, Participant
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from urllib.parse import quote
from django.urls import reverse
from django.middleware.csrf import get_token
from django.http import HttpResponseForbidden
from django.db import connection, OperationalError as DBOperationalError

logger = logging.getLogger(__name__)


def generate_code(n=6):
    return secrets.token_urlsafe(n)[:n]


def create_title(request):
    """View to display a form for creating a new title and save it to the database.

    If a `lobby` query parameter is present (e.g. /create-title/?lobby=ABC123), after
    creating the Title we also create the first StoryPage (page_number=1) and broadcast
    a `title.created` message to the lobby group so other participants (artist/narrator)
    can navigate to their respective views.
    """
    if request.method == "POST":
        title_text = request.POST.get("title", "").strip()
        # get lobby code from query param if present
        lobby_code = request.GET.get("lobby") or request.POST.get("lobby")
        if title_text:
            title_obj = Title.objects.create(title=title_text)
            # create first story page (writer will likely continue editing content)
            page = StoryPage.objects.create(
                title=title_obj, page_number=1, content=title_text
            )

            # If lobby code provided, broadcast title created event to lobby group
            if lobby_code:
                try:
                    channel_layer = get_channel_layer()
                    participants = (
                        list(
                            Lobby.objects.filter(code=lobby_code)
                            .first()
                            .participants.values("id", "name", "role")
                        )
                        if Lobby.objects.filter(code=lobby_code).exists()
                        else []
                    )
                    async_to_sync(channel_layer.group_send)(
                        f"lobby_{lobby_code}",
                        {
                            "type": "title.created",
                            "title_id": title_obj.id,
                            "page_id": page.id,
                            "title": title_text,
                            "participants": participants,
                        },
                    )
                except Exception:
                    # Broadcasting is best-effort
                    pass

            return redirect("storygame:success")
    return render(request, "storygame/create_title.html")


def success(request):
    """View to display a success message after title is saved."""
    return render(request, "storygame/success.html")


def add_story_page(request, title_id=None):
    """View to add a new page to a story."""
    if title_id is None:
        # Show list of titles to choose from
        if request.method == "POST":
            title_id = request.POST.get("title_id")
            if title_id:
                return redirect("storygame:add_story_page_with_id", title_id=title_id)

        titles = Title.objects.all()
        return render(request, "storygame/select_title.html", {"titles": titles})

    # Get the title object
    title = get_object_or_404(Title, pk=title_id)

    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        if content:
            # Get the next page number
            last_page = (
                StoryPage.objects.filter(title=title).order_by("-page_number").first()
            )
            page_number = (last_page.page_number + 1) if last_page else 1

            StoryPage.objects.create(
                title=title, page_number=page_number, content=content
            )
            return redirect("storygame:page_success", title_id=title_id)

    return render(request, "storygame/add_story_page.html", {"title": title})


def page_success(request, title_id):
    """View to display success message after story page is saved."""
    title = get_object_or_404(Title, pk=title_id)
    return render(request, "storygame/page_success.html", {"title": title})


def story_list(request):
    """View to display all stories."""
    stories = Title.objects.all()
    return render(request, "storygame/story_list.html", {"stories": stories})


def view_story(request, title_id):
    """View to display a complete story with all its pages."""
    title = get_object_or_404(Title, pk=title_id)
    pages = StoryPage.objects.filter(title=title).order_by("page_number")

    context = {
        "title": title,
        "pages": pages,
        "page_count": pages.count(),
    }
    return render(request, "storygame/view_story.html", context)


def draw_on_page(request, page_id):
    """View to display a page and allow drawing on it."""
    page = get_object_or_404(StoryPage, pk=page_id)
    title = page.title

    if request.method == "POST":
        drawing_data = request.POST.get("drawing_data", "")
        if drawing_data:
            page.drawing = drawing_data
            page.save()
            return redirect("storygame:draw_success", page_id=page_id)

    context = {
        "page": page,
        "title": title,
    }
    return render(request, "storygame/draw_on_page.html", context)


def draw_success(request, page_id):
    """View to display success message after drawing is saved."""
    page = get_object_or_404(StoryPage, pk=page_id)
    context = {
        "page": page,
        "title": page.title,
    }
    return render(request, "storygame/draw_success.html", context)


# New views for narration recording
def narrate_page(request, page_id):
    """Display a page and accept a base64 audio data URL POST to save narration."""
    page = get_object_or_404(StoryPage, pk=page_id)

    def bad(msg):
        # log details to help debugging
        try:
            body_preview = request.body.decode("utf-8", errors="ignore")[:200]
        except Exception:
            body_preview = "<unable to decode body>"
        logger.warning(
            "Narrate POST rejected: %s; content_type=%s; headers=%s; body_preview=%s",
            msg,
            request.content_type,
            dict(request.headers),
            body_preview,
        )
        return HttpResponseBadRequest(msg)

    if request.method == "POST":
        # Try several ways to extract audio_data from the request
        audio_data = ""

        # 0) multipart file upload (FormData with a Blob/file)
        if request.FILES:
            # take the first uploaded file
            try:
                key, uploaded = next(iter(request.FILES.items()))
                content = uploaded.read()
                mime = uploaded.content_type or "audio/webm"
                b64 = base64.b64encode(content).decode("ascii")
                audio_data = f"data:{mime};base64,{b64}"
            except Exception as e:
                return bad("Unable to read uploaded file")

        # 1) standard form POST (audio_data field)
        if not audio_data:
            try:
                audio_data = request.POST.get("audio_data", "") or ""
            except Exception:
                audio_data = ""

        content_type = (request.content_type or "").lower()

        # 2) application/json (allow parameters like charset)
        if not audio_data and content_type.startswith("application/json"):
            import json

            try:
                payload = json.loads(request.body.decode("utf-8") or "{}")
                audio_data = payload.get("audio_data", "")
            except Exception:
                audio_data = ""

        # 3) raw body that contains a data URL directly (text/plain or other)
        if not audio_data and request.body:
            try:
                raw = request.body.decode("utf-8", errors="ignore").strip()
                if raw.startswith("data:audio/"):
                    audio_data = raw
                else:
                    # try urlencoded body like audio_data=data:audio/.. or JSON-like
                    from urllib.parse import parse_qs

                    parsed = parse_qs(raw)
                    if "audio_data" in parsed:
                        audio_data = parsed["audio_data"][0]
                    else:
                        # last resort: try to find substring
                        idx = raw.find("data:audio/")
                        if idx != -1:
                            audio_data = raw[idx:]
            except Exception:
                audio_data = ""

        # 4) raw binary body where content-type is like audio/webm, audio/ogg, etc.
        if not audio_data and content_type.startswith("audio/") and request.body:
            try:
                b64 = base64.b64encode(request.body).decode("ascii")
                audio_data = f"data:{content_type};base64,{b64}"
            except Exception:
                audio_data = ""

        if not audio_data:
            return bad("No audio data provided")

        # Basic validation: must start with data:audio/ and contain base64
        if not audio_data.startswith("data:audio/") or ";base64," not in audio_data:
            return bad("Invalid audio data format")

        try:
            header, b64 = audio_data.split(";base64,", 1)
            mime = header[len("data:") :]
            decoded = base64.b64decode(b64)
        except Exception:
            return bad("Unable to decode audio data")

        # Size limit: 5 MB
        if len(decoded) > 5 * 1024 * 1024:
            return bad("Audio file too large")

        # Whitelist common audio mime types
        # Normalize mime by removing any parameters, e.g. 'audio/webm;codecs=opus' -> 'audio/webm'
        mime_base = mime.split(";", 1)[0].strip()
        allowed = {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg"}
        if mime_base not in allowed:
            return bad("Unsupported audio mime type")

        # Save to model as data URL and store mime
        page.narration = audio_data
        page.narration_mime = mime_base
        page.save()

        # If AJAX expect JSON, otherwise redirect to success page
        is_json_request = content_type.startswith("application/json")
        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or is_json_request
        ):
            return JsonResponse({"status": "ok", "page_id": page.id})

        return redirect("storygame:narrate_success", page_id=page_id)

    # GET: render recording template
    context = {
        "page": page,
        "title": page.title,
    }
    return render(request, "storygame/record_narration.html", context)


def narrate_success(request, page_id):
    page = get_object_or_404(StoryPage, pk=page_id)
    return render(
        request, "storygame/draw_success.html", {"page": page, "title": page.title}
    )


def _ensure_connection_token_column():
    """Ensure the DB table for Participant has a connection_token column.

    If the column is missing (e.g. migrations haven't been applied), attempt to add
    a nullable varchar column. This is a runtime, best-effort fix to avoid hard
    crashes; the authoritative fix is to run migrations.
    """
    table = Participant._meta.db_table
    col = "connection_token"
    try:
        with connection.cursor() as cursor:
            cols = [
                c[0]
                for c in connection.introspection.get_table_description(cursor, table)
            ]
            if col not in cols:
                # Add a nullable column. SQLite supports ADD COLUMN for this.
                try:
                    cursor.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{col}" varchar(64);'
                    )
                except Exception:
                    # If we fail to add the column, just ignore and let the original error be raised
                    pass
    except Exception:
        # If introspection fails, do nothing; callers will handle the original DB error
        pass


def create_lobby(request):
    """
    Combined create-or-join lobby view. Renders a page with two forms: one to create a new lobby
    (host_name) and another to join an existing lobby (code + name). POST handling detects which
    form was submitted by checking for the presence of the `code` field (join) vs `host_name` (create).
    """
    if request.method == "POST":
        # If the POST contains a 'code' field, treat it as a join request
        if request.POST.get("code") is not None:
            code = request.POST.get("code", "").strip()
            name = request.POST.get("name", "Anon")[:100]
            if not code:
                return render(
                    request,
                    "storygame/create_lobby.html",
                    {
                        "join_error": "No code provided",
                        "host_name": request.POST.get("host_name", ""),
                    },
                )
            try:
                lobby = Lobby.objects.get(code=code)
                # create participant and redirect to lobby page
                p = Participant.objects.create(lobby=lobby, name=name)

                # Broadcast updated participants list to websocket group so connected clients update in real-time
                try:
                    channel_layer = get_channel_layer()
                    participants = list(lobby.participants.values("id", "name", "role"))
                    async_to_sync(channel_layer.group_send)(
                        f"lobby_{lobby.code}",
                        {
                            "type": "participants.message",
                            "participants": participants,
                        },
                    )
                except Exception:
                    # Don't fail join if broadcasting isn't available
                    pass

                # Redirect and include name so the lobby page can capture and persist it (localStorage)
                url = reverse("storygame:lobby_view", args=[code])
                return redirect(f"{url}?name={quote(name)}")
            except Lobby.DoesNotExist:
                return render(
                    request,
                    "storygame/create_lobby.html",
                    {
                        "join_error": "Lobby not found",
                        "host_name": request.POST.get("host_name", ""),
                    },
                )
        else:
            # Treat as create request
            host_name = request.POST.get("host_name", "Host")[:50]
            code = generate_code(6)
            # ensure uniqueness
            while Lobby.objects.filter(code=code).exists():
                code = generate_code(6)
            lobby = Lobby.objects.create(code=code, host_name=host_name)
            # Redirect to lobby view and include host name so the lobby page can pick it up
            url = reverse("storygame:lobby_view", args=[code])
            return redirect(f"{url}?name={quote(host_name)}")

    return render(request, "storygame/create_lobby.html")


def join_lobby(request):
    if request.method == "POST":
        code = request.POST.get("code", "").strip()
        name = request.POST.get("name", "Anon")[:100]
        if not code:
            return render(
                request, "storygame/join_lobby.html", {"error": "No code provided"}
            )
        try:
            lobby = Lobby.objects.get(code=code)
            # create participant and redirect to lobby page
            p = Participant.objects.create(lobby=lobby, name=name)

            # Broadcast updated participants list to websocket group so connected clients update in real-time
            try:
                channel_layer = get_channel_layer()
                participants = list(lobby.participants.values("id", "name", "role"))
                async_to_sync(channel_layer.group_send)(
                    f"lobby_{lobby.code}",
                    {
                        "type": "participants.message",
                        "participants": participants,
                    },
                )
            except Exception:
                # Don't fail join if broadcasting isn't available
                pass

            # Redirect and include name so the lobby page can capture and persist it (localStorage)
            url = reverse("storygame:lobby_view", args=[code])
            return redirect(f"{url}?name={quote(name)}")
        except Lobby.DoesNotExist:
            return render(
                request, "storygame/join_lobby.html", {"error": "Lobby not found"}
            )
    return render(request, "storygame/join_lobby.html")


def lobby_view(request, code):
    lobby = get_object_or_404(Lobby, code=code)

    # If a name was provided in the query params (e.g. from a redirect after join),
    # ensure a Participant exists for that name and broadcast the update.
    name = request.GET.get("name") or request.GET.get("username")
    if name:
        # Create participant if not already present
        if not lobby.participants.filter(name=name).exists():
            try:
                p = Participant.objects.create(lobby=lobby, name=name[:100])
            except DBOperationalError as e:
                if "no column named connection_token" in str(e):
                    _ensure_connection_token_column()
                    p = Participant.objects.create(lobby=lobby, name=name[:100])
                else:
                    raise
            try:
                channel_layer = get_channel_layer()
                participants = list(lobby.participants.values("id", "name", "role"))
                async_to_sync(channel_layer.group_send)(
                    f"lobby_{lobby.code}",
                    {
                        "type": "participants.message",
                        "participants": participants,
                    },
                )
            except Exception:
                pass

    participants = lobby.participants.all()
    # Pass initial_name so the template can avoid re-prompting if the server redirected with a name
    initial_name = name if name else ""
    csrf_val = get_token(request)
    is_host = True if (initial_name and initial_name == lobby.host_name) else False
    return render(
        request,
        "storygame/lobby.html",
        {
            "lobby": lobby,
            "participants": participants,
            "initial_name": initial_name,
            "csrf_token_value": csrf_val,
            "is_host": is_host,
        },
    )


# Simple HTTP API endpoints (useful for polling clients or non-websocket clients)
def lobby_participants_api(request, code):
    lobby = get_object_or_404(Lobby, code=code)
    participants = list(lobby.participants.values("id", "name", "role"))
    # Include lobby.started so polling clients can detect when the host started the game
    return JsonResponse({"participants": participants, "started": bool(lobby.started)})


@require_POST
def lobby_assign_api(request, code):
    lobby = get_object_or_404(Lobby, code=code)
    participant_id = request.POST.get("participant_id")
    role = request.POST.get("role")
    requester_name = request.POST.get("requester_name") or request.POST.get("name")
    logger.info(
        f"Assign API called for lobby={code} participant_id={participant_id} role={role} requester_name={requester_name}"
    )

    # Enforce that only the lobby host may assign roles
    if requester_name is None or requester_name != lobby.host_name:
        logger.warning(
            f"Unauthorized assign attempt in lobby={code} by requester={requester_name}"
        )
        return HttpResponseForbidden("Only the host may assign roles")

    if role not in dict(Participant.ROLE_CHOICES):
        return HttpResponseBadRequest("Invalid role")
    try:
        p = Participant.objects.get(pk=participant_id, lobby=lobby)
    except Participant.DoesNotExist:
        return HttpResponseBadRequest("Participant not found")
    p.role = role
    p.save()

    # Broadcast updated participants to websocket group as well
    try:
        channel_layer = get_channel_layer()
        participants = list(lobby.participants.values("id", "name", "role"))
        async_to_sync(channel_layer.group_send)(
            f"lobby_{lobby.code}",
            {
                "type": "participants.message",
                "participants": participants,
            },
        )
    except Exception:
        pass

    # In a Channels setup, the consumer will broadcast updates; for HTTP clients, return the new list
    participants = list(lobby.participants.values("id", "name", "role"))
    return JsonResponse({"participants": participants})


@require_POST
def lobby_start_api(request, code):
    """Host-only endpoint to start the game. Broadcasts a 'start' message to the lobby group.

    The host is enforced server-side by checking `request.POST.get('requester_name')` against
    the Lobby.host_name. On success, broadcasts a `game_start` group message and returns the
    participants list for HTTP clients.
    """
    lobby = get_object_or_404(Lobby, code=code)
    requester_name = request.POST.get("requester_name") or request.POST.get("name")
    if requester_name is None or requester_name != lobby.host_name:
        return HttpResponseForbidden("Only the host may start the game")

    # Ensure everyone has a role assigned (no 'unassigned' participants)
    participants = list(lobby.participants.values("id", "name", "role"))
    # Optionally, disallow starting unless no participants remain unassigned
    # For now, require that at least one writer exists
    writer_exists = any(p.get("role") == "writer" for p in participants)
    if not writer_exists:
        return HttpResponseBadRequest(
            "At least one writer must be assigned before starting"
        )

    # Broadcast start message to the group; include participants for client routing
    try:
        channel_layer = get_channel_layer()
        # identify the writer participant id (if any)
        writer_qs = lobby.participants.filter(role="writer").values_list(
            "id", flat=True
        )
        writer_id = writer_qs.first() if writer_qs.exists() else None
        # mark lobby started so poll-based clients will redirect too
        lobby.started = True
        lobby.save()
        # First broadcast updated participants so each consumer can compute and send per-connection me_id
        async_to_sync(channel_layer.group_send)(
            f"lobby_{lobby.code}",
            {"type": "participants.message", "participants": participants},
        )
        # Then broadcast the start event (includes writer_id) so clients can react with an up-to-date participants view
        async_to_sync(channel_layer.group_send)(
            f"lobby_{lobby.code}",
            {
                "type": "game.start",
                "participants": participants,
                "writer_id": writer_id,
            },
        )
    except Exception:
        # If broadcasting fails, continue but return participants to client
        pass

    return JsonResponse(
        {"status": "started", "participants": participants, "writer_id": writer_id}
    )


def waiting_view(request, code):
    """Simple page shown to non-writer players after the game starts."""
    lobby = get_object_or_404(Lobby, code=code)
    return render(request, "storygame/waiting.html", {"lobby": lobby})
