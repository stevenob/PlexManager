"""Movie organizer — identify and rename movies using TMDb metadata."""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.organizer import is_well_named, propose_rename, execute_rename, RenameProposal

if TYPE_CHECKING:
    from bot.client import PlexManagerBot

logger = logging.getLogger(__name__)

COLOR_ORGANIZE = 0x1ABC9C


class AcceptRenameButton(discord.ui.Button):
    """Button to accept a rename proposal."""

    def __init__(self, proposal: RenameProposal) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="✅ Accept",
            custom_id=f"org_accept:{hash(proposal.current_path) % 100000}",
        )
        self.proposal = proposal

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        success = execute_rename(self.proposal)
        if success:
            await bot.db.update_media_path(
                self.proposal.current_path, self.proposal.proposed_path
            )
            # Enrich with TMDb metadata if missing
            if self.proposal.tmdb_id:
                media = await bot.db.get_media(self.proposal.proposed_path)
                if media and not media.tmdb_id:
                    try:
                        media.tmdb_id = self.proposal.tmdb_id
                        media.title = self.proposal.tmdb_title
                        media.year = self.proposal.tmdb_year
                        media.rating = self.proposal.tmdb_rating
                        media.poster_url = self.proposal.tmdb_poster
                        media = await bot.tmdb.enrich_media(media)
                        await bot.db.add_media(media)
                    except Exception:
                        pass  # Enrichment is best-effort
            await interaction.response.send_message(
                f"✅ Renamed to **{self.proposal.tmdb_title} ({self.proposal.tmdb_year})**",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Rename failed — check bot logs.", ephemeral=True
            )
        # Disable buttons
        if self.view:
            for item in self.view.children:
                item.disabled = True
            await interaction.message.edit(view=self.view)


class DeclineRenameButton(discord.ui.Button):
    """Button to decline a rename proposal."""

    def __init__(self, path: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="❌ Decline",
            custom_id=f"org_decline:{hash(path) % 100000}",
        )
        self.path = path

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await bot.db.set_organize_status(self.path, "skipped")
        await interaction.response.send_message(
            "⏭️ Skipped — won't be proposed again.", ephemeral=True
        )
        if self.view:
            for item in self.view.children:
                item.disabled = True
            await interaction.message.edit(view=self.view)


class RenameView(discord.ui.View):
    """View with Accept/Decline buttons for a rename proposal."""

    def __init__(self, proposal: RenameProposal) -> None:
        super().__init__(timeout=None)
        self.add_item(AcceptRenameButton(proposal))
        self.add_item(DeclineRenameButton(proposal.current_path))


