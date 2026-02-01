import base64
import json
import asyncio
from django.test import TestCase, Client, TransactionTestCase
from django.urls import reverse
from channels.testing import WebsocketCommunicator
from storygame.asgi import application
from .models import Title, StoryPage, Lobby, Participant
from unittest import mock
from django.db import OperationalError


def tiny_sine_wave_base64():
    # A very small WAV header + silence; using an actual small base64-encoded WAV is easiest
    # This is a 1-second silent WAV base64 (PCM, 1 channel, 8-bit) — tiny but valid for tests
    b64 = (
        "data:audio/wav;base64,"
        "UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA="
    )
    return b64


class NarrationViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.title = Title.objects.create(title="Test Title")
        self.page = StoryPage.objects.create(
            title=self.title, page_number=1, content="Page content"
        )

    def test_get_narrate_page(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Record")

    def test_post_valid_audio_saves_narration(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        b64 = tiny_sine_wave_base64()
        resp = self.client.post(url, data={"audio_data": b64})
        # Expect redirect to success
        self.assertIn(resp.status_code, (302, 200))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertTrue(self.page.narration.startswith("data:audio/"))
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_invalid_mime_rejected(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        bad_b64 = "data:audio/unknown;base64,AAA"
        resp = self.client.post(url, data={"audio_data": bad_b64})
        self.assertEqual(resp.status_code, 400)
        self.page.refresh_from_db()
        self.assertIsNone(self.page.narration)

    def test_post_oversize_rejected(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        # create a large base64 payload (>5MB when decoded)
        raw = b"A" * (5 * 1024 * 1024 + 10)
        b64 = "data:audio/wav;base64," + base64.b64encode(raw).decode("ascii")
        resp = self.client.post(url, data={"audio_data": b64})
        self.assertEqual(resp.status_code, 400)
        self.page.refresh_from_db()
        self.assertIsNone(self.page.narration)

    def test_post_json_body_accepted(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        b64 = tiny_sine_wave_base64()
        resp = self.client.post(
            url, data=json.dumps({"audio_data": b64}), content_type="application/json"
        )
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_raw_dataurl_body_accepted(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        b64 = tiny_sine_wave_base64()
        # send raw body that is just the data URL string
        resp = self.client.post(url, data=b64, content_type="text/plain")
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_urlencoded_body_accepted(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        b64 = tiny_sine_wave_base64()
        # urlencoded body like audio_data=data:audio/...
        body = "audio_data=" + b64
        resp = self.client.post(
            url, data=body, content_type="application/x-www-form-urlencoded"
        )
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_file_upload_accepted(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        url = reverse("storygame:narrate_page", args=[self.page.id])
        # create a tiny WAV file content
        raw = base64.b64decode(
            "UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA="
        )
        uploaded = SimpleUploadedFile("voice.wav", raw, content_type="audio/wav")
        resp = self.client.post(url, data={"file": uploaded}, format="multipart")
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_raw_binary_audio_body_accepted(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        raw = base64.b64decode(
            "UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA="
        )
        resp = self.client.post(url, data=raw, content_type="audio/wav")
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")

    def test_post_dataurl_with_mime_parameters_accepted(self):
        url = reverse("storygame:narrate_page", args=[self.page.id])
        # include a mime parameter
        b64_body = "data:audio/wav;param=1;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA="
        resp = self.client.post(url, data={"audio_data": b64_body})
        self.assertIn(resp.status_code, (200, 302))
        self.page.refresh_from_db()
        self.assertIsNotNone(self.page.narration)
        self.assertEqual(self.page.narration_mime, "audio/wav")


class LobbyTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_create_and_join_lobby_and_participants(self):
        # Host creates a lobby; follow redirect so the lobby view runs and creates the host participant
        resp = self.client.post(
            reverse("storygame:create_lobby"), data={"host_name": "Host"}, follow=True
        )
        # follow=True should return 200 after redirect to lobby view
        self.assertEqual(resp.status_code, 200)
        lobby = Lobby.objects.first()
        self.assertIsNotNone(lobby)
        self.assertEqual(lobby.host_name, "Host")

        # Another user joins via the create_lobby (join flow)
        resp = self.client.post(
            reverse("storygame:create_lobby"),
            data={"code": lobby.code, "name": "Guest"},
        )
        self.assertIn(resp.status_code, (302, 301))
        participants = list(
            lobby.participants.order_by("id").values("id", "name", "role")
        )
        self.assertEqual(len(participants), 2)
        names = {p["name"] for p in participants}
        self.assertTrue({"Host", "Guest"}.issubset(names))

        # participants API returns the two participants and started is False
        url = reverse("storygame:lobby_participants_api", args=[lobby.code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("participants", data)
        self.assertFalse(data.get("started", True))
        self.assertEqual(len(data["participants"]), 2)

    def test_assign_role_host_only(self):
        # create lobby and participants directly
        lobby = Lobby.objects.create(code="TEST01", host_name="Host")
        host = Participant.objects.create(lobby=lobby, name="Host")
        guest = Participant.objects.create(lobby=lobby, name="Guest")

        url = reverse("storygame:lobby_assign_api", args=[lobby.code])
        # Guest attempts to assign -> forbidden
        resp = self.client.post(
            url,
            data={
                "participant_id": guest.id,
                "role": "writer",
                "requester_name": "Guest",
            },
        )
        self.assertEqual(resp.status_code, 403)
        guest.refresh_from_db()
        self.assertEqual(guest.role, "unassigned")

        # Host assigns -> success
        resp = self.client.post(
            url,
            data={
                "participant_id": guest.id,
                "role": "writer",
                "requester_name": "Host",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("participants", data)
        # Verify guest now has writer role
        guest.refresh_from_db()
        self.assertEqual(guest.role, "writer")

    def test_start_game_requires_writer_and_marks_started(self):
        lobby = Lobby.objects.create(code="START1", host_name="Host")
        host = Participant.objects.create(lobby=lobby, name="Host")
        guest = Participant.objects.create(lobby=lobby, name="Guest")

        url = reverse("storygame:lobby_start_api", args=[lobby.code])
        # Attempt to start without any writer assigned -> bad request
        resp = self.client.post(url, data={"requester_name": "Host"})
        self.assertEqual(resp.status_code, 400)

        # Assign a writer and start
        guest.role = "writer"
        guest.save()
        resp = self.client.post(url, data={"requester_name": "Host"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "started")
        self.assertIn("writer_id", data)
        self.assertEqual(data.get("writer_id"), guest.id)
        # Lobby started flag should be set
        lobby.refresh_from_db()
        self.assertTrue(lobby.started)

        # Non-writer can view waiting page
        wait_url = reverse("storygame:lobby_waiting", args=[lobby.code])
        resp = self.client.get(wait_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Waiting for writer")


class WebSocketLobbyTests(TransactionTestCase):
    def setUp(self):
        self.client = Client()

    def test_ws_join_and_participant_broadcast(self):
        loop = asyncio.get_event_loop()
        code = "WSROOM1"

        # Pre-create lobby to avoid concurrent table creation races on SQLite during tests
        Lobby.objects.create(code=code, host_name="Host")

        # Host connects with name in query string -> should create lobby and host participant
        host_path = f"/ws/lobby/{code}/?name=Host"
        host_ws = WebsocketCommunicator(application, host_path)
        connected, _ = loop.run_until_complete(host_ws.connect())
        self.assertTrue(connected)

        # Host should receive initial participants message (only host present)
        host_msg = loop.run_until_complete(host_ws.receive_json_from())
        self.assertEqual(host_msg.get("type"), "participants")
        self.assertEqual(len(host_msg.get("participants", [])), 1)
        self.assertEqual(host_msg["participants"][0]["name"], "Host")

        # Guest connects
        guest_path = f"/ws/lobby/{code}/?name=Guest"
        guest_ws = WebsocketCommunicator(application, guest_path)
        connected, _ = loop.run_until_complete(guest_ws.connect())
        self.assertTrue(connected)

        # Guest receives its own participants message
        guest_msg = loop.run_until_complete(guest_ws.receive_json_from())
        self.assertEqual(guest_msg.get("type"), "participants")
        # Host should receive a broadcast participants message after guest joined.
        # Read messages until the participants list contains both names or we time out.
        found = False
        for _ in range(6):
            try:
                host_broadcast = loop.run_until_complete(host_ws.receive_json_from())
            except Exception:
                break
            if host_broadcast.get("type") == "participants":
                names = {p["name"] for p in host_broadcast.get("participants", [])}
                if {"Host", "Guest"}.issubset(names):
                    found = True
                    break
        self.assertTrue(
            found,
            f"Participants broadcast did not include both Host and Guest; last names={names if 'names' in locals() else None}",
        )

        # Clean up
        loop.run_until_complete(host_ws.disconnect())
        loop.run_until_complete(guest_ws.disconnect())

    def test_ws_role_assignment_and_game_start_broadcast(self):
        loop = asyncio.get_event_loop()
        code = "WSROOM2"

        # Pre-create lobby to avoid SQLite locking when concurrent connects attempt to create the lobby table/row
        Lobby.objects.create(code=code, host_name="Host")

        # Connect host and guest
        host_ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Host")
        guest_ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Guest")
        self.assertTrue(loop.run_until_complete(host_ws.connect())[0])
        self.assertTrue(loop.run_until_complete(guest_ws.connect())[0])

        # Drain initial messages: host gets participants, guest gets participants, host gets broadcast
        _ = loop.run_until_complete(host_ws.receive_json_from())
        guest_msg = loop.run_until_complete(guest_ws.receive_json_from())
        _ = loop.run_until_complete(host_ws.receive_json_from())

        # Find guest participant id from DB
        guest_obj = Participant.objects.get(lobby__code=code, name="Guest")
        guest_id = guest_obj.id

        # Guest tries to assign themself -> should get an error
        loop.run_until_complete(
            guest_ws.send_json_to(
                {"type": "assign", "participant_id": guest_id, "role": "writer"}
            )
        )
        # Read messages until we see an error message or exhaust a few receives
        found_error = False
        last_msg = None
        for _ in range(6):
            try:
                last_msg = loop.run_until_complete(guest_ws.receive_json_from())
            except Exception:
                break
            if last_msg.get("type") == "error":
                found_error = True
                break
        self.assertTrue(
            found_error,
            f"Expected an error message from unauthorized assign; last_msg={last_msg}",
        )

        # Host assigns guest to writer
        loop.run_until_complete(
            host_ws.send_json_to(
                {"type": "assign", "participant_id": guest_id, "role": "writer"}
            )
        )
        # Both should receive a participants broadcast
        p1 = loop.run_until_complete(host_ws.receive_json_from())
        p2 = loop.run_until_complete(guest_ws.receive_json_from())
        # Ensure the guest now has role writer
        guest_obj.refresh_from_db()
        self.assertEqual(guest_obj.role, "writer")

        # Host starts the game via HTTP API
        resp = self.client.post(
            reverse("storygame:lobby_start_api", args=[code]),
            data={"requester_name": "Host"},
        )
        self.assertEqual(resp.status_code, 200)

        # Both host and guest should eventually receive a 'start' message. Read messages until we see it.
        def read_until_start(ws):
            last = None
            for _ in range(10):
                try:
                    last = loop.run_until_complete(ws.receive_json_from())
                except Exception:
                    break
                if last.get("type") == "start":
                    return last
            return last

        start_host = read_until_start(host_ws)
        start_guest = read_until_start(guest_ws)
        self.assertIsNotNone(start_host)
        self.assertIsNotNone(start_guest)
        self.assertEqual(start_host.get("type"), "start")
        self.assertEqual(start_guest.get("type"), "start")
        # writer_id should match guest id
        self.assertEqual(start_host.get("writer_id"), guest_id)
        self.assertEqual(start_guest.get("writer_id"), guest_id)

        # Disconnect
        loop.run_until_complete(host_ws.disconnect())
        loop.run_until_complete(guest_ws.disconnect())

    def test_reconnect_with_token_preserves_role(self):
        loop = asyncio.get_event_loop()
        code = "WSRECONNECT"

        # Pre-create lobby
        Lobby.objects.create(code=code, host_name="Host")

        # Connect a participant that will receive a token
        ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Alice")
        self.assertTrue(loop.run_until_complete(ws.connect())[0])
        # Drain initial participants msg
        msg = loop.run_until_complete(ws.receive_json_from())
        # Fetch participant from DB and ensure it has a token
        p = Participant.objects.get(lobby__code=code, name="Alice")
        self.assertIsNotNone(p.connection_token)
        token = p.connection_token

        # Assign Alice to writer via HTTP (simulate host assigning)
        lobby = p.lobby
        # create host participant
        Participant.objects.get_or_create(lobby=lobby, name="Host")
        assign_url = reverse("storygame:lobby_assign_api", args=[code])
        resp = self.client.post(
            assign_url,
            data={"participant_id": p.id, "role": "writer", "requester_name": "Host"},
        )
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.role, "writer")

        # Disconnect ws (simulate a dropped connection)
        loop.run_until_complete(ws.disconnect())

        # Reconnect with token - should reuse participant and preserve role
        ws2 = WebsocketCommunicator(
            application, f"/ws/lobby/{code}/?name=Alice&token={token}"
        )
        self.assertTrue(loop.run_until_complete(ws2.connect())[0])
        # Read participants message
        joined = loop.run_until_complete(ws2.receive_json_from())
        names = {pp["name"]: pp for pp in joined.get("participants", [])}
        self.assertIn("Alice", names)
        # Verify Alice still has writer role in DB
        p.refresh_from_db()
        self.assertEqual(p.role, "writer")

        loop.run_until_complete(ws2.disconnect())

    def test_start_handles_missing_connection_token_column(self):
        """Regression test: ensure starting a lobby doesn't fail if the DB column for
        connection_token is absent (simulate by forcing the consumer helper to report
        the field is missing). Previously this caused an OperationalError when the
        code attempted to write to a non-existent column.
        """
        loop = asyncio.get_event_loop()
        code = "NOCOL"

        # Pre-create lobby
        Lobby.objects.create(code=code, host_name="Host")

        # Patch the consumers helper to simulate missing DB column
        import storygame.consumers as consumers_mod

        original_checker = getattr(consumers_mod, "_model_has_field", None)
        try:
            consumers_mod._model_has_field = lambda m, f: False

            # Connect host which would normally attempt to create a tokenized participant
            host_ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Host")
            self.assertTrue(loop.run_until_complete(host_ws.connect())[0])
            # Drain initial participants msg
            _ = loop.run_until_complete(host_ws.receive_json_from())

            # Create a guest participant directly and assign writer role
            lobby = Lobby.objects.get(code=code)
            guest = Participant.objects.create(lobby=lobby, name="Guest", role="writer")

            # Start the game via HTTP as host; this should not raise OperationalError
            resp = self.client.post(
                reverse("storygame:lobby_start_api", args=[code]),
                data={"requester_name": "Host"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data.get("status"), "started")

            # Ensure host and any connected clients can still be cleaned up
            loop.run_until_complete(host_ws.disconnect())
        finally:
            # restore original helper
            if original_checker is not None:
                consumers_mod._model_has_field = original_checker
            else:
                delattr(consumers_mod, "_model_has_field")

    def test_title_creation_broadcasts_to_roles(self):
        loop = asyncio.get_event_loop()
        code = "TITLE1"

        # Pre-create lobby
        Lobby.objects.create(code=code, host_name="Host")

        # Connect three clients: writer, artist, narrator
        writer_ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Writer")
        artist_ws = WebsocketCommunicator(application, f"/ws/lobby/{code}/?name=Artist")
        narrator_ws = WebsocketCommunicator(
            application, f"/ws/lobby/{code}/?name=Narrator"
        )
        self.assertTrue(loop.run_until_complete(writer_ws.connect())[0])
        self.assertTrue(loop.run_until_complete(artist_ws.connect())[0])
        self.assertTrue(loop.run_until_complete(narrator_ws.connect())[0])

        # Drain initial participants messages
        _ = loop.run_until_complete(writer_ws.receive_json_from())
        _ = loop.run_until_complete(artist_ws.receive_json_from())
        _ = loop.run_until_complete(narrator_ws.receive_json_from())

        # Assign roles via HTTP as host
        lobby = Lobby.objects.get(code=code)
        Participant.objects.get_or_create(lobby=lobby, name="Host")
        w = Participant.objects.create(lobby=lobby, name="Writer")
        a = Participant.objects.create(lobby=lobby, name="Artist")
        n = Participant.objects.create(lobby=lobby, name="Narrator")

        # Assign writer/artist/narrator roles
        assign_url = reverse("storygame:lobby_assign_api", args=[code])
        self.client.post(
            assign_url,
            data={"participant_id": w.id, "role": "writer", "requester_name": "Host"},
        )
        self.client.post(
            assign_url,
            data={"participant_id": a.id, "role": "artist", "requester_name": "Host"},
        )
        self.client.post(
            assign_url,
            data={"participant_id": n.id, "role": "narrator", "requester_name": "Host"},
        )

        # Host starts the game
        resp = self.client.post(
            reverse("storygame:lobby_start_api", args=[code]),
            data={"requester_name": "Host"},
        )
        self.assertEqual(resp.status_code, 200)

        # Drain the start messages on sockets
        def drain_until_type(ws, typ, timeout=10):
            last = None
            for _ in range(timeout):
                try:
                    last = loop.run_until_complete(ws.receive_json_from())
                except Exception:
                    break
                if last.get("type") == typ:
                    return last
            return last

        _ = drain_until_type(writer_ws, "start")
        _ = drain_until_type(artist_ws, "start")
        _ = drain_until_type(narrator_ws, "start")

        # Now have the writer create a title and include lobby code
        resp = self.client.post(
            f"{reverse('storygame:create_title')}?lobby={code}",
            data={"title": "The Great Tale"},
        )
        self.assertIn(resp.status_code, (302, 200))

        # artist and narrator should receive a title_created message
        title_event_artist = drain_until_type(artist_ws, "title_created", timeout=10)
        title_event_narr = drain_until_type(narrator_ws, "title_created", timeout=10)
        self.assertIsNotNone(title_event_artist)
        self.assertIsNotNone(title_event_narr)
        self.assertEqual(title_event_artist.get("type"), "title_created")
        self.assertEqual(title_event_narr.get("type"), "title_created")
        self.assertIn("page_id", title_event_artist)
        self.assertIn("title_id", title_event_artist)

        # Cleanup
        loop.run_until_complete(writer_ws.disconnect())
        loop.run_until_complete(artist_ws.disconnect())
        loop.run_until_complete(narrator_ws.disconnect())
