# Bifrost

Three-way chat bridge connecting **Slack**, **IRC**, and **Discord** in a single Python process. Messages sent in any of the three platforms are relayed to the other two with per-user display names and avatars.

Bifrost is the evolution of [slackIRCbridge](https://github.com/karttoon/slackIRCbridge) — the original lightweight Slack-to-IRC bridge. Bifrost adds Discord as a third platform, a raw Discord Gateway v10 client (no `discord.py` dependency), webhook-based message delivery with custom avatars, and self-healing connections with built-in health monitoring.

## Features

- **Three-way relay** — Slack, IRC, and Discord all bridged simultaneously
- **Custom avatars** — Messages appear with the sender's actual avatar (or a generated one via [RoboHash](https://robohash.org))
- **Discord webhooks** — Messages show as individual users rather than a single bot
- **Self-healing connections** — Each service thread auto-restarts on crash with exponential backoff
- **Health monitoring** — Periodic liveness checks with status logging every 5 minutes
- **Slack Socket Mode watchdog** — Detects the Slack WebSocket death spiral (SSL/broken-pipe errors) and forces a clean reconnection
- **Proactive Slack restart** — Safety valve that cycles the Slack connection every 24 hours to prevent long-running degradation
- **Discord send fallback** — If the Discord Gateway loop goes down, messages are still delivered via a synchronous webhook POST
- **Rotating log files** — 5MB log rotation with 3 backups

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp bifrost_config.example.json bifrost_config.json
```

Edit `bifrost_config.json` with your credentials (see [Configuration](#configuration) below).

### 3. Run

```bash
python3 bifrost.py
```

Or use the management script:

```bash
chmod +x bifrost_ctl.sh
./bifrost_ctl.sh start
```

## Configuration

### Slack

1. Create a [Slack App](https://api.slack.com/apps)
2. Enable **Socket Mode** and generate an app-level token with `connections:write` scope
3. Under **Event Subscriptions**, subscribe to `message.channels`
4. Add the following **Bot Token Scopes**: `chat:write`, `chat:write.customize`, `channels:read`, `users:read`
5. Install the app to your workspace
6. Add the bot to your target channel

Fill in `bifrost_config.json`:
- `bot_token` — Bot User OAuth Token (`xoxb-...`)
- `app_token` — App-Level Token (`xapp-...`)
- `channel_id` — Channel ID (find it in Channel Details at the bottom)

### IRC

Fill in `bifrost_config.json`:
- `server` — IRC server hostname
- `port` — IRC server port (typically `6667`)
- `nick` — Bot nickname
- `channel` — Channel to join (e.g. `#mychannel`)

### Discord

1. Create a [Discord Application](https://discord.com/developers/applications)
2. Under **Bot**, enable the **Message Content Intent** (required for reading message content)
3. Generate a bot token
4. Invite the bot to your server with permissions: Send Messages, Read Message History, Use External Emojis
5. (Recommended) Create a [webhook](https://support.discord.com/hc/en-us/articles/228383668) in the target channel for custom avatars

Fill in `bifrost_config.json`:
- `bot_token` — Bot token
- `channel_id` — Channel ID (enable Developer Mode in Discord settings, then right-click the channel)
- `webhook_url` — (Optional but recommended) Webhook URL for custom avatar display
- `gateway_intents` — `33281` (Guilds + Guild Messages + Message Content)

### Bridge Options

- `irc_tag` / `slack_tag` / `discord_tag` — Prefix tags shown on relayed messages (default: `[I]`, `[S]`, `[D]`)
- `avatar_service` — URL template for fallback avatars (`{username}` is replaced)
- `log_level` — `DEBUG`, `INFO`, `WARNING`, or `ERROR`
- `log_file` — Path to the application log file

## Management

`bifrost_ctl.sh` provides process management:

```
./bifrost_ctl.sh start      # Start the bridge
./bifrost_ctl.sh stop       # Stop the bridge
./bifrost_ctl.sh restart    # Stop then start
./bifrost_ctl.sh status     # Show uptime, memory, recent logs
./bifrost_ctl.sh log [N]    # Show last N log lines (default 30)
./bifrost_ctl.sh follow     # Tail the log in real-time
./bifrost_ctl.sh errors     # Show recent errors and warnings
```

## Health Monitoring

Bifrost logs a health status line every 5 minutes:

```
HEALTH: irc=UP 12h30m r=0 | slack=UP 2h15m r=1 | discord=UP 12h30m r=0 | dispatcher=UP r=0
```

Each entry shows: service name, status (UP/DOWN), uptime since last restart, and total restart count (`r=`).

If a service thread crashes, it is automatically restarted with exponential backoff (15s to 5min max). If the Slack Socket Mode connection enters a death spiral (20+ errors in 5 minutes), the watchdog forces a clean reconnection.

## Architecture

Bifrost runs four daemon threads managed by a central `BifrostBridge` class:

- **IRC thread** — `irc.bot.SingleServerIRCBot` with exponential backoff reconnection
- **Slack thread** — `slack_bolt` Socket Mode handler for real-time events
- **Discord thread** — Raw Gateway v10 client using `aiohttp` WebSockets (no `discord.py`)
- **Dispatcher thread** — Reads from a shared message queue and fans out to the other two platforms

All threads feed inbound messages into a single `queue.Queue`. The dispatcher reads from it and routes each message to the platforms it didn't originate from.

## License

MIT
