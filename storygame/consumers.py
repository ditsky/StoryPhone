import logging
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from .models import Lobby, Participant
from asgiref.sync import sync_to_async
from django.core.exceptions import FieldDoesNotExist

logger = logging.getLogger(__name__)


def _model_has_field(model, field_name):
    """Return True if the given Django model has a field with the provided name."""
    try:
        model._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


@sync_to_async
def get_lobby_host(code):
    try:
        return Lobby.objects.get(code=code).host_name
    except Lobby.DoesNotExist:
        return None


@sync_to_async
def assign_role_sync(participant_id, role, lobby_code):
    try:
        p = Participant.objects.get(pk=participant_id, lobby__code=lobby_code)
        if role in dict(Participant.ROLE_CHOICES):
            p.role = role
            p.save()
            return True
    except Participant.DoesNotExist:
        return False
    return False


@sync_to_async
def get_participant_id_by_name(name, lobby_code):
    try:
        p = Participant.objects.filter(lobby__code=lobby_code, name=name).first()
        return p.id if p else None
    except Exception:
        return None


class LobbyConsumer(AsyncJsonWebsocketConsumer):
    """WebSocket consumer that handles lobby participant list and role assignments.

    Protocol:
      - On connect: client should send ?name=... as a query param (we'll only rely on a later 'join' message for name)
      - Messages from client (JSON):
          {"type": "join", "name": "Alice"}
          {"type": "assign", "participant_id": 5, "role": "writer"}  # only allowed for host
      - Server broadcasts participant list updates:
          {"type": "participants", "participants": [{"id":1,"name":"A","role":"writer"}, ...]}
    """

    async def connect(self):
        # URL path: /ws/lobby/<code>/
        self.lobby_code = self.scope["url_route"]["kwargs"].get("code")
        self.group_name = f"lobby_{self.lobby_code}"
        self.connection_name = None

        # accept the connection and add to group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        logger.info(
            f"WebSocket connect: channel={self.channel_name} lobby={self.lobby_code}"
        )

        # If query string contains a name, create a participant for this connection
        qs = self.scope.get("query_string", b"").decode("utf-8")
        params = {}
        if qs:
            from urllib.parse import parse_qs

            parsed = parse_qs(qs)
            name_vals = parsed.get("name") or parsed.get("username")
            token_vals = parsed.get("token") or parsed.get("connection_token")
            if name_vals:
                name = name_vals[0]
                self.connection_name = name
                # If a connection token is provided, try to reconnect to an existing Participant
                token = token_vals[0] if token_vals else None
                if token and _model_has_field(Participant, "connection_token"):
                    # attempt to find participant by token and lobby
                    existing = await find_participant_by_token(token, self.lobby_code)
                    if existing:
                        # reuse participant id (do not create a new DB row)
                        self.participant_id = existing.id
                        # ensure name is up-to-date
                        await update_participant_name(existing.id, name)
                    else:
                        # token not found -> create new participant and assign token
                        self.participant_id = await self.add_participant(
                            name, token=True
                        )
                else:
                    # create a Participant for this connection and remember its id so we can remove it on disconnect
                    self.participant_id = await self.add_participant(name, token=True)
                await self.broadcast_participants()

        # Send current participant list to the new connection
        await self.send_participants()

    async def disconnect(self, close_code):
        # Remove the participant tied to this connection (if any) and broadcast updated list,
        # then leave the group.
        try:
            # If we created a participant for this connection, remove it from DB
            if getattr(self, "participant_id", None):
                try:
                    await remove_participant_sync(self.participant_id, self.lobby_code)
                except Exception:
                    # Ignore removal errors
                    pass
                # Broadcast updated participants list to the group so remaining clients update in real-time
                try:
                    participants = await sync_to_async(list)(
                        Participant.objects.filter(lobby__code=self.lobby_code).values(
                            "id", "name", "role"
                        )
                    )
                    await self.channel_layer.group_send(
                        self.group_name,
                        {"type": "participants.message", "participants": participants},
                    )
                except Exception:
                    pass

            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        except Exception:
            pass
        logger.info(
            f"WebSocket disconnect: channel={self.channel_name} lobby={self.lobby_code} code={close_code}"
        )

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        logger.info(f"Received WS message for lobby={self.lobby_code}: {content}")
        if msg_type == "join":
            name = content.get("name") or "Anonymous"
            token = content.get("token")
            self.connection_name = name
            if token and _model_has_field(Participant, "connection_token"):
                existing = await find_participant_by_token(token, self.lobby_code)
                if existing:
                    self.participant_id = existing.id
                    await update_participant_name(existing.id, name)
                else:
                    self.participant_id = await self.add_participant(name, token=True)
            else:
                self.participant_id = await self.add_participant(name, token=True)
            await self.broadcast_participants()
        elif msg_type == "assign":
            # Only allow the lobby host to assign roles
            participant_id = content.get("participant_id")
            role = content.get("role")
            logger.info(
                f"WS assign request lobby={self.lobby_code} participant_id={participant_id} role={role} requester={self.connection_name}"
            )
            host_name = await get_lobby_host(self.lobby_code)
            if (
                self.connection_name is None
                or host_name is None
                or self.connection_name != host_name
            ):
                # unauthorized
                logger.warning(
                    f"Unauthorized WS assign attempt in lobby={self.lobby_code} by requester={self.connection_name}"
                )
                await self.send_json(
                    {"type": "error", "message": "Only the host may assign roles"}
                )
                return
            # perform assignment
            ok = await assign_role_sync(participant_id, role, self.lobby_code)
            if ok:
                await self.broadcast_participants()
            else:
                await self.send_json(
                    {
                        "type": "error",
                        "message": "Participant not found or invalid role",
                    }
                )

    async def send_participants(self):
        participants = await sync_to_async(list)(
            Participant.objects.filter(lobby__code=self.lobby_code).values(
                "id", "name", "role"
            )
        )
        logger.debug(
            f"Sending participants to channel={self.channel_name} lobby={self.lobby_code}: {participants}"
        )
        # Determine participant id for this connection (if any)
        me_id = None
        if self.connection_name:
            try:
                me_id = await get_participant_id_by_name(
                    self.connection_name, self.lobby_code
                )
            except Exception:
                me_id = None
        await self.send_json(
            {"type": "participants", "participants": participants, "me_id": me_id}
        )

    async def broadcast_participants(self):
        participants = await sync_to_async(list)(
            Participant.objects.filter(lobby__code=self.lobby_code).values(
                "id", "name", "role"
            )
        )
        logger.info(
            f"Broadcasting participants to group={self.group_name}: {participants}"
        )
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "participants.message", "participants": participants},
        )

    async def participants_message(self, event):
        logger.debug(
            f'participants_message event in channel={self.channel_name} lobby={self.lobby_code} payload={event.get("participants")}'
            ""
        )
        # Compute me_id for this connection and include it in the message
        me_id = None
        if self.connection_name:
            try:
                me_id = await get_participant_id_by_name(
                    self.connection_name, self.lobby_code
                )
            except Exception:
                me_id = None
        await self.send_json(
            {
                "type": "participants",
                "participants": event["participants"],
                "me_id": me_id,
            }
        )

    async def game_start(self, event):
        """
        Handler for a game start broadcast. Forward a simple 'start' message to clients
        with the participants payload so clients can decide where to navigate.
        """
        logger.info(
            f"Broadcasting game start to channel={self.channel_name} lobby={self.lobby_code}"
        )
        me_id = None
        if self.connection_name:
            try:
                me_id = await get_participant_id_by_name(
                    self.connection_name, self.lobby_code
                )
            except Exception:
                me_id = None
        # Forward writer_id from the event so clients can quickly decide routing
        writer_id = event.get("writer_id") if isinstance(event, dict) else None
        await self.send_json(
            {
                "type": "start",
                "participants": event.get("participants", []),
                "me_id": me_id,
                "writer_id": writer_id,
            }
        )

    async def title_created(self, event):
        """Forward a title.created group event to this connection with details.

        The event payload will include title_id, page_id, title, and participants.
        Clients can use this to navigate artist/narrator to the correct page.
        """
        logger.info(
            f"Broadcasting title created to channel={self.channel_name} lobby={self.lobby_code} payload={event}"
        )
        me_id = None
        if self.connection_name:
            try:
                me_id = await get_participant_id_by_name(
                    self.connection_name, self.lobby_code
                )
            except Exception:
                me_id = None
        await self.send_json(
            {
                "type": "title_created",
                "title_id": event.get("title_id"),
                "page_id": event.get("page_id"),
                "title": event.get("title"),
                "participants": event.get("participants", []),
                "me_id": me_id,
            }
        )

    @sync_to_async
    def add_participant(self, name, token=False):
        from django.db import IntegrityError, transaction

        lobby, _ = Lobby.objects.get_or_create(
            code=self.lobby_code, defaults={"host_name": name}
        )
        # Use get_or_create to avoid duplicate participant rows when multiple connects happen
        try:
            with transaction.atomic():
                if token and _model_has_field(Participant, "connection_token"):
                    # create a token and assign to participant
                    import secrets

                    token_val = secrets.token_urlsafe(32)
                    p, created = Participant.objects.get_or_create(
                        lobby=lobby,
                        name=name,
                        defaults={"connection_token": token_val},
                    )
                    # ensure token is set on the existing participant if it didn't have one
                    if not getattr(p, "connection_token", None):
                        try:
                            p.connection_token = token_val
                            p.save()
                        except Exception:
                            # If the DB doesn't actually have the column, ignore
                            pass
                    return p.id
                else:
                    p, created = Participant.objects.get_or_create(
                        lobby=lobby, name=name
                    )
                    return p.id
        except IntegrityError:
            # If there was a race, fallback to fetching the existing participant
            p = Participant.objects.filter(lobby=lobby, name=name).first()
            return p.id if p else None

    @sync_to_async
    def assign_role(self, participant_id, role):
        try:
            p = Participant.objects.get(pk=participant_id)
            if role in dict(Participant.ROLE_CHOICES):
                p.role = role
                p.save()
        except Participant.DoesNotExist:
            pass


@sync_to_async
def remove_participant_sync(participant_id, lobby_code):
    try:
        # For reconnection via token, keep the Participant row and token intact.
        # We intentionally do not delete or clear the token here; cleanup of truly stale
        # participants should be handled by a separate expiry/cleanup process.
        return True
    except Participant.DoesNotExist:
        return False


@sync_to_async
def find_participant_by_token(token, lobby_code):
    try:
        if not _model_has_field(Participant, "connection_token"):
            return None
        return Participant.objects.filter(
            connection_token=token, lobby__code=lobby_code
        ).first()
    except Exception:
        return None


@sync_to_async
def update_participant_name(participant_id, name):
    try:
        p = Participant.objects.get(pk=participant_id)
        if p.name != name:
            p.name = name
            p.save()
        return True
    except Participant.DoesNotExist:
        return False