class OrganizerCog(commands.Cog):
    """Movie organizer — identify and rename using TMDb."""

    def __init__(self, bot: PlexManagerBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="organize",
        description="Scan for misnamed movies and show count",
    )
    async def organize_scan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        movies = await self.bot.db.get_misnamed_movies(limit=1000)
        misnamed = []
        already_good = []

        for movie in movies:
            if is_well_named(movie.path):
                await self.bot.db.set_organize_status(movie.path, "organized")
                already_good.append(movie)
            else:
                misnamed.append(movie)

        stats = await self.bot.db.get_organize_stats()

        embed = discord.Embed(
            title="📂 Movie Organization Scan",
            color=COLOR_ORGANIZE,
        )
        embed.add_field(name="⚠️ Need Renaming", value=str(len(misnamed)), inline=True)
        embed.add_field(name="✅ Already Correct", value=str(len(already_good)), inline=True)
        embed.add_field(name="⏭️ Skipped", value=str(stats.get("skipped", 0)), inline=True)
        embed.set_footer(text="Use /renameall to start renaming with TMDb lookup")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="renameall",
        description="Start organizing movies — sends proposals with Accept/Decline buttons",
    )
    @app_commands.describe(limit="Max number of proposals to send (default 10)")
    async def organize_run(
        self, interaction: discord.Interaction, limit: Optional[int] = None
    ) -> None:
        await interaction.response.defer()

        max_proposals = min(limit or 10, 25)  # Cap to avoid spam
        movies = await self.bot.db.get_misnamed_movies(limit=200)

        # Filter to only misnamed ones
        misnamed = [m for m in movies if not is_well_named(m.path)]

        if not misnamed:
            await interaction.followup.send(
                "✅ All movies are properly organized!", ephemeral=True
            )
            return

        sent = 0
        for movie in misnamed:
            if sent >= max_proposals:
                break

            proposal = await propose_rename(
                movie.path, self.bot.tmdb, self.bot.media_paths
            )

            if proposal is None:
                await self.bot.db.set_organize_status(movie.path, "needs_review")
                continue

            if proposal.already_correct:
                await self.bot.db.set_organize_status(movie.path, "organized")
                continue

            # Build proposal embed
            year_str = f" ({proposal.tmdb_year})" if proposal.tmdb_year else ""
            rating_str = f"⭐ {proposal.tmdb_rating:.1f}" if proposal.tmdb_rating else ""
            confidence_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
                proposal.confidence, "⚪"
            )

            edition_str = f" — {proposal.edition}" if proposal.edition else ""

            embed = discord.Embed(
                title="📂 Rename Proposal",
                description=f"**{proposal.tmdb_title}{edition_str}{year_str}** {rating_str}",
                color=COLOR_ORGANIZE,
            )

            if proposal.tmdb_poster:
                embed.set_thumbnail(url=proposal.tmdb_poster)

            embed.add_field(
                name="📁 Current",
                value=f"`{proposal.current_filename}`",
                inline=False,
            )
            embed.add_field(
                name="📂 Proposed",
                value=f"`{proposal.proposed_filename}`",
                inline=False,
            )
            embed.set_footer(text=f"Confidence: {confidence_icon} {proposal.confidence}")

            view = RenameView(proposal)
            await interaction.followup.send(embed=embed, view=view)
            sent += 1

        remaining = len(misnamed) - sent
        if remaining > 0:
            await interaction.followup.send(
                f"📋 Showing {sent} of {len(misnamed)} proposals. "
                "Run `/renameall` again for more."
            )

    @app_commands.command(
        name="renamecheck",
        description="Check a specific movie's proposed rename",
    )
    @app_commands.describe(title="Title of the movie to check")
    async def organize_check(
        self, interaction: discord.Interaction, title: str
    ) -> None:
        await interaction.response.defer()

        results = await self.bot.db.search(title, limit=5)
        movie = None
        for m in results:
            if m.title and m.title.lower() == title.lower():
                movie = m
                break
        if movie is None and results:
            movie = results[0]

        if movie is None:
            await interaction.followup.send(
                f"No movie found matching '{title}'", ephemeral=True
            )
            return

        if is_well_named(movie.path):
            await interaction.followup.send(
                f"✅ **{movie.title}** is already properly named.", ephemeral=True
            )
            return

        proposal = await propose_rename(
            movie.path, self.bot.tmdb, self.bot.media_paths
        )

        if proposal is None:
            await interaction.followup.send(
                f"❌ Couldn't find **{movie.title}** on TMDb.", ephemeral=True
            )
            return

        year_str = f" ({proposal.tmdb_year})" if proposal.tmdb_year else ""
        rating_str = f"⭐ {proposal.tmdb_rating:.1f}" if proposal.tmdb_rating else ""
        confidence_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
            proposal.confidence, "⚪"
        )

        edition_str = f" — {proposal.edition}" if proposal.edition else ""

        embed = discord.Embed(
            title="📂 Rename Proposal",
            description=f"**{proposal.tmdb_title}{edition_str}{year_str}** {rating_str}",
            color=COLOR_ORGANIZE,
        )

        if proposal.tmdb_poster:
            embed.set_thumbnail(url=proposal.tmdb_poster)

        embed.add_field(name="📁 Current", value=f"`{proposal.current_filename}`", inline=False)
        embed.add_field(name="📂 Proposed", value=f"`{proposal.proposed_filename}`", inline=False)
        embed.set_footer(text=f"Confidence: {confidence_icon} {proposal.confidence}")

        view = RenameView(proposal)
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: PlexManagerBot) -> None:
    await bot.add_cog(OrganizerCog(bot))
