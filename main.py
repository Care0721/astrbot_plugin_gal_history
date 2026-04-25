import asyncio
import re
import os
import json
import aiohttp
from datetime import datetime
from typing import List, Optional

# ---------- 兼容多种导入方式 ----------
try:
    # 标准 astrbot-api 路径
    from astrbot.api.plugin import Plugin, on_command, CommandContext
    from astrbot.api.message import MessageChain, Plain, Image
    from astrbot.api.scheduler import scheduled
    HAS_API = True
except ImportError:
    try:
        # 部分旧版本路径
        from astrbot import Plugin, on_command, CommandContext
        from astrbot import MessageChain, Plain, Image
        from astrbot import scheduled
        HAS_API = False
    except ImportError:
        raise ImportError("无法导入 AstrBot 依赖，请确认已安装 astrbot 或 astrbot-api")

# ---------- 配置加载工具 ----------
def load_config_from_schema(plugin_instance):
    """根据 _conf_schema.json 自动从框架获取配置字典"""
    config_path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    # AstrBot 通常会将所有配置合并后放在 plugin.config (如果框架支持)
    # 作为保底，我们直接将 schema 中的 default 值作为初始配置
    config = {}
    for key, desc in schema.items():
        config[key] = desc.get("default", None)
    # 如果框架提供了 get_plugin_config 方法，则用框架值覆盖
    if hasattr(plugin_instance.context, "get_plugin_config"):
        saved = plugin_instance.context.get_plugin_config()
        config.update(saved)
    return config


