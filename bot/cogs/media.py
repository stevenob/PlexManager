from __future__ import annotations

import math
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from media.models import MediaFile, MediaType, Movie, Episode

if TYPE_CHECKING:
    from bot.client import PlexManagerBot


RESULTS_PER_PAGE = 10
EMBED_COLOR_MOVIE = discord.Color.blue()
EMBED_COLOR_TV = discord.Color.green()
EMBED_COLOR_DEFAULT = discord.Color.greyple()


def _media_color(media: MediaFile) -> discord.Color:
    if media.media_type == MediaType.MOVIE:
        return EMBED_COLOR_MOVIE
    if media.media_type in (MediaType.EPISODE, MediaType.TV_SHOW):
        return EMBED_COLOR_TV
    return EMBED_COLOR_DEFAULT


def _type_label(media: MediaFile) -> str:
    return media.media_type.value.replace("_", " ").title()


class MediaCog(commands.Cog):
    """Commands for browsing and inspecting the media library."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot

    @app_commands.command(name="search", description="Search media by title")
    @app_commands.describe(
        query="Search term",
        type="Filter by media type (default: Movie)",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Movie", value="movie"),
            app_commands.Choice(name="TV Show", value="tv_show"),
        ]
    )
    async def search(
        self,
        interaction: discord.Interaction,
        query: str,
        type: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if type and type.value == "tv_show":
            await self._search_shows(interaction, query)
            return

        media_type = MediaType(type.value) if type else MediaType.MOVIE
        results: list[MediaFile] = await self.bot.db.search(
            query, media_type=media_type, limit=RESULTS_PER_PAGE
        )

        if not results:
            await interaction.response.send_message(
                f"No media found matching '{query}'", ephemeral=True
            )
            return

        total_pages = max(1, math.ceil(len(results) / RESULTS_PER_PAGE))
        page_results = results

        embed = discord.Embed(
            title=f"Search results for '{query}'",
            color=discord.Color.blurple(),
        )

        for item in page_results:
            year_str = f" ({item.year})" if item.year else ""
            size_str = item.human_size
            value_parts = [f"**Type:** {_type_label(item)}", f"**Size:** {size_str}"]
            if item.resolution_label != "Unknown":
                value_parts.append(f"**Res:** {item.resolution_label}")
            if item.codec_label != "Unknown":
                value_parts.append(f"**Codec:** {item.codec_label}")
            embed.add_field(
                name=f"{item.title}{year_str}",
                value=" · ".join(value_parts),
                inline=False,
            )

        if page_results and page_results[0].poster_url:
            embed.set_thumbnail(url=page_results[0].poster_url)

        embed.set_footer(text=f"Page 1/{total_pages} · {len(results)} result(s)")
        await interaction.response.send_message(embed=embed)

    async def _search_shows(
        self, interaction: discord.Interaction, query: str
    ) -> None:
        shows = await self.bot.db.search_shows(query, limit=RESULTS_PER_PAGE)

        if not shows:
            await interaction.response.send_message(
                f"No TV shows found matching '{query}'", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"TV Shows matching '{query}'",
            color=EMBED_COLOR_TV,
        )

        for show in shows:
            year_str = f" ({show['year']})" if show.get("year") else ""
            size_str = _human_bytes(show.get("total_size", 0))
            embed.add_field(
                name=f"{show['show_title']}{year_str}",
                value=(
                    f"**Seasons:** {show['seasons']} · "
                    f"**Episodes:** {show['episodes']} · "
                    f"**Size:** {size_str}"
                ),
                inline=False,
            )

        if shows and shows[0].get("poster_url"):
            embed.set_thumbnail(url=shows[0]["poster_url"])

        embed.set_footer(text=f"{len(shows)} show(s) found")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="Get detailed info for a media item")
    @app_commands.describe(title="Exact title of the media item")
    async def info(self, interaction: discord.Interaction, title: str) -> None:
        results: list[MediaFile] = await self.bot.db.search(title, limit=5)

        # Prefer exact match, fall back to first fuzzy result
        media = None
        for r in results:
            if r.title and r.title.lower() == title.lower():
                media = r
                break
        if media is None and results:
            media = results[0]

        if media is None:
            await interaction.response.send_message(
                f"No media found matching '{title}'", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=media.title or media.filename,
            color=_media_color(media),
        )

        if media.year:
            embed.add_field(name="Year", value=str(media.year), inline=True)
        if media.rating is not None:
            embed.add_field(name="Rating", value=f"⭐ {media.rating:.1f}", inline=True)
        if media.genres:
            embed.add_field(name="Genres", value=", ".join(media.genres), inline=True)

        if isinstance(media, Movie):
            if media.runtime:
                hours, mins = divmod(media.runtime, 60)
                runtime_str = f"{hours}h {mins}m" if hours else f"{mins}m"
                embed.add_field(name="Runtime", value=runtime_str, inline=True)
            if media.director:
                embed.add_field(name="Director", value=media.director, inline=True)

        if isinstance(media, Episode):
            if media.show_title:
                embed.add_field(name="Show", value=media.show_title, inline=True)
            if media.season_number is not None and media.episode_number is not None:
                embed.add_field(
                    name="Episode",
                    value=f"S{media.season_number:02d}E{media.episode_number:02d}",
                    inline=True,
                )

        if media.overview:
            synopsis = (
                media.overview if len(media.overview) <= 1024 else media.overview[:1021] + "…"
            )
            embed.add_field(name="Synopsis", value=synopsis, inline=False)

        file_info = (
            f"**Path:** `{media.path}`\n"
            f"**Size:** {media.human_size}\n"
            f"**Format:** {media.extension}"
        )
        if media.resolution_label != "Unknown":
            file_info += f"\n**Resolution:** {media.resolution_label}"
        if media.codec_label != "Unknown":
            file_info += f"\n**Codec:** {media.codec_label}"
        embed.add_field(name="File Details", value=file_info, inline=False)

        if media.poster_url:
            embed.set_thumbnail(url=media.poster_url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="recent", description="Show recently added media")
    @app_commands.describe(count="Number of items to show (max 25)")
    async def recent(
        self, interaction: discord.Interaction, count: Optional[int] = None
    ) -> None:
        count = min(max(count or 10, 1), 25)
        items: list[MediaFile] = await self.bot.db.get_recent(limit=count)

        if not items:
            await interaction.response.send_message(
                "No media in the library yet.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Recently Added Media",
            color=discord.Color.gold(),
        )

        for item in items:
            added = discord.utils.format_dt(item.created_at, style="R")
            embed.add_field(
                name=item.title or item.filename,
                value=(
                    f"**Type:** {_type_label(item)} · "
                    f"**Size:** {item.human_size} · "
                    f"**Added:** {added}"
                ),
                inline=False,
            )

        embed.set_footer(text=f"Showing {len(items)} item(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="list", description="List movies or TV shows in your library")
    @app_commands.describe(
        type="What to list (default: Movies)",
        query="Optional filter by title",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Movies", value="movie"),
            app_commands.Choice(name="TV Shows", value="tv_show"),
        ]
    )
    async def list_media(
        self,
        interaction: discord.Interaction,
        type: Optional[app_commands.Choice[str]] = None,
        query: Optional[str] = None,
    ) -> None:
        embeds: list[discord.Embed] = []
        selected = type.value if type else "movie"

        if selected == "movie":
            movies = await self.bot.db.list_movies(query=query, limit=25)
            filter_label = f' matching "{query}"' if query else ""
            embed = discord.Embed(
                title=f"🎬 Movies{filter_label}",
                color=EMBED_COLOR_MOVIE,
            )
            if movies:
                lines = []
                for m in movies:
                    year_str = f" ({m.year})" if m.year else ""
                    lines.append(f"• {m.title}{year_str} — {m.human_size}")
                embed.description = "\n".join(lines)
                embed.set_footer(text=f"{len(movies)} movie(s)")
            else:
                embed.description = "No movies found."
            embeds.append(embed)

        if selected == "tv_show":
            shows = await self.bot.db.list_shows(query=query, limit=25)
            filter_label = f' matching "{query}"' if query else ""
            embed = discord.Embed(
                title=f"📺 TV Shows{filter_label}",
                color=EMBED_COLOR_TV,
            )
            if shows:
                lines = []
                for s in shows:
                    year_str = f" ({s['year']})" if s.get("year") else ""
                    lines.append(
                        f"• {s['show_title']}{year_str} — "
                        f"{s['seasons']}S / {s['episodes']}E — "
                        f"{_human_bytes(s.get('total_size', 0))}"
                    )
                embed.description = "\n".join(lines)
                embed.set_footer(text=f"{len(shows)} show(s)")
            else:
                embed.description = "No TV shows found."
            embeds.append(embed)

        await interaction.response.send_message(embeds=embeds)

    @app_commands.command(name="random", description="Pick a random movie or TV show")
    @app_commands.describe(type="Filter by media type (default: Movie)")
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Movie", value="movie"),
            app_commands.Choice(name="TV Show", value="tv_show"),
        ]
    )
    async def random(
        self,
        interaction: discord.Interaction,
        type: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        media = await self.bot.db.get_random(
            media_type=type.value if type else "movie"
        )

        if not media:
            await interaction.response.send_message(
                "Your library is empty!", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🎲 {media.title or media.filename}",
            color=_media_color(media),
        )

        if media.year:
            embed.add_field(name="Year", value=str(media.year), inline=True)
        if media.rating is not None:
            embed.add_field(name="Rating", value=f"⭐ {media.rating:.1f}", inline=True)
        if media.genres:
            embed.add_field(name="Genres", value=", ".join(media.genres), inline=True)

        if isinstance(media, Movie) and media.runtime:
            hours, mins = divmod(media.runtime, 60)
            runtime_str = f"{hours}h {mins}m" if hours else f"{mins}m"
            embed.add_field(name="Runtime", value=runtime_str, inline=True)

        if isinstance(media, Episode):
            if media.show_title:
                embed.add_field(name="Show", value=media.show_title, inline=True)
            if media.season_number is not None and media.episode_number is not None:
                embed.add_field(
                    name="Episode",
                    value=f"S{media.season_number:02d}E{media.episode_number:02d}",
                    inline=True,
                )

        if media.overview:
            synopsis = media.overview if len(media.overview) <= 300 else media.overview[:297] + "…"
            embed.add_field(name="Synopsis", value=synopsis, inline=False)

        embed.add_field(name="Size", value=media.human_size, inline=True)

        if media.poster_url:
            embed.set_thumbnail(url=media.poster_url)

        embed.set_footer(text="Try again for another pick!")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="genre", description="Browse media by genre")
    @app_commands.describe(
        genre="Genre to browse (leave blank to see available genres)",
        type="Filter by media type (default: Movie)",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="Movie", value="movie"),
            app_commands.Choice(name="TV Show", value="tv_show"),
        ]
    )
    async def genre(
        self,
        interaction: discord.Interaction,
        genre: Optional[str] = None,
        type: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        media_type = type.value if type else "movie"

        if not genre:
            genres = await self.bot.db.get_genres(media_type=media_type)
            if not genres:
                await interaction.response.send_message(
                    "No genres found in the library.", ephemeral=True
                )
                return
            embed = discord.Embed(
                title="🎭 Available Genres",
                description=", ".join(genres),
                color=discord.Color.teal(),
            )
            embed.set_footer(text="Use /genre <name> to browse a genre")
            await interaction.response.send_message(embed=embed)
            return

        results = await self.bot.db.search_by_genre(
            genre, media_type=media_type, limit=25
        )

        if not results:
            await interaction.response.send_message(
                f"No media found with genre '{genre}'", ephemeral=True
            )
            return

        # Group: show unique movies and unique show titles
        seen_titles: set[str] = set()
        lines: list[str] = []
        for item in results:
            title = item.title or item.filename
            if title in seen_titles:
                continue
            seen_titles.add(title)
            year_str = f" ({item.year})" if item.year else ""
            type_icon = "🎬" if item.media_type == MediaType.MOVIE else "📺"
            lines.append(f"{type_icon} {title}{year_str}")

        embed = discord.Embed(
            title=f"🎭 {genre}",
            description="\n".join(lines),
            color=discord.Color.teal(),
        )
        embed.set_footer(text=f"{len(lines)} title(s)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="duplicates", description="Find duplicate movies in your library")
    async def duplicates(self, interaction: discord.Interaction) -> None:
        dupes = await self.bot.db.find_duplicates(limit=25)

        if not dupes:
            await interaction.response.send_message(
                "✅ No duplicate movies found!", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚠️ Duplicate Movies",
            color=discord.Color.orange(),
        )

        for d in dupes:
            paths = d["paths"].split("||")
            path_list = "\n".join(f"  `{p}`" for p in paths)
            embed.add_field(
                name=f"{d['title']} ({d['count']} copies — {_human_bytes(d['total_size'])})",
                value=path_list,
                inline=False,
            )

        embed.set_footer(text=f"{len(dupes)} duplicate title(s) found")
        await interaction.response.send_message(embed=embed)



def _human_bytes(size: int) -> str:
    """Format a byte count into a human-readable string."""
    if size == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = int(math.log(size, 1024))
    i = min(i, len(units) - 1)
    value = size / (1024**i)
    return f"{value:.2f} {units[i]}"


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(MediaCog(bot))
