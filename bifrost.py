#!/usr/bin/env python3
"""Bifrost - Three-way chat bridge connecting Slack, IRC, and Discord."""

import os
import sys

# Ensure UTF-8 output even on servers with ASCII locale
if sys.stdout.encoding != "utf-8":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)

import asyncio
import collections
import hashlib
import json
import logging
import logging.handlers
import queue
import random
import re
import threading
import time
import urllib.request
import urllib.error

import aiohttp
import irc.bot
import irc.strings
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

__author__ = "Jeff White [karttoon] @noottrak"
__version__ = "2.2.0"
__date__ = "20AUG2026"

# ---------------------------------------------------------------------------
# Message type shared across all threads
# ---------------------------------------------------------------------------

BridgeMessage = collections.namedtuple(
    "BridgeMessage", ["source", "username", "content", "is_action", "avatar_url"]
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(path):
    """Load and validate the JSON config file."""
    with open(path, "r") as fh:
        config = json.load(fh)

    required = {
        "irc": ["server", "port", "nick", "channel"],
        "slack": ["bot_token", "app_token", "channel_id"],
        "discord": ["bot_token", "channel_id", "gateway_intents"],
        "bridge": ["log_level"],
    }
    for section, keys in required.items():
        if section not in config:
            raise ValueError("Missing config section: {}".format(section))
        for key in keys:
            if key not in config[section]:
                raise ValueError("Missing config key: {}.{}".format(section, key))

    # Defaults
    config["bridge"].setdefault("irc_tag", "[I]")
    config["bridge"].setdefault("slack_tag", "[S]")
    config["bridge"].setdefault("discord_tag", "[D]")
    config["bridge"].setdefault("avatar_service", "https://robohash.org/{username}?size=1024x1024")
    config["bridge"].setdefault("log_file", "bifrost.log")

    return config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(config):
    """Configure rotating file + console logging."""
    log_level = getattr(logging, config["bridge"]["log_level"].upper(), logging.INFO)
    log_file = config["bridge"]["log_file"]

    logger = logging.getLogger("bifrost")
    logger.setLevel(log_level)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    fh.setLevel(log_level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Route Slack SDK/bolt warnings through the main logger so Socket Mode
    # errors show up in bifrost.log instead of only in stdout.
    for lib_name in ("slack_bolt", "slack_sdk"):
        lib_logger = logging.getLogger(lib_name)
        lib_logger.setLevel(logging.WARNING)
        lib_logger.addHandler(fh)
        lib_logger.addHandler(ch)

    logging.getLogger("irc").setLevel(logging.WARNING)

    return logger

# ---------------------------------------------------------------------------
# Message sanitizers
# ---------------------------------------------------------------------------

def sanitize_for_irc(text):
    """Clean message text for IRC (single-line, stripped formatting)."""
    text = text.replace("\n", " | ")
    text = re.sub(r"<@U[A-Z0-9]+>", "[user]", text)            # Slack user mentions
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)     # Slack channel links
    text = re.sub(r"<(https?://[^|>]+)(\|[^>]+)?>", r"\1", text)  # Slack URLs
    text = re.sub(r"<@!?\d+>", "[user]", text)                  # Discord user mentions
    text = re.sub(r"<#\d+>", "[channel]", text)                  # Discord channel mentions
    if len(text.encode("utf-8")) > 450:
        text = text[:440] + "..."
    return text


def sanitize_for_slack(text):
    """Clean message text for Slack."""
    text = re.sub(r"<@!?\d+>", "[user]", text)     # Discord user mentions
    text = re.sub(r"<#\d+>", "[channel]", text)      # Discord channel mentions
    return text


def sanitize_for_discord(text):
    """Clean message text for Discord."""
    text = re.sub(r"<@U[A-Z0-9]+>", "[user]", text)            # Slack user mentions
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)     # Slack channel links
    return text

# ---------------------------------------------------------------------------
# IRC Handler
# ---------------------------------------------------------------------------