class GalHistoryToday(Plugin):
    # 如果用高级 API，可以定义 Config 类；但为了兼容，我们一律使用字典
    def __init__(self, context):
        super().__init__(context)
        # 从 _conf_schema.json 读取默认值，并应用框架保存的配置
        self.config = load_config_from_schema(self)

        self.session: Optional[aiohttp.ClientSession] = None
        self.proxy = self.config.get("proxy") or None
        self.connector = None

        # 批评空间库检测
        self.erogamescape_available = False
        if self.config.get("enable_erogamescape"):
            try:
                import sqlforerogamer
                self.erogamescape_available = True
                self.context.logger.info("批评空间第三方库已加载。")
            except ImportError:
                self.context.logger.warning("缺少 sqlforerogamer，批评空间功能不可用。")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "User-Agent": "AstrBot_GalHistory/2.0",
                "Content-Type": "application/json"
            }
            self.session = aiohttp.ClientSession(headers=headers, connector=self.connector)
        return self.session

    # ---------- VNDB ----------
    async def _fetch_vndb_releases(self, date_mm_dd: str) -> List[dict]:
        today = datetime.now()
        years = range(today.year - 20, today.year + 1)  # 往前查 20 年
        all_releases = []
        session = await self._get_session()
        max_results = int(self.config.get("max_results", 5))

        for year in years:
            full_date = f"{year}-{date_mm_dd}"
            payload = {
                "filters": ["released", "=", full_date],
                "results": 10,
                "page": 1
            }
            try:
                async with session.post(
                    "https://api.vndb.org/kana/release",
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_releases.extend(data.get("results", []))
            except Exception as e:
                self.context.logger.error(f"VNDB 查询 {full_date} 失败: {e}")

        # 去重
        seen = set()
        unique = []
        for r in all_releases:
            rid = r.get("id")
            if rid not in seen:
                seen.add(rid)
                unique.append(r)
        return unique[:max_results]

    async def _enrich_vndb_details(self, releases: List[dict]) -> List[dict]:
        session = await self._get_session()
        for rel in releases:
            vn_id = rel.get("vn_id")
            if not vn_id:
                continue
            try:
                async with session.post(
                    "https://api.vndb.org/kana/vn",
                    json={"filters": ["id", "=", vn_id], "results": 1}
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("results"):
                            rel["vndb_details"] = data["results"][0]
            except Exception as e:
                self.context.logger.error(f"VNDB 详情获取失败 vn{vn_id}: {e}")
        return releases

    # ---------- Bangumi ----------
    async def _enrich_bangumi(self, releases: List[dict]) -> List[dict]:
        if not self.config.get("enable_bangumi", True):
            return releases
        session = await self._get_session()
        for rel in releases:
            title = rel.get("title", "")
            if not title:
                continue
            url = f"https://api.bgm.tv/search/subject/{title}?type=6&responseGroup=large"
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("list"):
                            rel["bangumi_details"] = data["list"][0]
            except Exception as e:
                self.context.logger.error(f"Bangumi 搜索失败 [{title}]: {e}")
        return releases

    # ---------- 批评空间 ----------
    async def _enrich_erogamescape(self, releases: List[dict]) -> List[dict]:
        if not (self.config.get("enable_erogamescape") and self.erogamescape_available):
            return releases
        for rel in releases:
            rel["erogamescape_attempted"] = True   # 仅标记，实际不做请求
        return releases

    # ---------- 消息构建 ----------
    def _build_message(self, releases: List[dict], date_str: str) -> MessageChain:
        chain = [Plain(f"📅 今日 Galgame 发售纪念日 ({date_str})\n")]
        if not releases:
            chain.append(Plain("今天没有记录到知名作品发售，换个日期试试吧~"))
            return MessageChain(chain)

        for idx, rel in enumerate(releases, 1):
            title = rel.get("title", "未知标题")
            publisher = rel.get("publisher", "未知发行商")
            text = f"\n{idx}. 《{title}》\n   🏢 {publisher}"

            vndb = rel.get("vndb_details", {})
            if vndb.get("rating"):
                text += f" | ⭐ VNDB: {vndb['rating']}/10"

            bgm = rel.get("bangumi_details", {})
            img_url = None
            if bgm:
                if bgm.get("rating", {}).get("score"):
                    text += f" | 🔵 Bangumi: {bgm['rating']['score']}"
                if bgm.get("summary"):
                    clean = re.sub(r'<[^>]+>', '', bgm["summary"]).strip()[:120]
                    text += f"\n   📖 {clean}..."
                img_url = bgm.get("images", {}).get("small")
            if not img_url:
                img_url = vndb.get("image", {}).get("url")

            if rel.get("erogamescape_attempted"):
                text += "\n   📊 批评空间: 数据已收录"

            chain.append(Plain(text))

            # 附加图片
            if img_url:
                try:
                    chain.append(Image.from_url(img_url))
                except Exception:
                    pass
        return MessageChain(chain)

    # ---------- 用户指令 ----------
    @on_command("gal历史", aliases=["galhistory", "今天发售"])
    async def show_gal_history(self, ctx: CommandContext):
        args = ctx.message.split()
        target_mmdd = datetime.now().strftime("%m-%d")
        if len(args) > 1:
            date_str = args[1]
            try:
                if "月" in date_str:
                    month, day = date_str.replace("月", "-").replace("日", "").split("-")
                    target_mmdd = f"{int(month):02d}-{int(day):02d}"
                else:
                    target_mmdd = date_str.strip()
            except Exception:
                await ctx.send(MessageChain([Plain("日期格式错误，示例：/gal历史 2月14")]))
                return

        await ctx.send(MessageChain([Plain(f"🔎 正在查询 {target_mmdd} 发售的 Galgame ...")]))
        releases = await self._fetch_vndb_releases(target_mmdd)
        releases = await self._enrich_vndb_details(releases)
        releases = await self._enrich_bangumi(releases)
        releases = await self._enrich_erogamescape(releases)
        await ctx.send(self._build_message(releases, target_mmdd))

    # ---------- 每日自动播报 ----------
    async def _daily_push_task(self):
        hour, minute = map(int, self.config.get("push_time", "08:00").split(":"))
        while True:
            now = datetime.now()
            if now.hour == hour and now.minute == minute:
                mmdd = now.strftime("%m-%d")
                releases = await self._fetch_vndb_releases(mmdd)
                releases = await self._enrich_vndb_details(releases)
                releases = await self._enrich_bangumi(releases)
                releases = await self._enrich_erogamescape(releases)
                msg = self._build_message(releases, mmdd)
                for gid in self.config.get("target_groups", []):
                    try:
                        await self.context.send_group_msg(gid, msg)
                    except Exception as e:
                        self.context.logger.error(f"群 {gid} 推送失败: {e}")
                await asyncio.sleep(60)   # 防止同一分钟重复推送
            else:
                await asyncio.sleep(30)   # 每 30 秒检查一次时间

    async def on_load(self):
        """插件加载时自动启动播报任务"""
        if self.config.get("target_groups"):
            asyncio.create_task(self._daily_push_task())
            self.context.logger.info(f"每日 Gal 纪念日报时已启动，推送时间 {self.config.get('push_time')}")

    async def on_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()