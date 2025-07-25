import discord
from discord import app_commands
from discord.ext import commands
import datetime
import logging

from ranking_config import RankingConfig
from shared.discord_utils import safe_defer
from .models.dto.tag import TagDTO
from .views.author_search_view import NewAuthorTagSelectionView
from .views.global_search_view import GlobalSearchView
from sqlalchemy.orm import sessionmaker
from .repository import SearchRepository
from tag_system.repository import TagSystemRepository
from shared.models.thread import Thread as ThreadModel
from search.models.qo.thread_search import ThreadSearchQuery
from .views.channel_selection_view import ChannelSelectionView
from .views.author_search_view import NewAuthorTagSelectionView
from .views.global_search_view import GlobalSearchView
from .views.persistent_channel_search_view import PersistentChannelSearchView
from .prefs_handler import SearchPreferencesHandler

# 获取一个模块级别的 logger
logger = logging.getLogger(__name__)

class Search(commands.Cog):
    """搜索相关命令"""


    def __init__(self, bot: commands.Bot, session_factory: sessionmaker):
        self.bot = bot
        self.session_factory = session_factory
        self.tag_system_repo = TagSystemRepository
        self.prefs_handler = SearchPreferencesHandler(bot, session_factory)
        self.channel_tags_cache = {}  # 缓存频道tags
        self.global_search_view = GlobalSearchView(self)
        self.persistent_channel_search_view = PersistentChannelSearchView(self)
        self._has_cached_tags = False # 用于确保 on_ready 只执行一次缓存

    @commands.Cog.listener()
    async def on_ready(self):
        """当机器人准备就绪时，执行一次性的缓存任务"""
        if not self._has_cached_tags:
            logger.info("机器人已准备就绪，开始缓存频道标签...")
            await self.cache_channel_tags()
            self._has_cached_tags = True

    @commands.Cog.listener()
    async def on_index_updated(self):
        """监听由 Indexer 发出的索引更新事件。"""
        logger.info("接收到 'index_updated' 事件，正在刷新搜索模块的标签缓存...")
        await self.cache_channel_tags()

    async def cog_load(self):
        """在Cog加载时注册持久化View"""
        # 注册持久化view，使其在bot重启后仍能响应
        self.bot.add_view(self.global_search_view)
        self.bot.add_view(self.persistent_channel_search_view)

    async def cache_channel_tags(self):
        """缓存所有已索引频道的tags"""
        try:
            # 获取已索引的频道ID
            async with self.session_factory() as session:
                repo = self.tag_system_repo(session)
                indexed_channel_ids_list = await repo.get_indexed_channel_ids()
                indexed_channel_ids = set(indexed_channel_ids_list)

            self.channel_tags_cache = {}

            # 启动时尽力缓存，但不强制要求成功
            for guild in self.bot.guilds:
                for channel in guild.channels:
                    if isinstance(channel, discord.ForumChannel) and channel.id in indexed_channel_ids:
                        self.channel_tags_cache[channel.id] = {tag.name: tag.id for tag in channel.available_tags}
                        
            logger.info(f"已缓存 {len(self.channel_tags_cache)} 个频道的tags")
            
        except Exception as e:
            logger.warning(f"缓存频道tags时出错: {e}", exc_info=True)

    def get_merged_tags(self, channel_ids: list[int]) -> list[TagDTO]:
        """
        获取多个频道的合并tags，重名tag会被合并显示。
        返回一个 TagDTO 对象列表
        """
        all_tags_names = set()
        
        for channel_id in channel_ids:
            channel_tags = self.channel_tags_cache.get(channel_id, {})
            all_tags_names.update(channel_tags.keys())
        
        # 返回 TagDTO 对象列表，确保后续代码可以安全地访问 .id 和 .name
        return [TagDTO(id=0, name=tag_name) for tag_name in sorted(all_tags_names)]

    # ----- 用户偏好设置 -----
    @app_commands.command(name="每页结果数量", description="设置每页展示的搜索结果数量（3-10）")
    @app_commands.describe(num="要设置的数量 (3-10)")
    async def set_page_size(self, interaction: discord.Interaction, num: app_commands.Range[int, 3, 10]):
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                await repo.save_user_preferences(interaction.user.id, {'results_per_page': num})
            
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"已将每页结果数量设置为 {num}。", ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 设置失败: {e}", ephemeral=True),
                priority=1
            )


    # ----- 搜索偏好设置 -----
    search_prefs = app_commands.Group(name="搜索偏好", description="管理搜索偏好设置")
    @search_prefs.command(name="作者", description="管理作者偏好设置")
    @app_commands.describe(action="操作类型", user="要设置的用户（@用户 或 用户ID）")
    @app_commands.choices(action=[
        app_commands.Choice(name="只看作者", value="include"),
        app_commands.Choice(name="屏蔽作者", value="exclude"),
        app_commands.Choice(name="取消屏蔽", value="unblock"),
        app_commands.Choice(name="清空作者偏好", value="clear")
    ])
    async def search_preferences_author(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User = None
    ):
        await self.prefs_handler.search_preferences_author(interaction, action, user)

    @search_prefs.command(name="时间", description="设置搜索时间范围偏好")
    @app_commands.describe(after_date="开始日期 (YYYY-MM-DD)", before_date="结束日期 (YYYY-MM-DD)")
    async def search_preferences_time(
        self,
        interaction: discord.Interaction,
        after_date: str = None,
        before_date: str = None
    ):
        await self.prefs_handler.search_preferences_time(interaction, after_date, before_date)

    @search_prefs.command(name="标签", description="设置多选标签逻辑偏好")
    @app_commands.choices(logic=[
        app_commands.Choice(name="同时（必须包含所有选择的标签）", value="and"),
        app_commands.Choice(name="任一（只需包含任意一个选择的标签）", value="or")
    ])
    async def search_preferences_tag(
        self,
        interaction: discord.Interaction,
        logic: app_commands.Choice[str]
    ):
        await self.prefs_handler.search_preferences_tag(interaction, logic)

    @search_prefs.command(name="预览图", description="设置搜索结果预览图显示方式")
    @app_commands.describe(
        mode="预览图显示方式"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="缩略图（右侧小图）", value="thumbnail"),
        app_commands.Choice(name="大图（下方大图）", value="image")
    ])
    async def search_preferences_preview(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str]
    ):
        await self.prefs_handler.search_preferences_preview(interaction, mode)

    @search_prefs.command(name="查看", description="查看当前搜索偏好设置")
    async def search_preferences_view(self, interaction: discord.Interaction):
        await self.prefs_handler.search_preferences_view(interaction)

    @search_prefs.command(name="清空", description="清空所有搜索偏好设置")
    async def search_preferences_clear(self, interaction: discord.Interaction):
        await self.prefs_handler.search_preferences_clear(interaction)

    # ----- 排序算法管理 -----
    @app_commands.command(name="排序算法配置", description="管理员设置搜索排序算法参数")
    @app_commands.describe(
        preset="预设配置方案",
        time_weight="时间权重因子 (0.0-1.0)",
        tag_weight="标签权重因子 (0.0-1.0)",
        reaction_weight="反应权重因子 (0.0-1.0)",
        time_decay="时间衰减率 (0.01-0.5)",
        reaction_log_base="反应数对数基数 (10-200)",
        severe_penalty="严重惩罚阈值 (0.0-1.0)",
        mild_penalty="轻度惩罚阈值 (0.0-1.0)"
    )
    @app_commands.choices(preset=[
        app_commands.Choice(name="平衡配置 (默认)", value="balanced"),
        app_commands.Choice(name="偏重时间新鲜度", value="time_focused"),
        app_commands.Choice(name="偏重内容质量", value="quality_focused"),
        app_commands.Choice(name="偏重受欢迎程度", value="popularity_focused"),
        app_commands.Choice(name="严格质量控制", value="strict_quality")
    ])
    async def configure_ranking(
        self, 
        interaction: discord.Interaction,
        preset: app_commands.Choice[str] = None,
        time_weight: float = None,
        tag_weight: float = None,
        reaction_weight: float = None,
        time_decay: float = None,
        reaction_log_base: int = None,
        severe_penalty: float = None,
        mild_penalty: float = None
    ):
        # 检查权限 (需要管理员权限)
        await safe_defer(interaction)
        if not interaction.user.guild_permissions.administrator:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("此命令需要管理员权限。", ephemeral=True),
                priority=1
            )
            return

        try:
            # 应用预设配置
            if preset:
                from ranking_config import PresetConfigs
                if preset.value == "balanced":
                    PresetConfigs.balanced()
                elif preset.value == "time_focused":
                    PresetConfigs.time_focused()
                elif preset.value == "quality_focused":
                    PresetConfigs.quality_focused()
                elif preset.value == "popularity_focused":
                    PresetConfigs.popularity_focused()
                elif preset.value == "strict_quality":
                    PresetConfigs.strict_quality()
                
                config_name = preset.name
            else:
                # 手动配置参数
                if time_weight is not None:
                    if 0 <= time_weight <= 1:
                        RankingConfig.TIME_WEIGHT_FACTOR = time_weight
                    else:
                        raise ValueError("时间权重必须在0-1之间")
                
                if tag_weight is not None:
                    if 0 <= tag_weight <= 1:
                        RankingConfig.TAG_WEIGHT_FACTOR = tag_weight
                    else:
                        raise ValueError("标签权重必须在0-1之间")
                
                if reaction_weight is not None:
                    if 0 <= reaction_weight <= 1:
                        RankingConfig.REACTION_WEIGHT_FACTOR = reaction_weight
                    else:
                        raise ValueError("反应权重必须在0-1之间")
                
                # 确保权重和为1 (三个权重)
                if time_weight is not None or tag_weight is not None or reaction_weight is not None:
                    # 计算当前权重总和
                    current_total = RankingConfig.TIME_WEIGHT_FACTOR + RankingConfig.TAG_WEIGHT_FACTOR + RankingConfig.REACTION_WEIGHT_FACTOR
                    
                    # 如果权重和不为1，按比例重新分配
                    if abs(current_total - 1.0) > 0.001:
                        RankingConfig.TIME_WEIGHT_FACTOR = RankingConfig.TIME_WEIGHT_FACTOR / current_total
                        RankingConfig.TAG_WEIGHT_FACTOR = RankingConfig.TAG_WEIGHT_FACTOR / current_total
                        RankingConfig.REACTION_WEIGHT_FACTOR = RankingConfig.REACTION_WEIGHT_FACTOR / current_total
                
                if time_decay is not None:
                    if 0.01 <= time_decay <= 0.5:
                        RankingConfig.TIME_DECAY_RATE = time_decay
                    else:
                        raise ValueError("时间衰减率必须在0.01-0.5之间")
                
                if reaction_log_base is not None:
                    if 10 <= reaction_log_base <= 200:
                        RankingConfig.REACTION_LOG_BASE = reaction_log_base
                    else:
                        raise ValueError("反应数对数基数必须在10-200之间")
                
                if severe_penalty is not None:
                    if 0 <= severe_penalty <= 1:
                        RankingConfig.SEVERE_PENALTY_THRESHOLD = severe_penalty
                    else:
                        raise ValueError("严重惩罚阈值必须在0-1之间")
                
                if mild_penalty is not None:
                    if 0 <= mild_penalty <= 1:
                        RankingConfig.MILD_PENALTY_THRESHOLD = mild_penalty
                    else:
                        raise ValueError("轻度惩罚阈值必须在0-1之间")
                
                config_name = "自定义配置"
            
            # 验证配置
            RankingConfig.validate()
            
            # 构建响应消息
            embed = discord.Embed(
                title="✅ 排序算法配置已更新",
                description=f"当前配置：**{config_name}**",
                color=0x00ff00
            )
            
            embed.add_field(
                name="权重配置",
                value=f"• 时间权重：**{RankingConfig.TIME_WEIGHT_FACTOR:.1%}**\n"
                      f"• 标签权重：**{RankingConfig.TAG_WEIGHT_FACTOR:.1%}**\n"
                      f"• 反应权重：**{RankingConfig.REACTION_WEIGHT_FACTOR:.1%}**\n"
                      f"• 时间衰减率：**{RankingConfig.TIME_DECAY_RATE}**\n"
                      f"• 反应对数基数：**{RankingConfig.REACTION_LOG_BASE}**",
                inline=True
            )
            
            embed.add_field(
                name="惩罚机制",
                value=f"• 严重惩罚阈值：**{RankingConfig.SEVERE_PENALTY_THRESHOLD}**\n"
                      f"• 轻度惩罚阈值：**{RankingConfig.MILD_PENALTY_THRESHOLD}**\n"
                      f"• 严重惩罚系数：**{RankingConfig.SEVERE_PENALTY_FACTOR}**",
                inline=True
            )
            
            # 添加算法说明
            embed.add_field(
                name="算法说明",
                value="新的排序算法将立即生效，影响所有后续搜索结果。\n"
                      "时间权重基于指数衰减，标签权重基于Wilson Score算法。",
                inline=False
            )
            
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(embed=embed, ephemeral=True),
                priority=1
            )
            
        except ValueError as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 配置错误：{e}", ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 配置失败：{e}", ephemeral=True),
                priority=1
            )

    @app_commands.command(name="查看排序配置", description="查看当前搜索排序算法配置")
    async def view_ranking_config(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        embed = discord.Embed(
            title="🔧 当前排序算法配置",
            description="智能混合权重排序算法参数",
            color=0x3498db
        )
        
        embed.add_field(
            name="权重配置",
            value=f"• 时间权重：**{RankingConfig.TIME_WEIGHT_FACTOR:.1%}**\n"
                  f"• 标签权重：**{RankingConfig.TAG_WEIGHT_FACTOR:.1%}**\n"
                  f"• 反应权重：**{RankingConfig.REACTION_WEIGHT_FACTOR:.1%}**\n"
                  f"• 时间衰减率：**{RankingConfig.TIME_DECAY_RATE}**\n"
                  f"• 反应对数基数：**{RankingConfig.REACTION_LOG_BASE}**",
            inline=True
        )
        
        embed.add_field(
            name="惩罚机制",
            value=f"• 严重惩罚阈值：**{RankingConfig.SEVERE_PENALTY_THRESHOLD}**\n"
                  f"• 轻度惩罚阈值：**{RankingConfig.MILD_PENALTY_THRESHOLD}**\n"
                  f"• 严重惩罚系数：**{RankingConfig.SEVERE_PENALTY_FACTOR:.1%}**\n"
                  f"• 轻度惩罚系数：**{RankingConfig.MILD_PENALTY_FACTOR:.1%}**",
            inline=True
        )
        
        embed.add_field(
            name="算法特性",
            value="• **Wilson Score**：置信度评估标签质量\n"
                  "• **指数衰减**：时间新鲜度自然衰减\n"
                  "• **智能惩罚**：差评内容自动降权\n"
                  "• **可配置权重**：灵活调整排序偏好",
            inline=False
        )
        
        embed.set_footer(text="管理员可使用 /排序算法配置 命令调整参数")
        
        await self.bot.api_scheduler.submit(
            coro=interaction.followup.send(embed=embed, ephemeral=True),
            priority=1
        )

    @app_commands.command(name="创建频道搜索", description="在当前帖子内创建频道搜索按钮")
    @app_commands.guild_only()
    async def create_channel_search(self, interaction: discord.Interaction):
        """在一个帖子内创建一个持久化的搜索按钮，该按钮将启动一个仅限于该频道的搜索流程。"""
        await safe_defer(interaction)
        try:
            if not isinstance(interaction.channel, discord.Thread):
                await self.bot.api_scheduler.submit(
                    coro=interaction.followup.send("请在帖子内使用此命令。", ephemeral=True),
                    priority=1
                )
                return

            channel_id = interaction.channel.parent_id

            # 创建美观的embed
            embed = discord.Embed(
                title=f"🔍 {interaction.channel.parent.name} 频道搜索",
                description=f"点击下方按钮，搜索 <#{channel_id}> 频道内的所有帖子",
                color=0x3498db
            )
            embed.add_field(
                name="使用方法",
                value="根据标签、作者、关键词等条件进行搜索。",
                inline=False
            )

            # 发送带有持久化视图的消息
            await self.bot.api_scheduler.submit(
                coro=interaction.channel.send(embed=embed, view=self.persistent_channel_search_view),
                priority=1
            )
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("✅ 已成功创建频道内搜索按钮。", ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 创建失败: {e}", ephemeral=True),
                priority=1
            )

    @app_commands.command(name="创建全局搜索", description="在当前频道创建全局搜索按钮")
    async def create_global_search(self, interaction: discord.Interaction):
        """在当前频道创建一个持久化的全局搜索按钮。"""
        await safe_defer(interaction)
        try:
            embed = discord.Embed(
                title="🌐 全局搜索",
                description="搜索服务器内所有论坛频道的帖子",
                color=0x2ecc71
            )
            embed.add_field(
                name="使用方法",
                value="1. 点击下方按钮选择要搜索的论坛频道\n2. 设置搜索条件（标签、关键词等）\n3. 查看搜索结果",
                inline=False
            )
            view = GlobalSearchView(self)
            await self.bot.api_scheduler.submit(
                coro=interaction.channel.send(embed=embed, view=view),
                priority=1
            )
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("✅ 已创建全局搜索按钮。", ephemeral=True),
                priority=1
            )
        except Exception as e:
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 创建失败: {e}", ephemeral=True),
                priority=1
            )

    @app_commands.command(name="全局搜索", description="开始一次仅自己可见的全局搜索")
    async def global_search(self, interaction: discord.Interaction):
        """直接触发全局搜索流程"""
        await self.start_global_search_flow(interaction)

    async def start_global_search_flow(self, interaction: discord.Interaction):
        """启动全局搜索流程的通用逻辑。"""
        await safe_defer(interaction)
        try:
            async with self.session_factory() as session:
                repo = self.tag_system_repo(session)
                indexed_channel_ids_list = await repo.get_indexed_channel_ids()
                indexed_channel_ids = set(indexed_channel_ids_list)
            
            logger.debug(f"从数据库找到 {len(indexed_channel_ids)} 个已索引频道ID: {indexed_channel_ids}")

            if not indexed_channel_ids:
                await interaction.followup.send("没有已索引的频道可供搜索。", ephemeral=True)
                return

            channels = []
            for ch_id in indexed_channel_ids:
                logger.debug(f"正在处理频道ID: {ch_id}")
                channel = self.bot.get_channel(ch_id)
                logger.debug(f"  - get_channel (缓存) 结果: {'找到' if channel else '未找到'}")
                
                if channel is None:
                    logger.debug(f"  - 缓存未命中，尝试 fetch_channel...")
                    try:
                        channel = await self.bot.fetch_channel(ch_id)
                        logger.debug(f"  - fetch_channel (API) 结果: {'找到' if channel else '未找到'}")
                    except discord.NotFound:
                        logger.warning(f"  - fetch_channel 失败 (ID: {ch_id}): 未找到 (NotFound)。")
                        continue
                    except discord.Forbidden:
                        logger.warning(f"  - fetch_channel 失败 (ID: {ch_id}): 无权限 (Forbidden)。")
                        continue
                    except Exception:
                        logger.error(f"  - fetch_channel 失败 (ID: {ch_id})，出现意外错误。", exc_info=True)
                        continue
                
                if isinstance(channel, discord.ForumChannel):
                    logger.debug(f"  - 成功！频道 '{channel.name}' 是一个论坛频道，已添加到列表。")
                    channels.append(channel)
                    if ch_id not in self.channel_tags_cache:
                        self.channel_tags_cache[ch_id] = {tag.name: tag.id for tag in channel.available_tags}
                else:
                    if channel:
                        logger.warning(f"  - 失败。ID为 {ch_id} 的频道不是论坛频道 (类型: {type(channel)})。")
                    else:
                        logger.warning(f"  - 失败。在所有尝试后，ID为 {ch_id} 的频道对象仍为空。")

            logger.debug(f"最终频道对象列表大小: {len(channels)}")
            logger.debug(f"最终频道名称列表: {[ch.name for ch in channels]}")

            if not channels:
                await interaction.followup.send("❌ 未找到任何可供搜索的已索引论坛频道。\n请检查机器人权限或联系管理组。", ephemeral=True)
                return

            view = ChannelSelectionView(self, interaction, channels)
            await interaction.followup.send("请选择要搜索的频道：", view=view, ephemeral=True)
        except Exception:
            logger.error("在 start_global_search_flow 中发生严重错误", exc_info=True)
            # 确保即使有异常，也能给用户一个反馈
            if not interaction.response.is_done():
                await safe_defer(interaction)
            await interaction.followup.send(f"❌ 启动搜索时发生严重错误，请联系管理员。", ephemeral=True)

    @app_commands.command(name="快捷搜索", description="快速搜索指定作者的所有帖子")
    @app_commands.describe(author="要搜索的作者（@用户 或 用户ID）")
    async def quick_author_search(self, interaction: discord.Interaction, author: discord.User):
        """启动一个交互式视图，用于搜索特定作者的帖子并按标签等进行筛选。"""
        # Defer 将在 NewAuthorTagSelectionView 的 start() -> update_view() 中被调用
        # 这里我们只需要捕获异常并用 followup 发送
        try:
            view = NewAuthorTagSelectionView(self, interaction, author.id)
            await view.start()
        except Exception as e:
            await safe_defer(interaction)
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send(f"❌ 启动快捷搜索失败: {e}", ephemeral=True),
                priority=1
            )

    # ----- Embed 构造 -----
    async def _build_thread_embed(self, thread: 'ThreadModel', guild: discord.Guild, preview_mode: str = "thumbnail") -> discord.Embed:
        """根据Thread ORM对象构建嵌入消息"""
        
        author_display = f"作者 <@{thread.author_id}>"

        embed = discord.Embed(
            title=thread.title,
            description=author_display,
            url=f"https://discord.com/channels/{guild.id}/{thread.thread_id}"
        )
        
        # 标签信息通过 relationship 加载
        tag_names = [tag.name for tag in thread.tags]

        # 基础统计信息
        basic_stats = (
            f"发帖日期: **{thread.created_at.strftime('%Y-%m-%d %H:%M:%S')}** | "
            f"最近活跃: **{thread.last_active_at.strftime('%Y-%m-%d %H:%M:%S')}**\n"
            f"最高反应数: **{thread.reaction_count}** | 总回复数: **{thread.reply_count}**\n"
            f"标签: **{', '.join(tag_names) if tag_names else '无'}**"
        )
        
        embed.add_field(
            name="统计",
            value=basic_stats,
            inline=False,
        )
        
        # 首楼摘要
        excerpt = thread.first_message_excerpt or ""
        excerpt_display = excerpt[:200] + "..." if len(excerpt) > 200 else (excerpt or "无内容")
        embed.add_field(name="首楼摘要", value=excerpt_display, inline=False)
        
        # 根据用户偏好设置预览图显示方式
        if thread.thumbnail_url:
            if preview_mode == "image":
                embed.set_image(url=thread.thumbnail_url)
            else:  # thumbnail
                embed.set_thumbnail(url=thread.thumbnail_url)
        
        return embed
            
    async def _search_and_display(
        self,
        interaction: discord.Interaction,
        search_qo: 'ThreadSearchQuery',
        page: int = 1
    ) -> dict:
        """
        通用搜索和显示函数
        
        :param interaction: discord.Interaction
        :param search_qo: ThreadSearchQO 查询对象
        :param page: 当前页码
        :return: 包含搜索结果信息的字典
        """
        try:
            logger.debug(f"--- 搜索开始 (Page: {page}) ---")
            logger.debug(f"初始QO: {search_qo}")
            async with self.session_factory() as session:
                repo = SearchRepository(session)
                user_prefs = await repo.get_user_preferences(interaction.user.id)
                logger.debug(f"用户偏好: {user_prefs}")
                
                per_page = 5
                preview_mode = "thumbnail"
                if user_prefs:
                    per_page = user_prefs.results_per_page
                    preview_mode = user_prefs.preview_image_mode
                    
                    # 合并偏好设置到查询对象
                    # 只有当查询对象中没有相应值时，才使用偏好设置
                    if search_qo.include_authors is None:
                        search_qo.include_authors = user_prefs.include_authors
                    if search_qo.exclude_authors is None:
                        search_qo.exclude_authors = user_prefs.exclude_authors
                    if search_qo.after_ts is None:
                        search_qo.after_ts = user_prefs.after_date
                    if search_qo.before_ts is None:
                        search_qo.before_ts = user_prefs.before_date
                    
                    # 标签逻辑总是以用户偏好为准，除非视图有特殊覆盖
                    # 在这个场景下，我们让偏好覆盖默认值
                    if user_prefs.tag_logic:
                        search_qo.tag_logic = user_prefs.tag_logic
                
                logger.debug(f"合并后QO: {search_qo}")

                # 设置分页
                offset = (page - 1) * per_page
                limit = per_page

                # 执行搜索
                threads = await repo.search_threads(search_qo, offset=offset, limit=limit)
                total_threads = await repo.count_threads(search_qo)

            if not threads:
                return {'has_results': False, 'total': 0}

            # 构建 embeds
            embeds = []
            for thread in threads:
                embed = await self._build_thread_embed(thread, interaction.guild, preview_mode)
                embeds.append(embed)

            return {
                'has_results': True,
                'embeds': embeds,
                'total': total_threads,
                'page': page,
                'per_page': per_page,
                'max_page': (total_threads + per_page - 1) // per_page
            }
        except Exception as e:
            logger.error(f"搜索时发生错误: {e}", exc_info=True)
            return {'has_results': False, 'error': str(e)}


