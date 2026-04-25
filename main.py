import asyncio
import re
import os
import json
import aiohttp
from datetime import datetime

# ========== 兼容导入（只用你已有的 AstrBot） ==========
try:
    from astrbot import Plugin, on_command, CommandContext
    from astrbot import MessageChain, Plain, Image
except ImportError:
    from astrbot.core.plugin import Plugin
    from astrbot.core.plugin import on_command, CommandContext
    from astrbot.core.message import MessageChain, Plain, Image

# ========== 从 _conf_schema.json 加载配置 ==========
def load_config(plugin):
    path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    config = {}
    for key, attrs in schema.items():
        config[key] = attrs.get("default")
    # 如果框架保存过配置，优先使用框架内的
    try:
        saved = plugin.context.get_plugin_config()
        config.update(saved)
    except:
        pass
    return config


class GalHistoryToday(Plugin):
    def __init__(self, context):
        super().__init__(context)
        self.config = load_config(self)

        self.session = None
        self.proxy = self.config.get("proxy", "") or None
        self.erogamescape_available = False
        if self.config.get("enable_erogamescape"):
            try:
                import sqlforerogamer
                self.erogamescape_available = True
            except ImportError:
                pass

    async def _get_session(self):
        if self.session is None or self.session.closed:
            headers = {"User-Agent": "AstrBot_GalToday/3.0", "Content-Type": "application/json"}
            self.session = aiohttp.ClientSession(headers=headers)
        return self.session

    # ---------- VNDB 查询 ----------
    async def _fetch_vndb_releases(self, mmdd: str):
        today = datetime.now()
        years = range(today.year - 20, today.year + 1)
        all_releases = []
        s = await self._get_session()
        for y in years:
            payload = {"filters": ["released", "=", f"{y}-{mmdd}"], "results": 10, "page": 1}
            try:
                async with s.post("https://api.vndb.org/kana/release", json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_releases.extend(data.get("results", []))
            except:
                continue
        # 去重
        seen = set()
        unique = []
        for r in all_releases:
            if r["id"] not in seen:
                seen.add(r["id"])
                unique.append(r)
        return unique[: int(self.config.get("max_results", 5))]

    async def _enrich_vndb_details(self, releases):
        s = await self._get_session()
        for r in releases:
            vn_id = r.get("vn_id")
            if not vn_id:
                continue
            try:
                async with s.post("https://api.vndb.org/kana/vn", json={
                    "filters": ["id", "=", vn_id], "results": 1
                }) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("results"):
                            r["vndb_details"] = data["results"][0]
            except:
                pass
        return releases

    # ---------- Bangumi 补充 ----------
    async def _enrich_bangumi(self, releases):
        if not self.config.get("enable_bangumi", True):
            return releases
        s = await self._get_session()
        for r in releases:
            title = r.get("title", "")
            if not title:
                continue
            try:
                async with s.get(f"https://api.bgm.tv/search/subject/{title}?type=6&responseGroup=large") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("list"):
                            r["bangumi_details"] = data["list"][0]
            except:
                pass
        return releases

    # ---------- 批评空间标记 ----------
    async def _enrich_erogamescape(self, releases):
        if self.config.get("enable_erogamescape") and self.erogamescape_available:
            for r in releases:
                r["erogamescape_attempted"] = True
        return releases

    # ---------- 构造消息 ----------
    def _build_msg(self, releases, date_str):
        msg = MessageChain([Plain(f"📅 今日 Galgame 发售纪念日 ({date_str})\n")])
        if not releases:
            msg.append(Plain("今天没有记录到知名作品发售，换个日期试试吧～"))
            return msg

        for idx, r in enumerate(releases, 1):
            title = r.get("title", "未知标题")
            pub = r.get("publisher", "未知发行商")
            text = f"\n{idx}. 《{title}》\n   🏢 {pub}"

            vndb = r.get("vndb_details", {})
            if vndb.get("rating"):
                text += f" | ⭐ VNDB: {vndb['rating']}/10"

            bgm = r.get("bangumi_details", {})
            img_url = None
            if bgm:
                if bgm.get("rating", {}).get("score"):
                    text += f" | 🔵 Bangumi: {bgm['rating']['score']}"
                if bgm.get("summary"):
                    clean = re.sub(r"<[^>]+>", "", bgm["summary"]).strip()[:120]
                    text += f"\n   📖 {clean}..."
                img_url = bgm.get("images", {}).get("small")
            if not img_url:
                img_url = vndb.get("image", {}).get("url")

            if r.get("erogamescape_attempted"):
                text += "\n   📊 批评空间: 数据已收录"

            msg.append(Plain(text))
            if img_url:
                try:
                    msg.append(Image.from_url(img_url))
                except:
                    pass
        return msg

    # ---------- 用户指令 ----------
    @on_command("gal历史", aliases=["galhistory", "今天发售"])
    async def gal_history(self, ctx: CommandContext):
        args = ctx.message.split()
        mmdd = datetime.now().strftime("%m-%d")
        if len(args) > 1:
            ds = args[1]
            try:
                if "月" in ds:
                    month, day = ds.replace("月", "-").replace("日", "").split("-")
                    mmdd = f"{int(month):02d}-{int(day):02d}"
                else:
                    mmdd = ds.strip()
            except:
                await ctx.send(MessageChain([Plain("日期格式错误，示例：/gal历史 2月14")]))
                return

        await ctx.send(MessageChain([Plain(f"🔎 正在查询 {mmdd} 发售的 Galgame ...")]))
        releases = await self._fetch_vndb_releases(mmdd)
        releases = await self._enrich_vndb_details(releases)
        releases = await self._enrich_bangumi(releases)
        releases = await self._enrich_erogamescape(releases)
        await ctx.send(self._build_msg(releases, mmdd))

    # ---------- 每日播报 ----------
    async def _daily_task(self):
        target = self.config.get("push_time", "08:00")
        hour, minute = map(int, target.split(":"))
        while True:
            now = datetime.now()
            if now.hour == hour and now.minute == minute:
                mmdd = now.strftime("%m-%d")
                rels = await self._fetch_vndb_releases(mmdd)
                rels = await self._enrich_vndb_details(rels)
                rels = await self._enrich_bangumi(rels)
                rels = await self._enrich_erogamescape(rels)
                msg = self._build_msg(rels, mmdd)
                for gid in self.config.get("target_groups", []):
                    try:
                        await self.context.send_group_msg(gid, msg)
                    except:
                        pass
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(30)

    async def on_load(self):
        if self.config.get("target_groups"):
            asyncio.create_task(self._daily_task())

    async def on_unload(self):
        if self.session:
            await self.session.close()