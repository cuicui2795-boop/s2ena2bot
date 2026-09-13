# -*- coding: utf-8 -*-
import asyncio
import os
import sys

# Windows 콘솔 유니코드 출력 오류 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 단일 인스턴스 보호 ──────────────────────────────────────────────
import psutil

_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.pid")

def _check_single_instance():
    my_pid = os.getpid()
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            if old_pid != my_pid and psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if "python" in proc.name().lower():
                    print(f"[경고] 이미 실행 중인 봇 프로세스 발견 (PID {old_pid}) → 종료 후 재시작합니다.")
                    proc.terminate()
                    proc.wait(timeout=5)
        except Exception:
            pass
    with open(_PID_FILE, "w") as f:
        f.write(str(my_pid))

_check_single_instance()
# ───────────────────────────────────────────────────────────────────

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"[봇 시작] {bot.user} (ID: {bot.user.id})")
    print(f"[서버 수] {len(bot.guilds)}")


async def main():
    async with bot:
        await bot.load_extension("cogs.verification")
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