class BifrostIRCBot(irc.bot.SingleServerIRCBot):
    """IRC bot that relays messages to the bridge queue."""

    def __init__(self, config, message_queue):
        server = irc.bot.ServerSpec(config["irc"]["server"], config["irc"]["port"])
        super(BifrostIRCBot, self).__init__(
            [server],
            config["irc"]["nick"],
            config["irc"]["nick"],
            recon=irc.bot.ExponentialBackoff(min_interval=15, max_interval=300),
        )
        self.bridge_channel = config["irc"]["channel"]
        self.message_queue = message_queue
        self.logger = logging.getLogger("bifrost.irc")

    def on_nicknameinuse(self, connection, event):
        connection.nick(connection.get_nickname() + "_")

    def on_welcome(self, connection, event):
        self.logger.info("IRC connected, joining %s", self.bridge_channel)
        connection.join(self.bridge_channel)

    def on_disconnect(self, connection, event):
        self.logger.warning("IRC disconnected, will auto-reconnect")

    def on_pubmsg(self, connection, event):
        nick = event.source.nick
        if nick == connection.get_nickname():
            return
        text = event.arguments[0]
        md5 = hashlib.md5(nick.encode()).hexdigest()
        avatar = "https://robohash.org/{}?size=1024x1024".format(md5)
        self.message_queue.put(
            BridgeMessage(
                source="irc",
                username=nick,
                content=text,
                is_action=False,
                avatar_url=avatar,
            )
        )

    def on_action(self, connection, event):
        nick = event.source.nick
        if nick == connection.get_nickname():
            return
        text = event.arguments[0]
        md5 = hashlib.md5(nick.encode()).hexdigest()
        avatar = "https://robohash.org/{}?size=1024x1024".format(md5)
        self.message_queue.put(
            BridgeMessage(
                source="irc",
                username=nick,
                content=text,
                is_action=True,
                avatar_url=avatar,
            )
        )

# ---------------------------------------------------------------------------
# Slack Handler
# ---------------------------------------------------------------------------

def setup_slack(config, message_queue):
    """Create and configure the Slack app, handler, and client."""
    slack_client = WebClient(token=config["slack"]["bot_token"])
    slack_app = App(token=config["slack"]["bot_token"])
    slack_channel_id = config["slack"]["channel_id"]
    logger = logging.getLogger("bifrost.slack")

    # Cache for user ID -> (display_name, avatar_url)
    user_cache = {}

    @slack_app.event("message")
    def handle_message(event, say):
        subtype = event.get("subtype", "")
        if subtype in ("bot_message", "message_changed", "message_deleted",
                        "channel_join", "channel_leave"):
            return
        if event.get("bot_id"):
            return
        if event.get("channel") != slack_channel_id:
            return

        user_id = event.get("user", "")
        text = event.get("text", "")
        if not text:
            return

        # Resolve display name and avatar (with cache)
        if user_id in user_cache:
            display_name, avatar_url = user_cache[user_id]
        else:
            try:
                info = slack_client.users_info(user=user_id)
                profile = info["user"]["profile"]
                display_name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or info["user"].get("name", user_id)
                )
                avatar_url = (
                    profile.get("image_192")
                    or profile.get("image_72")
                    or profile.get("image_48")
                    or ""
                )
                user_cache[user_id] = (display_name, avatar_url)
            except Exception as exc:
                logger.error("Failed to resolve Slack user %s: %s", user_id, exc)
                display_name = user_id
                avatar_url = ""

        message_queue.put(
            BridgeMessage(
                source="slack",
                username=display_name,
                content=text,
                is_action=False,
                avatar_url=avatar_url,
            )
        )

    handler = SocketModeHandler(slack_app, config["slack"]["app_token"])
    return slack_app, handler, slack_client

# ---------------------------------------------------------------------------
# Discord Gateway Client
# ---------------------------------------------------------------------------

class DiscordHeartbeat(object):
    """Manages heartbeat ACK tracking for the Discord Gateway."""

    def __init__(self):
        self._ack_received = True
        self.logger = logging.getLogger("bifrost.discord.heartbeat")

    async def loop(self, ws, interval, get_sequence):
        """Send heartbeats at the given interval. Close ws if ACK missing."""
        await asyncio.sleep(interval * random.random())
        while True:
            if not self._ack_received:
                self.logger.warning("No heartbeat ACK received, closing connection")
                await ws.close()
                return
            self._ack_received = False
            try:
                await ws.send_json({"op": 1, "d": get_sequence()})
            except Exception:
                return
            await asyncio.sleep(interval)

    def ack(self):
        self._ack_received = True


