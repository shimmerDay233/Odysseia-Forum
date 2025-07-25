import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import logging

from sqlalchemy.orm import sessionmaker
from shared.discord_utils import safe_defer
from tag_system.cog import TagSystem
from .views import IndexerDashboard

class Indexer(commands.Cog):
    """构建索引相关命令"""

    def __init__(self, bot: commands.Bot, session_factory: sessionmaker):
        self.bot = bot
        self.session_factory = session_factory

    @staticmethod
    async def _aiter_to_list(aiter):
        return [item async for item in aiter]

    @staticmethod
    async def _aiter_to_list(aiter):
        return [item async for item in aiter]

    @app_commands.command(name="构建索引", description="对当前论坛频道的所有帖子进行索引")
    async def build_index(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        if not isinstance(interaction.channel, discord.Thread):
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("请在论坛频道的帖子内使用此命令。", ephemeral=True),
                priority=1
            )
            return
        
        channel = interaction.channel.parent
        if not isinstance(channel, discord.ForumChannel):
            await self.bot.api_scheduler.submit(
                coro=interaction.followup.send("此命令仅适用于论坛频道。", ephemeral=True),
                priority=1
            )
            return

        dashboard = IndexerDashboard(self, channel)
        await dashboard.start(interaction)

    async def run_indexer(self, dashboard: IndexerDashboard):
        """运行生产者和消费者任务"""
        logging.info(f"[{dashboard.channel.id}] run_indexer 开始.")
        producer_task = self.bot.loop.create_task(self.producer(dashboard))
        consumer_task = self.bot.loop.create_task(self.consumer(dashboard))
        
        try:
            await asyncio.gather(producer_task, consumer_task)
        except Exception as e:
            dashboard.progress['error'] = f"{type(e).__name__}: {e}"
        
        dashboard.progress['finished'] = True
        await dashboard.update_embed()

        # 索引完成，分发一个全局事件，通知所有相关模块刷新缓存
        logging.info(f"[{dashboard.channel.id}] 索引完成，分发 'index_updated' 事件。")
        self.bot.dispatch("index_updated")

    async def producer(self, dashboard: IndexerDashboard):
        """生产者：发现帖子并放入队列"""
        channel = dashboard.channel
        progress = dashboard.progress
        queue = dashboard.queue
        
        # 活跃线程 (来自缓存，无API调用)
        for thread in channel.threads:
            await queue.put(thread)
            progress['discovered'] += 1

        # 已归档线程 (手动分页，通过调度器获取)
        last_thread_timestamp = None
        while not dashboard.is_cancelled():
            # 将获取一个批次的操作作为一个协程，提交给调度器
            archived_threads_iterator = channel.archived_threads(limit=100, before=last_thread_timestamp)
            batch = await self.bot.api_scheduler.submit(
                coro=self._aiter_to_list(archived_threads_iterator),
                priority=10 # 这是低优先级后台任务
            )

            if not batch:
                break

            for thread in batch:
                await self.queue.put(thread)
                progress['discovered'] += 1
            
            last_thread_timestamp = batch[-1].created_at
            
        if not dashboard.is_cancelled():
            progress['total'] = progress['discovered']

        await queue.put(None) # 终止符

    async def consumer(self, dashboard: IndexerDashboard):
        """消费者：从队列中取出帖子并处理"""
        tag_system_cog: TagSystem = self.bot.get_cog("TagSystem")
        progress = dashboard.progress
        queue = dashboard.queue

        while True:
            await dashboard.wait_if_paused()
            if dashboard.is_cancelled():
                break

            thread = await queue.get()
            if thread is None: # 终止信号符到达
                queue.task_done()
                break
            
            if tag_system_cog:
                await tag_system_cog.sync_thread(thread, fetch_if_incomplete=True)

            progress['processed'] += 1
            
            queue.task_done()
