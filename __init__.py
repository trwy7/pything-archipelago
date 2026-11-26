"""Archipelago multiworld event monitor."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from websockets.exceptions import InvalidMessage, InvalidStatus
from websockets.sync.client import connect as websocket_connect

from pything.app import App
from pything.settings import IntegerSetting, StringSetting
from pything.web import render_template

app = App(
    [
        StringSetting("host", "Host", "archipelago.gg"),
        IntegerSetting("port", "Port", 38281, min_value=1, max_value=65535),
        StringSetting("slot", "Slot", ""),
        StringSetting("password", "Password", ""),
    ]
)

_players: dict[int, dict[str, Any]] = {}
_items: dict[str, dict[int, str]] = {}
_locations: dict[str, dict[int, str]] = {}
_recent: list[dict[str, Any]] = []
_lock = threading.Lock()
_reconnect = threading.Event()


@app.blueprint.route("/launch")
def launch():
    with _lock:
        initial = list(_recent)
    return render_template(f"{app.dirname}/pages/home.html", initial=initial)


def log_line(text: str, kind: str = "info", parts: list[dict[str, str]] | None = None):
    """Store and broadcast a safely-rendered log entry."""
    line = {"text": text, "kind": kind, "parts": parts or []}
    with _lock:
        _recent.append(line)
        del _recent[:-500]
    app.send("line", line)


def clear_session_data():
    with _lock:
        _players.clear()
        _items.clear()
        _locations.clear()


def lookup_player(slot: Any) -> dict[str, Any]:
    try:
        return _players.get(int(slot), {"name": f"Player{slot}", "game": ""})
    except (TypeError, ValueError):
        return {"name": str(slot or "Unknown player"), "game": ""}


def name_for(kind: str, game: str, value: Any) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value)
    table = _items if kind == "item" else _locations
    return table.get(game, {}).get(numeric, str(value))


def item_class(flags: Any) -> str:
    try:
        flags = int(flags)
    except (TypeError, ValueError):
        flags = 0
    if flags & 4:
        return "trap"
    if flags & 1:
        return "progression"
    if flags & 2:
        return "useful"
    return "filler"


def structured_item_event(data: dict[str, Any], own_slot: int) -> dict[str, Any] | None:
    """Build a compact sent-item line from a PrintJSON ItemSend packet."""
    item = data.get("item", {})
    if not isinstance(item, dict):
        item = {"item": item}
    sending_slot = item.get("player", data.get("sending", data.get("player", 0)))
    receiving_slot = data.get("receiving", own_slot)
    sender = lookup_player(sending_slot)
    recipient = lookup_player(receiving_slot)
    location = item.get("location", data.get("location", {}))
    if not isinstance(location, dict):
        location = {"location": location}
    item_game = str(item.get("game", recipient.get("game", "")))
    location_game = str(location.get("game", sender.get("game", "")))
    item_name = name_for("item", item_game, item.get("item", item.get("id", "?")))
    location_value = location.get("location", location.get("id"))
    location_name = name_for("location", location_game, location_value) if location_value is not None else ""
    own_sender = str(sending_slot) == str(own_slot)
    own_recipient = str(receiving_slot) == str(own_slot)
    parts = [
        {"text": sender["name"], "class": "player own" if own_sender else "player"},
        {"text": " sent ", "class": "plain"},
        {"text": item_name, "class": item_class(item.get("flags", data.get("flags", 0)))},
        {"text": " to ", "class": "plain"},
        {"text": recipient["name"], "class": "player own" if own_recipient else "player"},
    ]
    if location_name:
        parts.extend([
            {"text": " (", "class": "plain"},
            {"text": location_name, "class": "location"},
            {"text": ")", "class": "plain"},
        ])
    text = "".join(part["text"] for part in parts)
    own = own_sender or own_recipient
    return {"text": text, "kind": "item own-line" if own else "item", "parts": parts}


def print_json_line(packet: dict[str, Any], own_slot: int) -> None:
    data = packet.get("data") or []
    if not isinstance(data, list):
        data = [{"text": str(data)}]
    # ItemSend stores receiver and NetworkItem alongside the normal text
    # segments; render it in the compact web-log format.
    if packet.get("type", "").lower() == "itemsend":
        line = structured_item_event(packet, own_slot)
        if line:
            with _lock:
                _recent.append(line)
                del _recent[:-500]
            app.send("line", line)
            return

    parts: list[dict[str, str]] = []
    own_involved = False
    for segment in data:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text", ""))
        segment_type = segment.get("type", "")
        player_id = segment.get("slot", segment.get("player"))
        css_class = "plain"
        if segment_type == "player_id":
            player = lookup_player(player_id)
            text = player["name"]
            if player_id is not None:
                try:
                    own_involved = own_involved or int(player_id) == own_slot
                except (TypeError, ValueError):
                    pass
            css_class = "player own" if player_id is not None and str(player_id) == str(own_slot) else "player"
        elif segment_type == "item_id":
            game = str(segment.get("game", ""))
            text = name_for("item", game, segment.get("text", segment.get("item", "")))
            css_class = item_class(segment.get("flags", 0))
            try:
                own_involved = own_involved or int(segment.get("player", -1)) == own_slot
            except (TypeError, ValueError):
                pass
        elif segment_type == "location_id":
            game = str(segment.get("game", ""))
            text = name_for("location", game, segment.get("text", segment.get("location", "")))
            css_class = "location"
        elif segment_type == "color":
            css_class = "plain"
        parts.append({"text": text, "class": css_class})
    text = "".join(part["text"] for part in parts)
    if text.strip():
        log_line(text, "own-line" if own_involved else "info", parts)


def collect_room_info(packet: dict[str, Any]) -> None:
    with _lock:
        _players.clear()
        slot_info = packet.get("slot_info", {})
        for player in packet.get("players", []):
            if isinstance(player, dict) and player.get("slot") is not None:
                slot = int(player["slot"])
                info = slot_info.get(str(slot), slot_info.get(slot, {}))
                _players[slot] = {
                    "name": str(player.get("alias") or player.get("name") or f"Player{slot}"),
                    "game": str(info.get("game", player.get("game", ""))),
                }


def cache_data_package(packet: dict[str, Any]) -> None:
    games = (packet.get("data") or {}).get("games", {})
    for game_name, data_pkg in games.items():
        _items[game_name] = {
            int(value): name
            for name, value in data_pkg.get("item_name_to_id", {}).items()
        }
        _locations[game_name] = {
            int(value): name
            for name, value in data_pkg.get("location_name_to_id", {}).items()
        }


def connect_socket(host: str, port: int):
    """Connect to ws:// servers, falling back to wss:// for TLS-only hosts.

    A host may also include an explicit ws:// or wss:// scheme to skip probing.
    """
    scheme = "ws"
    if host.startswith(("ws://", "wss://")):
        scheme, host = host.split("://", 1)
    host = host.strip().rstrip("/")
    url = f"{scheme}://{host}:{port}"
    try:
        return websocket_connect(url, open_timeout=10, proxy=None), url
    except (InvalidMessage, InvalidStatus):
        if scheme == "wss":
            raise
        secure_url = f"wss://{host}:{port}"
        app.logger.info("Plain websocket upgrade rejected; retrying with TLS")
        return websocket_connect(secure_url, open_timeout=10, proxy=None), secure_url


def connect_loop():
    while True:
        if not app.should_poll():
            time.sleep(1)
            continue
        host = str(app.settings["host"].get_value()).strip()
        port = app.settings["port"].get_value()
        slot_name = str(app.settings["slot"].get_value()).strip()
        password = str(app.settings["password"].get_value())
        if not host or not slot_name:
            time.sleep(2)
            continue

        ws = None
        try:
            clear_session_data()
            app.logger.info("Connecting to Archipelago at %s:%s", host, port)
            ws, connected_url = connect_socket(host, port)
            app.logger.info("Websocket connected via %s", connected_url)
            log_line(f"Connecting to {host}:{port}…", "status")

            # Request the room's name mappings before authenticating, as
            # recommended by the Archipelago client handshake.
            room_info = json.loads(ws.recv(timeout=10))
            if isinstance(room_info, list):
                room_info = room_info[0] if room_info else {}
            if room_info.get("cmd") == "RoomInfo":
                collect_room_info(room_info)
                games = room_info.get("games", [])
                if games:
                    ws.send(json.dumps([{"cmd": "GetDataPackage", "games": games}]))
                    while True:
                        response = json.loads(ws.recv(timeout=10))
                        response_packets = response if isinstance(response, list) else [response]
                        received_package = False
                        for response_packet in response_packets:
                            if isinstance(response_packet, dict) and response_packet.get("cmd") == "DataPackage":
                                cache_data_package(response_packet)
                                received_package = True
                        if received_package:
                            break
            ws.send(json.dumps([{
                "cmd": "Connect",
                "password": password or None,
                "name": slot_name,
                "game": "",
                "uuid": "pything-archipelago-monitor",
                "version": {"major": 0, "minor": 5, "build": 0, "class": "Version"},
                "items_handling": 0,
                "tags": ["AP", "Tracker", "DeathLink"],
            }]))

            own_slot = 0
            connected = False
            while app.should_poll():
                if _reconnect.is_set():
                    _reconnect.clear()
                    break
                try:
                    raw = ws.recv(timeout=2)
                except TimeoutError:
                    continue
                if not raw:
                    break
                packets = json.loads(raw)
                if isinstance(packets, dict):
                    packets = [packets]
                for packet in packets:
                    if not isinstance(packet, dict):
                        continue
                    cmd = packet.get("cmd")
                    if cmd == "RoomInfo":
                        collect_room_info(packet)
                    elif cmd == "ConnectionRefused":
                        errors = packet.get("errors", [])
                        reasons = ", ".join(errors) or "Connection refused"
                        log_line(reasons, "error")
                        raise RuntimeError(reasons)
                    elif cmd == "Connected":
                        own_slot = int(packet.get("slot", 0))
                        connected = True
                        slot_info = packet.get("slot_info", {})
                        for player in packet.get("players", []):
                            if isinstance(player, dict) and player.get("slot") is not None:
                                player_slot = int(player["slot"])
                                info = slot_info.get(str(player_slot), slot_info.get(player_slot, {}))
                                _players[player_slot] = {
                                    "name": str(player.get("alias") or player.get("name") or f"Player{player_slot}"),
                                    "game": str(info.get("game", player.get("game", ""))),
                                }
                        log_line(f"Connected to Archipelago as {slot_name}", "status")
                    elif cmd == "DataPackage":
                        cache_data_package(packet)
                    elif cmd == "PrintJSON" and connected:
                        print_json_line(packet, own_slot)
                    elif cmd == "Bounced" and connected:
                        bounce_tags = packet.get("tags", [])
                        death = packet.get("data") or {}
                        if "DeathLink" in bounce_tags and isinstance(death, dict):
                            source = str(death.get("source") or "Unknown player")
                            cause = str(death.get("cause") or "No reason given")
                            log_line(f"{source} sent a death: {cause}", "deathlink")
                    elif cmd == "ReceivedItems" and connected:
                        # ReceivedItems has no explicit recipient: it is always
                        # for this connection. PrintJSON supplies the richer,
                        # location-aware sent event when available.
                        continue
                    elif cmd == "RoomUpdate":
                        for player in packet.get("players", []):
                            if isinstance(player, dict) and player.get("slot") is not None:
                                player_slot = int(player["slot"])
                                previous = _players.get(player_slot, {})
                                _players[player_slot] = {
                                    "name": str(player.get("alias") or player.get("name") or f"Player{player_slot}"),
                                    "game": str(player.get("game", previous.get("game", ""))),
                                }
        except TimeoutError:
            # A quiet multiworld is normal; the socket timeout also lets us
            # notice when the app is closed or settings change.
            pass
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("Archipelago connection ended: %s", exc)
            log_line(f"Connection lost: {exc}", "error")
            time.sleep(3)
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        time.sleep(1)


@app.on_setting_update
def settings_updated(_):
    _reconnect.set()
    app.send("reload", "")


@app.on_startup
def start_monitor():
    threading.Thread(target=connect_loop, daemon=True).start()
