# PlexManager

A Discord bot that connects to your NAS media library, providing search, browsing, rich metadata from TMDb, real-time notifications when files are added or removed, and a Blu-ray upgrade finder that identifies low-resolution movies and searches eBay for deals.

## Features

- **🔍 Media Search** — `/search` to find movies and TV shows by title
- **📋 Detailed Info** — `/info` for rich embeds with TMDb posters, ratings, synopsis, and file details
- **📄 Library Browsing** — `/list` to browse all movies or TV shows, with optional title filtering
- **🎲 Random Pick** — `/random` to get a random movie or show when you can't decide
- **🎭 Genre Browse** — `/genre` to explore your library by genre
- **⚠️ Duplicates** — `/duplicates` to find duplicate movies eating up disk space
- **📥 Recent Additions** — `/recent` to see what's been added lately
- **📊 Library Stats** — `/stats` for total movies, shows, episodes, and disk usage
- **🔔 Real-time Notifications** — Instant Discord alerts when files are added or removed from your NAS
- **🔄 Manual Rescan** — `/rescan` to trigger a full library scan on demand
- **📜 Change History** — `/notifications` to view recent file change events
- **📂 Multiple Media Paths** — Scan and watch multiple directories (e.g. Movies + TV Shows)

### 📀 Blu-ray Upgrade Finder

- **`/upgrades list`** — View all DVD-quality (≤480p) movies that could be upgraded to Blu-ray
- **`/upgrades check <title>`** — Check eBay Blu-ray prices for a specific movie
- **`/upgrades sellcheck <title>`** — Check what your DVD could sell for on eBay
- **`/upgrades deals`** — View all Blu-ray deals found below average price
- **`/upgrades scan`** — Trigger a full eBay deal search across all low-res movies
- **`/upgrades status`** — Dashboard showing tracked, purchased, ignored, and unmatched counts
- **`/upgrades purchased <title>`** — Mark a movie as purchased (stops deal alerts)
- **`/upgrades ignore <title>`** — Exclude a movie from future upgrade scans
- **`/upgrades unmatched`** — View movies that failed TMDb lookup for manual fixing
- **`/upgrades rescan_resolution`** — Probe resolution for movies not yet scanned
- **Auto-scheduling** — Daily eBay scans with deal notifications posted to a dedicated channel
- **Smart filtering** — TMDb release checks to auto-skip movies with no Blu-ray release
- **Interactive notifications** — Deal embeds include "View on eBay" and "Purchased" buttons

## Prerequisites

- Python 3.9+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- A TMDb API key ([get one here](https://www.themoviedb.org/settings/api))
- NAS media library mounted as a local path
- **ffmpeg** (for resolution detection) — `brew install ffmpeg`
- **eBay Developer account** (optional, for deal searching) — [apply here](https://developer.ebay.com/)

## Setup

### 1. Clone and install dependencies

```bash
cd PlexManager
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** and give it a name
3. Go to **Bot** → click **Add Bot**
4. Copy the **Token**
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2** → **URL Generator**
7. Select scopes: `bot`, `applications.commands`
8. Select permissions: `Send Messages`, `Embed Links`, `Read Message History`
9. Use the generated URL to invite the bot to your server

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
DISCORD_TOKEN=your_discord_bot_token_here
TMDB_API_KEY=your_tmdb_api_key_here
MEDIA_PATHS=/path/to/movies,/path/to/tv/shows
NOTIFICATION_CHANNEL_ID=your_channel_id_here
GUILD_ID=your_guild_id_here

# Optional — eBay Blu-ray deal searching
EBAY_APP_ID=your_ebay_app_id_here
EBAY_CERT_ID=your_ebay_cert_id_here
UPGRADE_CHANNEL_ID=your_upgrade_channel_id_here
```

### 4. Run the bot

```bash
python main.py
```

The bot will:
1. Validate configuration
2. Connect to the SQLite database
3. Run an initial scan of all media paths
4. Start watching for file changes across all paths
5. Connect to Discord and sync slash commands

## Media Library Structure

The scanner expects your media organized in a standard layout:

```
/Volumes/Media/
├── Movies/
│   ├── The Matrix (1999)/
│   │   └── The Matrix (1999).mkv
│   └── Inception (2010)/
│       └── Inception (2010).mkv
└── TV Shows/
    └── Breaking Bad/
        ├── Season 01/
        │   ├── S01E01 - Pilot.mkv
        │   └── S01E02 - Cat's in the Bag.mkv
        └── Season 02/
            └── S02E01 - Seven Thirty-Seven.mkv
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/search <query> [type]` | Search media by title (defaults to Movies, select TV Show to search shows) |
| `/info <title>` | Detailed info card with poster, rating, synopsis |
| `/list [type] [query]` | List all movies or TV shows, with optional title filter |
| `/random [type]` | Pick a random movie or TV show |
| `/genre [genre] [type]` | Browse by genre, or list available genres |
| `/duplicates` | Find duplicate movies in your library |
| `/recent [count]` | Show recently added media (default: 10) |
| `/stats` | Library statistics |
| `/rescan` | Trigger a manual library rescan |
| `/status` | Bot status and uptime |
| `/notifications [count]` | View recent file change history |
| `/upgrades list` | View all low-res movies (≤480p) |
| `/upgrades check <title>` | Check eBay Blu-ray prices for a movie |
| `/upgrades sellcheck <title>` | Check what your DVD could sell for |
| `/upgrades deals` | View Blu-ray deals found below average price |
| `/upgrades scan` | Trigger a full eBay deal search |
| `/upgrades status` | Upgrade scanner dashboard |
| `/upgrades purchased <title>` | Mark a movie as purchased |
| `/upgrades ignore <title>` | Exclude a movie from upgrade scans |
| `/upgrades unmatched` | Movies that failed TMDb lookup |
| `/upgrades rescan_resolution` | Probe resolution for unscanned movies |

## Configuration

| Variable | Description | Required |
|----------|-------------|----------|
| `DISCORD_TOKEN` | Discord bot token | Yes |
| `TMDB_API_KEY` | TMDb API key | Yes |
| `MEDIA_PATHS` | Comma-separated paths to media directories | Yes |
| `NOTIFICATION_CHANNEL_ID` | Discord channel for file change alerts | Yes |
| `GUILD_ID` | Discord server ID for instant slash command sync | Yes |
| `LOG_LEVEL` | Logging level (default: `INFO`) | No |
| `SCAN_INTERVAL_MINUTES` | Periodic scan interval (default: `60`) | No |
| `DB_PATH` | SQLite database path (default: `plexmanager.db`) | No |
| `EBAY_APP_ID` | eBay application ID for Browse API | No |
| `EBAY_CERT_ID` | eBay certificate ID for OAuth | No |
| `UPGRADE_CHANNEL_ID` | Dedicated channel for upgrade deal notifications | No |
| `UPGRADE_SCAN_INTERVAL_HOURS` | How often to scan eBay for deals (default: `24`) | No |
| `MAX_EBAY_PRICE` | Maximum price cap for eBay searches (default: no cap) | No |

> **Note:** The legacy `MEDIA_PATH` variable (single path) is still supported for backwards compatibility.

## Supported Formats

`.mkv`, `.mp4`, `.avi`, `.m4v`, `.ts`, `.mov`, `.wmv`

## License

MIT
