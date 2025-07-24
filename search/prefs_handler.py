import discord
from discord import app_commands
import datetime

from sqlalchemy.orm import sessionmaker
from shared.discord_utils import safe_defer
from .repository import SearchRepository

class SearchPreferencesHandler:
    """处理用户搜索偏好设置的业务逻辑"""

    def __init__(self, bot, session_factory: sessionmaker):
        self.bot = bot
        self.session_factory = session_factory

    async def search_preferences_author(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User = None
    ):
        await safe_defer(interaction)
        try:
            user_id = interaction.user.id
            if action.value in ["include", "exclude", "unblock"] and not user:
                await self.bot.api_scheduler.submit(
                    coro=interaction.followup.send("❌ 请指定要设置的用户。", ephemeral=True),
                    priority=1
                )
                return

            async with self.session_factory() as session:
                repo = SearchRepository(session)
                prefs = await repo.get_user_preferences(user_id)
                
                if not prefs:
                    prefs_data = {'include_authors': [], 'exclude_authors': []}
                else:
                    prefs_data = {'include_authors': prefs.include_authors or [], 'exclude_authors': prefs.exclude_authors or []}

                include_authors = set(prefs_data['include_authors'])
                exclude_authors = set(prefs_data['exclude_authors'])

                if action.value == "include":
                    include_authors.add(user.id)
                    exclude_authors.discard(user.id)
                    message = f"✅ 已将 {user.mention} 添加到只看作者列表。"
                elif action.value == "exclude":
                    exclude_authors.add(user.id)
                    include_authors.discard(user.id)
                    message = f"✅ 已将 {user.mention} 添加到屏蔽作者列表。"
                elif action.value == "unblock":
                    if user.id in exclude_authors:
                        exclude_authors.remove(user.id)
                        message = f"✅ 已将 {user.mention} 从屏蔽列表中移除。"
                    else:
                        message = f"ℹ️ {user.mention} 不在屏蔽列表中。"
                elif action.value == "clear":
                    include_authors.clear()
                    exclude_authors.clear()
                    message = "✅ 已清空所有作者偏好设置。"
                
                await repo.save_user_preferences(user_id, {'include_authors': list(include_authors), 'exclude_authors': list(exclude_authors)})
            
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(message, ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败: {e}", ephemeral=True),
                priority=1
            )

    async def search_preferences_time(
        self,
        interaction: discord.Interaction,
        after_date: str = None,
        before_date: str = None
    ):
        await safe_defer(interaction)
        try:
            user_id = interaction.user.id
            update_data = {}
            if after_date:
                update_data['after_date'] = datetime.datetime.strptime(after_date, "%Y-%m-%d")
            if before_date:
                update_data['before_date'] = datetime.datetime.strptime(before_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            
            if not after_date and not before_date:
                update_data = {'after_date': None, 'before_date': None}
                message = "✅ 已清空时间范围设置。"
            else:
                message = "✅ 已成功设置时间范围。"

            async with self.session_factory() as session:
                repo = SearchRepository(session)
                await repo.save_user_preferences(user_id, update_data)
            
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(message, ephemeral=True),
                priority=1
            )
        except ValueError:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("❌ 日期格式错误，请使用 YYYY-MM-DD 格式。", ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败：{e}", ephemeral=True),
                priority=1
            )

    async def search_preferences_tag(
        self,
        interaction: discord.Interaction,
        logic: app_commands.Choice[str]
    ):
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                await repo.save_user_preferences(interaction.user.id, {'tag_logic': logic.value})

            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(
                    f"✅ 已设置多选标签逻辑为：**{logic.name}**\n"
                    f"• 同时：必须包含所有选择的标签\n"
                    f"• 任一：只需包含任意一个选择的标签",
                    ephemeral=True
                ),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败: {e}", ephemeral=True),
                priority=1
            )

    async def search_preferences_preview(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str]
    ):
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                await repo.save_user_preferences(interaction.user.id, {'preview_image_mode': mode.value})

            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(
                    f"✅ 已设置预览图显示方式为：**{mode.name}**\n"
                    f"• 缩略图：在搜索结果右侧显示小图\n"
                    f"• 大图：在搜索结果下方显示大图",
                    ephemeral=True
                ),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败：{e}", ephemeral=True),
                priority=1
            )

    async def search_preferences_view(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                prefs = await repo.get_user_preferences(interaction.user.id)
            
                embed = discord.Embed(title="🔍 当前搜索偏好设置", color=0x3498db)

                if not prefs:
                    embed.description = "您还没有任何偏好设置。"
                else:
                    # 作者偏好
                    author_info = []
                    if prefs.include_authors:
                        authors = [f"<@{uid}>" for uid in prefs.include_authors]
                        author_info.append(f"**只看作者：** {', '.join(authors)}")
                    if prefs.exclude_authors:
                        authors = [f"<@{uid}>" for uid in prefs.exclude_authors]
                        author_info.append(f"**屏蔽作者：** {', '.join(authors)}")
                    embed.add_field(name="作者设置",
                                     value="\n".join(author_info) if author_info else "无限制",
                                     inline=False)

                    # 时间偏好
                    time_info = []
                    if prefs.after_date:
                        time_info.append(f"**开始时间：** {prefs.after_date.strftime('%Y-%m-%d')}")
                    if prefs.before_date:
                        time_info.append(f"**结束时间：** {prefs.before_date.strftime('%Y-%m-%d')}")
                    embed.add_field(name="时间设置", value="\n".join(time_info) if time_info else "**时间范围：** 无限制", inline=False)

                    # 标签逻辑设置
                    tag_logic_display = "同时" if prefs.tag_logic == "and" else "任一"
                    embed.add_field(
                        name="标签逻辑",
                        value=f"**多选标签逻辑：** {tag_logic_display}\n"
                              f"• 同时：必须包含所有选择的标签\n"
                              f"• 任一：只需包含任意一个选择的标签",
                        inline=False
                    )

                    # 预览图设置
                    preview_display = "缩略图（右侧小图）" if prefs.preview_image_mode == "thumbnail" else "大图（下方大图）"
                    embed.add_field(
                        name="预览图设置",
                        value=f"**预览图显示方式：** {preview_display}\n"
                              f"• 缩略图：在搜索结果右侧显示小图\n"
                              f"• 大图：在搜索结果下方显示大图",
                        inline=False
                    )
                    embed.add_field(name="显示设置", value=f"每页结果数量：**{prefs.results_per_page}**", inline=False)

                embed.set_footer(text="使用 /搜索偏好 子命令来修改这些设置")
            
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(embed=embed, ephemeral=True),
                priority=1
            )
                    
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败：{e}", ephemeral=True),
                priority=1
            )

    async def search_preferences_clear(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                await repo.save_user_preferences(interaction.user.id, {
                    'include_authors': [], 'exclude_authors': [],
                    'after_date': None, 'before_date': None,
                    'tag_logic': 'and', 'preview_image_mode': 'thumbnail', 'results_per_page': 5
                })

            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("✅ 已清空所有搜索偏好设置。", ephemeral=True),
                priority=1
            )
        
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 操作失败：{e}", ephemeral=True),
                priority=1
            )