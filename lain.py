import asyncio
import os
import time
from astrbot.api import AstrBotPlugin, MessageEvent, CommandResult
from astrbot.api.message import MessageChain, Plain, Voice
from astrbot.api.platform import Platform

'''
Lain 服务插件
触发命令: /lain 或 /服务
'''

# 用户状态存储
user_states = {}

class LainServicePlugin(AstrBotPlugin):
    def __init__(self, bot):
        super().__init__(bot)
        self.audio_path = os.path.join(os.path.dirname(__file__), "go.ogg")

    async def on_command(self, event: MessageEvent, args: list):
        if event.command == "lain" or event.command == "服务":
            user_id = event.sender_id
            # 进入卡网输入状态
            user_states[user_id] = {"state": "await_card", "data": {}}
            yield CommandResult(
                message=MessageChain([Plain("🔒 请输入卡网：")]),
                reply=True
            )

    async def on_message(self, event: MessageEvent):
        user_id = event.sender_id
        if user_id not in user_states:
            return

        state_info = user_states[user_id]
        text = event.message.get_text().strip()
        msg_chain = []

        # 状态 1：等待输入卡网
        if state_info["state"] == "await_card":
            if text.lower() == "lain":
                # 验证成功
                state_info["state"] = "loading"
                msg_chain.append(Plain("✅ 检测到是开发者用户，已为你自动屏蔽"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

                # 异步开始加载动画
                asyncio.create_task(self.show_loading(event, user_id))
            else:
                msg_chain.append(Plain("❌ 卡网错误，请重新输入 lain"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

        # 状态 2：主菜单选择 (自瞄/断点子追/硬件子追/hook一下)
        elif state_info["state"] == "main_menu":
            choice = text
            if choice == "1":     # 自瞄
                msg_chain.append(Plain("🎯 自瞄已激活，瞄准中..."))
                del user_states[user_id]   # 结束流程
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

            elif choice == "2":   # 断点子追
                msg_chain.append(Plain("🔗 断点子追已启动，追踪子弹轨迹..."))
                del user_states[user_id]
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

            elif choice == "3":   # 硬件子追 → 进入参数调节
                state_info["state"] = "param_tune"
                state_info["data"]["param"] = 50   # 默认参数50%
                param = 50
                bar = self.generate_bar(param)
                msg_chain.append(Plain(f"⚙️ 硬件子追参数调节\n\n当前参数：{param}%\n{bar}\n\n"
                                       "发送数字(0-100)设置参数，发送“完成”结束调节"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

            elif choice == "4":   # hook一下 → 进入二级菜单
                state_info["state"] = "hook_menu"
                msg_chain.append(Plain("💉 Hook 菜单\n1. 黑客入侵\n2. 疯狂状态\n回复数字选择"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

            else:
                msg_chain.append(Plain("请回复数字 1-4 选择功能"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

        # 状态 3：Hook 二级菜单
        elif state_info["state"] == "hook_menu":
            choice = text
            if choice == "1":   # 黑客入侵
                # 悬浮窗 + 语音 "go"
                msg_chain.append(Plain("✅ 开启成功，感受黑客的愤怒吧！"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

                # 发送语音
                if os.path.exists(self.audio_path):
                    voice = Voice(file=self.audio_path)
                    yield CommandResult(message=MessageChain([voice]))
                else:
                    yield CommandResult(message=MessageChain([Plain("(语音文件缺失，但 GO 已在心中响起)")]))

                del user_states[user_id]   # 结束

            elif choice == "2":   # 疯狂状态
                msg_chain.append(Plain("🤯 疯狂状态已激活！系统超频运转..."))
                del user_states[user_id]
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

            else:
                msg_chain.append(Plain("请回复 1 或 2 选择"))
                yield CommandResult(message=MessageChain(msg_chain), reply=True)

        # 状态 4：硬件子追参数调节
        elif state_info["state"] == "param_tune":
            if text == "完成":
                del user_states[user_id]
                yield CommandResult(message=MessageChain([Plain("✅ 参数调节完成，已保存")]), reply=True)
            else:
                try:
                    val = int(text)
                    if 0 <= val <= 100:
                        state_info["data"]["param"] = val
                        bar = self.generate_bar(val)
                        yield CommandResult(
                            message=MessageChain([Plain(f"当前参数：{val}%\n{bar}\n发送“完成”结束")]),
                            reply=True
                        )
                    else:
                        yield CommandResult(message=MessageChain([Plain("数值需在 0-100 之间")]), reply=True)
                except ValueError:
                    yield CommandResult(message=MessageChain([Plain("请输入有效数字或“完成”")]), reply=True)

    # 入侵加载动画
    async def show_loading(self, event: MessageEvent, user_id: int):
        # 发送初始加载条
        load_msg = await self.bot.send(event, MessageChain([Plain("正在尝试入侵 ACE 服务器 [                  ] 0%")]))
        if not load_msg:
            return

        for i in range(1, 21):
            await asyncio.sleep(0.3)
            progress = i * 5
            filled = "█" * i + " " * (20 - i)
            text = f"正在尝试入侵 ACE 服务器 [{filled}] {progress}%"
            try:
                await self.bot.edit_message(load_msg, MessageChain([Plain(text)]))
            except Exception:
                pass

        # 入侵成功动画
        success_text = (
            "🔓 入侵成功\n"
            "✅ 类似 Face ID 解锁成功\n"
            "━━━━━━━━━━━━━\n"
            "服务器连接正常"
        )
        await self.bot.edit_message(load_msg, MessageChain([Plain(success_text)]))

        # 发送功能菜单
        menu_text = (
            "请选择功能：\n"
            "1. 自瞄\n"
            "2. 断点子追\n"
            "3. 硬件子追\n"
            "4. hook 一下"
        )
        await self.bot.send(event, MessageChain([Plain(menu_text)]))

        # 更新状态为主菜单
        user_states[user_id] = {"state": "main_menu", "data": {}}

    # 生成进度条字符串
    def generate_bar(self, value):
        filled = int(value / 5)
        bar = "[" + "█" * filled + " " * (20 - filled) + "]"
        return bar