class DiscordGateway(object):
    """Raw Discord Gateway v10 client using aiohttp."""

    GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
    API_BASE = "https://discord.com/api/v10"
    FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

    def __init__(self, config, message_queue):
        self.token = config["discord"]["bot_token"]
        self.channel_id = config["discord"]["channel_id"]
        self.webhook_url = config["discord"].get("webhook_url", "")
        self.intents = config["discord"].get("gateway_intents", 33281)
        self.config = config
        self.message_queue = message_queue
        self.bot_user_id = None
        self.session_id = None
        self.sequence = None
        self.resume_url = None
        self.loop = None
        self._session = None
        self.logger = logging.getLogger("bifrost.discord")

    def run_in_thread(self):
        """Entry point for the Discord thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run())
        finally:
            self.loop.close()

    async def _run(self):
        """Main gateway loop with reconnection."""
        self._session = aiohttp.ClientSession()
        try:
            while True:
                try:
                    await self._connect_and_listen()
                except Exception as exc:
                    self.logger.error("Gateway error: %s", exc)
                delay = 5 + random.random() * 5
                self.logger.info("Discord reconnecting in %.1fs...", delay)
                await asyncio.sleep(delay)
        finally:
            await self._session.close()

    async def _connect_and_listen(self):
        """Single connection lifecycle: Hello -> Identify/Resume -> event loop."""
        url = self.resume_url or self.GATEWAY_URL
        self.logger.info("Connecting to Gateway: %s", url[:60])

        async with self._session.ws_connect(url) as ws:
            # 1. Receive Hello (opcode 10)
            hello = await ws.receive_json()
            if hello.get("op") != 10:
                self.logger.error("Expected Hello (op 10), got op=%s", hello.get("op"))
                return
            heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
            self.logger.info("Hello received, heartbeat interval=%.1fs", heartbeat_interval)

            # 2. Start heartbeat
            hb = DiscordHeartbeat()
            hb_task = asyncio.ensure_future(
                hb.loop(ws, heartbeat_interval, lambda: self.sequence)
            )

            # 3. Identify or Resume
            if self.session_id and self.sequence is not None:
                await ws.send_json({
                    "op": 6,
                    "d": {
                        "token": self.token,
                        "session_id": self.session_id,
                        "seq": self.sequence,
                    },
                })
                self.logger.info("Sent Resume (session=%s, seq=%s)", self.session_id, self.sequence)
            else:
                await ws.send_json({
                    "op": 2,
                    "d": {
                        "token": self.token,
                        "intents": self.intents,
                        "properties": {
                            "os": "linux",
                            "browser": "bifrost",
                            "device": "bifrost",
                        },
                    },
                })
                self.logger.info("Sent Identify (intents=%d)", self.intents)

            # 4. Event loop
            try:
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(ws_msg.data)
                        op = payload.get("op")

                        if op == 0:  # Dispatch
                            self.sequence = payload.get("s", self.sequence)
                            event_name = payload.get("t", "")
                            await self._handle_dispatch(event_name, payload.get("d", {}))

                        elif op == 1:  # Heartbeat request
                            await ws.send_json({"op": 1, "d": self.sequence})

                        elif op == 7:  # Reconnect
                            self.logger.info("Server requested reconnect")
                            break

                        elif op == 9:  # Invalid Session
                            can_resume = payload.get("d", False)
                            if not can_resume:
                                self.session_id = None
                                self.sequence = None
                                self.resume_url = None
                                self.logger.info("Session invalidated, will re-identify")
                            else:
                                self.logger.info("Session invalidated but resumable")
                            await asyncio.sleep(random.uniform(1, 5))
                            break

                        elif op == 11:  # Heartbeat ACK
                            hb.ack()

                    elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        self.logger.warning("WebSocket closed/error: %s", ws_msg.type)
                        break
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

    async def _handle_dispatch(self, event_name, data):
        """Handle Gateway dispatch events."""
        if event_name == "READY":
            self.session_id = data["session_id"]
            self.resume_url = data.get("resume_gateway_url")
            if self.resume_url and "?" not in self.resume_url:
                self.resume_url += "?v=10&encoding=json"
            self.bot_user_id = data["user"]["id"]
            self.logger.info(
                "Ready as %s (id=%s)", data["user"]["username"], self.bot_user_id
            )

        elif event_name == "RESUMED":
            self.logger.info("Session resumed successfully")

        elif event_name == "MESSAGE_CREATE":
            await self._handle_message(data)

    async def _handle_message(self, data):
        """Process an incoming Discord message."""
        author = data.get("author", {})

        # Echo suppression
        if author.get("id") == self.bot_user_id:
            return
        if author.get("bot", False):
            return
        if data.get("webhook_id"):
            return
        if data.get("channel_id") != self.channel_id:
            return

        content = data.get("content", "")
        if not content:
            return

        username = author.get("username", "unknown")
        avatar_hash = author.get("avatar", "")
        if avatar_hash:
            avatar_url = "https://cdn.discordapp.com/avatars/{}/{}.png".format(
                author["id"], avatar_hash
            )
        else:
            avatar_url = ""

        self.message_queue.put(
            BridgeMessage(
                source="discord",
                username=username,
                content=content,
                is_action=False,
                avatar_url=avatar_url,
            )
        )

    async def send_message(self, msg):
        """Send a bridged message to Discord."""
        if self.webhook_url:
            await self._send_via_webhook(msg)
        else:
            await self._send_via_bot(msg)

    async def _send_via_webhook(self, msg):
        """Send using a Discord webhook (custom username + avatar per message)."""
        tag = self.config["bridge"]["irc_tag"] if msg.source == "irc" else self.config["bridge"]["slack_tag"]
        display_name = "{} {}".format(tag, msg.username)
        if msg.is_action:
            content = "\\* {} {}".format(msg.username, sanitize_for_discord(msg.content))
        else:
            content = sanitize_for_discord(msg.content)

        avatar = msg.avatar_url or self.config["bridge"]["avatar_service"].format(
            username=msg.username
        )
        payload = {
            "content": content,
            "username": display_name[:80],
            "avatar_url": avatar,
        }

        for attempt in range(3):
            try:
                async with self._session.post(self.webhook_url, json=payload) as resp:
                    if resp.status == 429:
                        retry_data = await resp.json()
                        wait = retry_data.get("retry_after", 1.0)
                        self.logger.warning("Discord rate limited, waiting %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        self.logger.error("Webhook error %s: %s", resp.status, body)
                    return
            except Exception as exc:
                self.logger.error("Webhook send error (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)

    async def _send_via_bot(self, msg):
        """Fallback: send using bot REST API (all messages show as the bot)."""
        tag = self.config["bridge"]["irc_tag"] if msg.source == "irc" else self.config["bridge"]["slack_tag"]
        if msg.is_action:
            content = "\\* {} {}".format(msg.username, sanitize_for_discord(msg.content))
        else:
            content = "{} <{}> {}".format(tag, msg.username, sanitize_for_discord(msg.content))

        url = "{}/channels/{}/messages".format(self.API_BASE, self.channel_id)
        headers = {
            "Authorization": "Bot {}".format(self.token),
            "Content-Type": "application/json",
        }
        payload = {"content": content}

        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        retry_data = await resp.json()
                        wait = retry_data.get("retry_after", 1.0)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        body = await resp.text()
                        self.logger.error("Bot send error %s: %s", resp.status, body)
                    return
            except Exception as exc:
                self.logger.error("Bot send error (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)

    def schedule_send(self, msg):
        """Thread-safe: schedule a send on the asyncio loop from another thread."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.send_message(msg), self.loop)
        elif self.webhook_url:
            self.logger.warning("Gateway loop not running, using sync webhook fallback")
            self._send_webhook_sync(msg)
        else:
            self.logger.error("Cannot send to Discord: gateway loop down and no webhook")

    def _send_webhook_sync(self, msg):
        """Synchronous webhook fallback using urllib (no asyncio dependency)."""
        tag = self.config["bridge"]["irc_tag"] if msg.source == "irc" else self.config["bridge"]["slack_tag"]
        display_name = "{} {}".format(tag, msg.username)
        if msg.is_action:
            content = "\\* {} {}".format(msg.username, sanitize_for_discord(msg.content))
        else:
            content = sanitize_for_discord(msg.content)

        avatar = msg.avatar_url or self.config["bridge"]["avatar_service"].format(
            username=msg.username
        )
        data = json.dumps({
            "content": content,
            "username": display_name[:80],
            "avatar_url": avatar,
        }).encode("utf-8")

        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 204):
                        return
                    self.logger.error("Sync webhook status %s", resp.status)
                    return
            except Exception as exc:
                self.logger.error("Sync webhook error (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)

# ---------------------------------------------------------------------------
# Message Dispatcher
# ---------------------------------------------------------------------------

class MessageDispatcher(object):
    """Reads BridgeMessages from the queue and fans out to other platforms."""

    def __init__(self, message_queue, bridge, config):
        self.queue = message_queue
        self.bridge = bridge
        self.config = config
        self.logger = logging.getLogger("bifrost.dispatcher")

    def run(self):
        self.logger.info("Dispatcher running")
        while True:
            msg = self.queue.get()
            if msg is None:
                self.logger.info("Dispatcher shutting down")
                break

            self.logger.info(
                "[%s] %s: %s", msg.source.upper(), msg.username,
                msg.content[:100] + ("..." if len(msg.content) > 100 else ""),
            )

            try:
                if msg.source != "irc":
                    self._send_to_irc(msg)
                if msg.source != "slack":
                    self._send_to_slack(msg)
                if msg.source != "discord":
                    self._send_to_discord(msg)
            except Exception as exc:
                self.logger.error("Dispatcher error: %s", exc)

    # -- IRC outbound ---------------------------------------------------------

    def _send_to_irc(self, msg):
        irc_bot = self.bridge.irc_bot
        if irc_bot is None:
            self.logger.warning("IRC not available, dropping message")
            return

        tag = self.config["bridge"]["discord_tag"] if msg.source == "discord" else self.config["bridge"]["slack_tag"]
        if msg.is_action:
            text = "* {} {}".format(msg.username, sanitize_for_irc(msg.content))
        else:
            text = "{} <{}> {}".format(tag, msg.username, sanitize_for_irc(msg.content))

        try:
            if irc_bot.connection.is_connected():
                irc_bot.connection.privmsg(self.config["irc"]["channel"], text)
            else:
                self.logger.warning("IRC not connected, dropping message")
        except Exception as exc:
            self.logger.error("IRC send error: %s", exc)

    # -- Slack outbound -------------------------------------------------------

    def _send_to_slack(self, msg):
        slack_client = self.bridge.slack_client
        if slack_client is None:
            self.logger.warning("Slack not available, dropping message")
            return

        tag = self.config["bridge"]["discord_tag"] if msg.source == "discord" else self.config["bridge"]["irc_tag"]
        display_name = "{} {}".format(tag, msg.username)

        if msg.is_action:
            text = "_{} {}_".format(msg.username, sanitize_for_slack(msg.content))
        else:
            text = sanitize_for_slack(msg.content)

        avatar = msg.avatar_url or self.config["bridge"]["avatar_service"].format(
            username=msg.username
        )

        for attempt in range(3):
            try:
                slack_client.chat_postMessage(
                    channel=self.config["slack"]["channel_id"],
                    text=text,
                    username=display_name,
                    icon_url=avatar,
                )
                return
            except SlackApiError as exc:
                if exc.response.get("error") == "ratelimited":
                    delay = int(exc.response.headers.get("Retry-After", 1))
                    self.logger.warning("Slack rate limited, waiting %ds", delay)
                    time.sleep(delay)
                else:
                    self.logger.error("Slack API error: %s", exc)
                    return
            except Exception as exc:
                self.logger.error("Slack send error (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)

    # -- Discord outbound -----------------------------------------------------

    def _send_to_discord(self, msg):
        discord_gw = self.bridge.discord_gw
        if discord_gw is None:
            self.logger.warning("Discord not available, dropping message")
            return
        discord_gw.schedule_send(msg)

# ---------------------------------------------------------------------------
# Slack Error Monitor
# ---------------------------------------------------------------------------

class SlackErrorMonitor(logging.Handler):
    """Counts Slack library errors; triggers a forced restart on death spiral."""

    def __init__(self, bridge, threshold=20, window=300):
        super(SlackErrorMonitor, self).__init__(level=logging.WARNING)
        self.bridge = bridge
        self.threshold = threshold
        self.window = window
        self._timestamps = collections.deque()

    def emit(self, record):
        now = time.time()
        self._timestamps.append(now)
        while self._timestamps and self._timestamps[0] < now - self.window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.threshold:
            self._timestamps.clear()
            self.bridge.force_restart_slack()

# ---------------------------------------------------------------------------
# Bridge Manager
# ---------------------------------------------------------------------------

class BifrostBridge(object):
    """Central manager with health monitoring and auto-restart for all services."""

    HEALTH_CHECK_INTERVAL = 60
    HEALTH_LOG_INTERVAL = 300
    MAX_RESTART_DELAY = 300
    SLACK_MAX_UPTIME = 24 * 3600
    IRC_DISCONNECT_GRACE = 300

    def __init__(self, config):
        self.config = config
        self.message_queue = queue.Queue()
        self.logger = logging.getLogger("bifrost")
        self._lock = threading.Lock()

        # Current service references (read by the dispatcher)
        self.irc_bot = None
        self.slack_client = None
        self.discord_gw = None

        # Threads
        self._threads = {}

        # Health tracking
        self._restart_counts = collections.Counter()
        self._service_started = {}

        # Slack restart signal — set by error monitor or health loop
        self._slack_restart_flag = threading.Event()

        # IRC restart signal — set by health loop on prolonged disconnect
        self._irc_restart_flag = threading.Event()
        self._irc_disconnect_since = None

        # Install Slack error monitor
        monitor = SlackErrorMonitor(self)
        for lib_name in ("slack_bolt", "slack_sdk"):
            logging.getLogger(lib_name).addHandler(monitor)

    # -- Service runners (each loops forever, restarting on crash) -----------

    def _run_irc(self):
        delay = 15
        while True:
            try:
                bot = BifrostIRCBot(self.config, self.message_queue)
                with self._lock:
                    self.irc_bot = bot
                self._service_started["irc"] = time.time()
                self._irc_disconnect_since = None
                self._irc_restart_flag.clear()
                delay = 15
                bot._connect()
                while not self._irc_restart_flag.is_set():
                    bot.reactor.process_once(0.2)
                self.logger.info("IRC service cycling (restart flag set)")
            except Exception:
                self.logger.exception("IRC service crashed")
            self._restart_counts["irc"] += 1
            self.logger.info(
                "HEALTH: IRC restarting in %ds (restart #%d)",
                delay, self._restart_counts["irc"],
            )
            time.sleep(delay)
            delay = min(delay * 2, self.MAX_RESTART_DELAY)

    def _run_slack(self):
        delay = 15
        while True:
            handler = None
            try:
                app, handler, client = setup_slack(self.config, self.message_queue)
                with self._lock:
                    self.slack_client = client
                self._service_started["slack"] = time.time()
                self._slack_restart_flag.clear()
                delay = 15
                handler.connect()
                self.logger.info("Slack service connected")
                started = time.time()
                while not self._slack_restart_flag.is_set():
                    if time.time() - started > self.SLACK_MAX_UPTIME:
                        self.logger.info(
                            "HEALTH: Proactive Slack restart (uptime %dh)",
                            int((time.time() - started) / 3600),
                        )
                        break
                    self._slack_restart_flag.wait(30)
            except Exception:
                self.logger.exception("Slack service crashed")
            finally:
                if handler:
                    try:
                        handler.close()
                    except Exception:
                        pass
            self._restart_counts["slack"] += 1
            self.logger.info(
                "HEALTH: Slack restarting in %ds (restart #%d)",
                delay, self._restart_counts["slack"],
            )
            time.sleep(delay)
            delay = min(delay * 2, self.MAX_RESTART_DELAY)

    def _run_discord(self):
        delay = 15
        while True:
            try:
                gw = DiscordGateway(self.config, self.message_queue)
                with self._lock:
                    self.discord_gw = gw
                self._service_started["discord"] = time.time()
                delay = 15
                gw.run_in_thread()
                self.logger.warning("Discord service exited unexpectedly")
            except Exception:
                self.logger.exception("Discord service crashed")
            self._restart_counts["discord"] += 1
            self.logger.info(
                "HEALTH: Discord restarting in %ds (restart #%d)",
                delay, self._restart_counts["discord"],
            )
            time.sleep(delay)
            delay = min(delay * 2, self.MAX_RESTART_DELAY)

    def _run_dispatcher(self):
        while True:
            try:
                dispatcher = MessageDispatcher(
                    self.message_queue, self, self.config,
                )
                dispatcher.run()
            except Exception:
                self.logger.exception("Dispatcher crashed, restarting in 5s")
            self._restart_counts["dispatcher"] += 1
            time.sleep(5)

    # -- Forced restart -------------------------------------------------------

    def force_restart_slack(self):
        """Force a Slack reconnection by signaling the Slack thread."""
        if self._slack_restart_flag.is_set():
            return
        self.logger.warning("HEALTH: Forcing Slack reconnection (error-rate trigger)")
        self._slack_restart_flag.set()

    # -- Start and health loop ------------------------------------------------

    def start(self):
        targets = [
            ("irc", self._run_irc),
            ("slack", self._run_slack),
            ("discord", self._run_discord),
            ("dispatcher", self._run_dispatcher),
        ]
        for name, target in targets:
            t = threading.Thread(target=target, name="{}-thread".format(name), daemon=True)
            t.start()
            self._threads[name] = t
            self.logger.info("%s thread started", name.capitalize())

        self.logger.info("Bifrost is running. Press Ctrl+C to stop.")
        self._health_loop()

    def _health_loop(self):
        last_full_log = 0
        try:
            while True:
                time.sleep(self.HEALTH_CHECK_INTERVAL)
                now = time.time()

                # Check thread liveness — restart if a thread exited entirely
                for name in list(self._threads):
                    if not self._threads[name].is_alive():
                        self.logger.error("HEALTH: %s thread DEAD, respawning", name)
                        target = {
                            "irc": self._run_irc,
                            "slack": self._run_slack,
                            "discord": self._run_discord,
                            "dispatcher": self._run_dispatcher,
                        }[name]
                        t = threading.Thread(
                            target=target,
                            name="{}-thread".format(name),
                            daemon=True,
                        )
                        t.start()
                        self._threads[name] = t

                # IRC connection health — detect stale disconnects
                bot = self.irc_bot
                if bot is not None:
                    try:
                        connected = bot.connection.is_connected()
                    except Exception:
                        connected = False
                    if not connected:
                        if self._irc_disconnect_since is None:
                            self._irc_disconnect_since = now
                        elif now - self._irc_disconnect_since > self.IRC_DISCONNECT_GRACE:
                            self.logger.warning(
                                "HEALTH: IRC disconnected >%ds, forcing restart",
                                self.IRC_DISCONNECT_GRACE,
                            )
                            self._irc_disconnect_since = None
                            self._irc_restart_flag.set()
                    else:
                        self._irc_disconnect_since = None

                # Periodic full status log
                if now - last_full_log >= self.HEALTH_LOG_INTERVAL:
                    last_full_log = now
                    self._log_health_status()

        except KeyboardInterrupt:
            self.logger.info("Shutting down Bifrost...")
            self.message_queue.put(None)
            sys.exit(0)

    def _log_health_status(self):
        parts = []
        for name in ("irc", "slack", "discord", "dispatcher"):
            thread = self._threads.get(name)
            alive = "UP" if (thread and thread.is_alive()) else "DOWN"
            restarts = self._restart_counts[name]
            uptime_str = ""
            started = self._service_started.get(name)
            if started:
                elapsed = time.time() - started
                hours = int(elapsed / 3600)
                mins = int((elapsed % 3600) / 60)
                uptime_str = " {}h{}m".format(hours, mins)
            extra = ""
            if name == "irc" and self.irc_bot is not None:
                try:
                    if not self.irc_bot.connection.is_connected():
                        extra = " DISCONNECTED"
                except Exception:
                    extra = " DISCONNECTED"
            parts.append("{}={}{}{} r={}".format(name, alive, uptime_str, extra, restarts))
        self.logger.info("HEALTH: %s", " | ".join(parts))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "bifrost_config.json"

    if not os.path.exists(config_path):
        print("Config file not found: {}".format(config_path))
        sys.exit(1)

    config = load_config(config_path)
    logger = setup_logging(config)
    logger.info("Bifrost v%s starting...", __version__)

    bridge = BifrostBridge(config)
    bridge.start()


if __name__ == "__main__":
    main()
