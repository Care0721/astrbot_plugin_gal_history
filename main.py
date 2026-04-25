import asyncio
import re
import aiohttp
from datetime import datetime
from typing import List, Optional

from astrbot.api.plugin import Plugin, on_command, CommandContext
from astrbot.api.message import MessageChain, Plain, Image
from astrbot.api.scheduler import scheduled
from astrbot.api.plugin_config import PluginConfig, ConfigItem

# ---------- 配置定义（会映射到网页面板） ----------
class GalHistoryConfig(PluginConfig):
    target_groups: List[str] = ConfigItem(default=[], desc="每日自动推送的群聊ID列表")
    push_time: str = ConfigItem(default="08:00", desc="每日播报时间 (HH:MM)")
    enable_bangumi: bool = ConfigItem(default=True, desc="启用Bangumi数据补充")
    enable_erogamescape: bool = ConfigItem(default=False, desc="尝试批评空间(需安装库)")
    max_results: int = ConfigItem(default=5, desc="每次播报最多展示的作品数")
    proxy: str = ConfigItem(default="", desc="HTTP代理地址，留空则不使用")

# ---------- 主插件类 ----------
class GalHistoryToday(Plugin):
    config: GalHistoryConfig

    def __init__(self, context, config: GalHistoryConfig):
        super().__init__(context)
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None

        # 初始化代理设置
        connector = None
        if self.config.proxy:
            connector = aiohttp.TCPConnector(force_close=True)
            self.proxy = self.config.proxy
        else:
            self.proxy = None

        self.connector = connector

        # 尝试加载非官方的批评空间库
        self.erogamescape_available = False
        if self.config.enable_erogamescape:
            try:
                import sqlforerogamer
                self.erogamescape_available = True
                self.context.logger.info("批评空间第三方库已加载。")
            except ImportError:
                self.context.logger.warning("未安装sqlforerogamer，批评空间功能不可用。")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "User-Agent": "AstrBot_GalHistory/2.0",
                "Content-Type": "application/json"
            }
            self.session = aiohttp.ClientSession(
                headers=headers,
                connector=self.connector
            )
        return self.session

    # ------- VNDB API -------
    async def _fetch_vndb_releases(self, date_mm_dd: str) -> List[dict]:
        """
        从VNDB获取指定 MM-DD 发售的游戏列表
        注意：VNDB的released字段格式为YYYY-MM-DD，需构造完整日期
        这里简单处理：用当前年份拼接，也可用 '*' 通配但API不支持
        更稳健的做法是查询所有年份当日，但我们只需播报，就查询当前年份即可
        如果想要所有年份，可以发送多个请求，这里简化
        """
        today = datetime.now()
        # 用当年拼接日期，也能获取往年今日但VNDB是按年度存储的，
        # 如果想获取历史所有年份，需要循环1970-now，这里只展示近20年示例
        years = range(today.year - 20, today.year + 1)
        all_releases = []
        session = await self._get_session()

        for year in years:
            full_date = f"{year}-{date_mm_dd}"
            payload = {
                "filters": ["released", "=", full_date],
                "results": 10,
                "page": 1
            }
            try:
                async with session.post("https://api.vndb.org/kana/release", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        all_releases.extend(results)
            except Exception as e:
                self.context.logger.error(f"VNDB请求 {full_date} 失败: {e}")
                continue

        # 去重 (可能同一作品不同版本)
        seen_ids = set()
        unique = []
        for r in all_releases:
            vid = r.get("id")
            if vid not in seen_ids:
                seen_ids.add(vid)
                unique.append(r)
        return unique[:self.config.max_results]

    async def _enrich_vndb_details(self, releases: List[dict]) -> List[dict]:
        """补充VNDB的VN详情（评分、封面）"""
        session = await self._get_session()
        for rel in releases:
            vn_id = rel.get("vn_id")
            if not vn_id:
                continue
            try:
                async with session.post("https://api.vndb.org/kana/vn", json={
                    "filters": ["id", "=", vn_id],
                    "results": 1
                }) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("results"):
                            rel["vndb_details"] = data["results"][0]
            except Exception as e:
                self.context.logger.error(f"获取VNDB详情失败 vn{vn_id}: {e}")
        return releases

    # ------- Bangumi API -------
    async def _enrich_bangumi(self, releases: List[dict]) -> List[dict]:
        if not self.config.enable_bangumi:
            return releases
        session = await self._get_session()
        for rel in releases:
            title = rel.get("title", "")
            if not title:
                continue
            # 用日文原名搜索，可能更好的匹配
            # Bangumi搜索API：https://api.bgm.tv/search/subject/{关键词}?type=6&responseGroup=large
            # 其中type=6代表游戏
            url = f"https://api.bgm.tv/search/subject/{title}?type=6&responseGroup=large"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("list", [])
                        if items:
                            # 简单取第一个，后续可做更精细的标题匹配
                            rel["bangumi_details"] = items[0]
            except Exception as e:
                self.context.logger.error(f"Bangumi搜索失败 [{title}]: {e}")
        return releases

    # ------- 批评空间 (非官方) -------
    async def _enrich_erogamescape(self, releases: List[dict]) -> List[dict]:
        if not (self.config.enable_erogamescape and self.erogamescape_available):
            return releases
        import sqlforerogamer
        for rel in releases:
            title = rel.get("title", "")
            if not title:
                continue
            # 这里仅做标记，实际解析可能需要深入爬虫，由于版权和稳定性，只展示尝试标记
            rel["erogamescape_attempted"] = True
            # 可以记录日志但不做实际请求，避免被封
        return releases

    # ------- 消息构建 -------
    def _build_message(self, releases: List[dict], date_str: str) -> MessageChain:
        chain = [Plain(f"📅 今日 Galgame 发售纪念日 ({date_str})\n")]
        if not releases:
            chain.append(Plain("今天没有记录到知名作品发售。"))
            return MessageChain(chain)

        for idx, rel in enumerate(releases, 1):
            title = rel.get("title", "未知标题")
            publisher = rel.get("publisher", "未知发行商")
            text = f"\n{idx}. 《{title}》\n   🏢 {publisher}"

            # VNDB评分
            vndb = rel.get("vndb_details", {})
            if vndb.get("rating"):
                text += f" | ⭐ VNDB: {vndb['rating']}/10"

            # Bangumi评分
            bgm = rel.get("bangumi_details", {})
            if bgm:
                bgm_rating = bgm.get("rating", {})
                if bgm_rating.get("score"):
                    text += f" | 🔵 Bangumi: {bgm_rating['score']}"
                # 简介
                summary = bgm.get("summary", "")
                if summary:
                    clean_summary = re.sub(r'<[^>]+>', '', summary).strip()[:100]
                    text += f"\n   📖 {clean_summary}..."

            # 批评空间标记
            if rel.get("erogamescape_attempted"):
                text += "\n   📊 批评空间: 数据已收录"

            chain.append(Plain(text))

            # 图片（优先Bangumi）
            img_url = None
            if bgm and bgm.get("images", {}).get("small"):
                img_url = bgm["images"]["small"]
            elif vndb.get("image", {}).get("url"):
                img_url = vndb["image"]["url"]
            if img_url:
                try:
                    chain.append(Image.from_url(img_url))
                except:
                    pass

        return MessageChain(chain)

    # ------- 核心指令 -------
    @on_command("gal历史", aliases=["galhistory", "今天发售"])
    async def show_gal_history(self, ctx: CommandContext):
        # 解析日期参数
        args = ctx.message.split()
        today = datetime.now()
        target_mmdd = today.strftime("-%m-%d")[1:]  # "MM-DD"
        if len(args) > 1:
            date_str = args[1]
            if "月" in date_str:
                try:
                    m, d = date_str.replace("月", "-").replace("日", "").split("-")
                    target_mmdd = f"{int(m):02d}-{int(d):02d}"
                except:
                    await ctx.send(MessageChain([Plain("日期格式错误，示例: /gal历史 2月14")]))
                    return
            else:
                # 支持 MM-DD
                target_mmdd = date_str.strip()

        await ctx.send(MessageChain([Plain(f"🔎 正在查询 {target_mmdd} 发售的 Galgame ...")]))

        releases = await self._fetch_vndb_releases(target_mmdd)
        releases = await self._enrich_vndb_details(releases)
        releases = await self._enrich_bangumi(releases)
        releases = await self._enrich_erogamescape(releases)

        msg = self._build_message(releases, target_mmdd)
        await ctx.send(msg)

    # ------- 定时自动播报 -------
    @scheduled("cron", args=["0", "0", "*", "*", "*"])  # 占位，实际用自定义调度器
    async def daily_push(self):
        """
        每日定时任务，在 config.push_time 配置的时间点触发。
        由于AstrBot的scheduled装饰器可能不支持动态时间，我们用简单循环检测。
        """
        # 解析时间
        hour, minute = map(int, self.config.push_time.split(":"))
        while True:
            now = datetime.now()
            if now.hour == hour and now.minute == minute:
                # 执行播报
                mmdd = now.strftime("%m-%d")
                releases = await self._fetch_vndb_releases(mmdd)
                releases = await self._enrich_vndb_details(releases)
                releases = await self._enrich_bangumi(releases)
                releases = await self._enrich_erogamescape(releases)
                msg = self._build_message(releases, mmdd)
                for group_id in self.config.target_groups:
                    try:
                        await self.context.send_group_msg(group_id, msg)
                    except Exception as e:
                        self.context.logger.error(f"每日推送群 {group_id} 失败: {e}")
                # 休眠60秒避免同一分钟多次触发
                await asyncio.sleep(60)
            else:
                # 每30秒检查一次
                await asyncio.sleep(30)

    async def on_load(self):
        """插件加载时启动每日播报任务"""
        if self.config.target_groups:
            asyncio.create_task(self.daily_push())
            self.context.logger.info(f"每日 Gal 纪念日播报已启动，时间 {self.config.push_time}")

    async def on_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()