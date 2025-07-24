import discord
import re
from typing import TYPE_CHECKING

from shared.discord_utils import safe_defer
from .generic_search_view import GenericSearchView

if TYPE_CHECKING:
    from ..cog import Search


class PersistentChannelSearchView(discord.ui.View):
    """
    一个持久化的视图，包含一个按钮，用于在特定频道启动搜索。
    """
    def __init__(self, cog: "Search"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🔍 搜索本频道", style=discord.ButtonStyle.primary, custom_id="persistent_channel_search_v2")
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        当用户点击“搜索本频道”按钮时，启动一个预设了频道ID的通用搜索流程。
        它从按钮所在消息的 embed 中解析出频道 ID。
        """
        await safe_defer(interaction, ephemeral=True)

        if not interaction.message.embeds:
            await self.cog.bot.api_scheduler.submit(
                coro=interaction.followup.send("❌ 搜索按钮配置错误：找不到关联的 embed 信息。", ephemeral=True),
                priority=1
            )
            return

        embed = interaction.message.embeds[0]
        match = re.search(r'<#(\d+)>', embed.description or "")

        if not match:
            await self.cog.bot.api_scheduler.submit(
                coro=interaction.followup.send("❌ 搜索按钮配置错误：无法从消息中解析频道ID。", ephemeral=True),
                priority=1
            )
            return

        channel_id = int(match.group(1))

        # 确保频道存在且是论坛
        channel = interaction.guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.ForumChannel):
            await self.cog.bot.api_scheduler.submit(
                coro=interaction.followup.send("❌ 目标频道不存在或已不是论坛频道。", ephemeral=True),
                priority=1
            )
            return

        # 创建并启动通用搜索视图，预设了频道ID
        generic_view = GenericSearchView(
            cog=self.cog,
            original_interaction=interaction,
            channel_ids=[channel_id]
        )
        await generic_view.start()