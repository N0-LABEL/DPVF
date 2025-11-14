# gov_house_bot.py
# Discord.py 2.x
import os
import asyncio
import uuid
import datetime
import json
import discord
from discord.ext import commands

# =========================
# Конфигурация — заполните!
# =========================
TOKEN = os.getenv("DISCORD_TOKEN") or ""
GUILD_ID = 1225075859333845154  # ваш сервер
VOICE_CHANNEL_ID =GUILD_ID = 1225075859333845154  # ваш сервер
VOICE_CHANNEL_ID = 1289694911234310155  # целевой войс
NEWS_CHANNEL_ID = 1301325369919410196  # канал с новостями от вебхуков
SOUND_FILE = "notification.mp3"        # локальный mp3/wav
PETITIONS_FILE = "petitions.json"      # тут храним петиции

# Роли-одобряющие
APPROVER_ROLE_IDS = {
    1226236176298541196,  # Президент
    1225212269541986365,  # Госбезопасность
}

# Роли статуса (ранжирование: Visa < PMJ < Grazhd)
STATUS_ROLE_IDS = {
    1282740488474067039: "Виза",
    1287407480045043814: "ПМЖ",
    1289911579097436232: "Гражданство",
}

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# Модель и хранилище петиций
# =========================

class PetitionState:
    def __init__(self, petition_id: str, author_id: int, guild_id: int):
        self.id = petition_id
        self.author_id = author_id
        self.guild_id = guild_id
        self.status = "pending"  # pending | accepted | rejected | finished
        self.accepted_by: int | None = None
        self.rejected_by: int | None = None
        self.approvers: set[int] = set()
        # approver_id -> (channel_id, message_id)
        self.approver_messages: dict[int, tuple[int, int]] = {}
        self.lock = asyncio.Lock()


petitions: dict[str, PetitionState] = {}
voice_rejoin_lock = asyncio.Lock()

# Цвета для разных статусов петиции
PENDING_COLOR = discord.Color.light_grey()
ACCEPTED_COLOR = discord.Color.from_rgb(255, 255, 128)   # мягкий жёлтый
REJECTED_COLOR = discord.Color.from_rgb(255, 128, 128)   # мягкий красный
FINISHED_COLOR = discord.Color.from_rgb(128, 255, 170)   # мягкий зелёный


def serialize_petition(p: PetitionState) -> dict:
    return {
        "id": p.id,
        "author_id": p.author_id,
        "guild_id": p.guild_id,
        "status": p.status,
        "accepted_by": p.accepted_by,
        "rejected_by": p.rejected_by,
        "approvers": list(p.approvers),
        # ключи в JSON должны быть строками
        "approver_messages": {
            str(k): [ch_id, msg_id] for k, (ch_id, msg_id) in p.approver_messages.items()
        },
    }


def save_petitions():
    data = {pid: serialize_petition(p) for pid, p in petitions.items()}
    try:
        with open(PETITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[storage] save_petitions error: {e}")


def load_petitions():
    if not os.path.exists(PETITIONS_FILE):
        return
    try:
        with open(PETITIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[storage] load_petitions error: {e}")
        return

    for pid, pdata in raw.items():
        p = PetitionState(
            petition_id=pdata.get("id", pid),
            author_id=pdata.get("author_id"),
            guild_id=pdata.get("guild_id"),
        )
        p.status = pdata.get("status", "pending")
        p.accepted_by = pdata.get("accepted_by")
        p.rejected_by = pdata.get("rejected_by")
        p.approvers = set(pdata.get("approvers", []))
        p.approver_messages = {}
        for k, v in pdata.get("approver_messages", {}).items():
            try:
                appr_id = int(k)
                ch_id, msg_id = v
                p.approver_messages[appr_id] = (int(ch_id), int(msg_id))
            except Exception:
                continue
        petitions[p.id] = p


def apply_status_to_embed(
    emb: discord.Embed,
    p: PetitionState,
    guild: discord.Guild | None
) -> discord.Embed:
    if p.status == "pending":
        status_text = "Новая (ожидает решения)"
        color = PENDING_COLOR
    elif p.status == "accepted":
        acc_name = "неизвестно"
        if guild and p.accepted_by:
            m = guild.get_member(p.accepted_by)
            if m:
                acc_name = m.display_name
        status_text = f"Принята на рассмотрение ({acc_name})"
        color = ACCEPTED_COLOR
    elif p.status == "rejected":
        rej_name = "неизвестно"
        if guild and p.rejected_by:
            m = guild.get_member(p.rejected_by)
            if m:
                rej_name = m.display_name
        status_text = f"Отклонена ({rej_name})"
        color = REJECTED_COLOR
    elif p.status == "finished":
        fin_name = "неизвестно"
        if guild and p.accepted_by:
            m = guild.get_member(p.accepted_by)
            if m:
                fin_name = m.display_name
        status_text = f"Исполнена ({fin_name})"
        color = FINISHED_COLOR
    else:
        status_text = "Неизвестный статус"
        color = PENDING_COLOR

    index = None
    for i, field in enumerate(emb.fields):
        if field.name == "Статус петиции":
            index = i
            break

    if index is not None:
        emb.set_field_at(index, name="Статус петиции", value=status_text, inline=False)
    else:
        emb.add_field(name="Статус петиции", value=status_text, inline=False)

    emb.color = color
    return emb


# =========================
# Утилиты
# =========================

def human_status(member: discord.Member) -> str:
    power = {"Виза": 1, "ПМЖ": 2, "Гражданство": 3}
    found = []
    for r in member.roles:
        if r.id in STATUS_ROLE_IDS:
            found.append(STATUS_ROLE_IDS[r.id])
    if not found:
        return "Нет статуса"
    return sorted(found, key=lambda s: power.get(s, 0))[-1]


def member_has_any_role(member: discord.Member, role_ids: set[int]) -> bool:
    ids = {r.id for r in member.roles}
    return bool(ids & role_ids)


async def ensure_voice_in_guild(guild: discord.Guild) -> discord.VoiceClient | None:
    target = guild.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(target, discord.VoiceChannel):
        return None
    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id != target.id:
            await vc.move_to(target)
        return vc
    try:
        vc = await target.connect()
        return vc
    except Exception as e:
        print(f"[voice] ensure_voice_in_guild error: {e}")
        return None


async def play_sound_in_guild(guild: discord.Guild):
    vc = await ensure_voice_in_guild(guild)
    if not vc:
        return
    try:
        if vc.is_playing():
            vc.stop()
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(SOUND_FILE))
        vc.play(source)
    except Exception as e:
        print(f"[voice] play_sound error: {e}")


def base_petition_embed(
    title: str,
    reason: str,
    details: str,
    petition_id: str,
    author: discord.Member
) -> discord.Embed:
    emb = discord.Embed(
        title=title,
        description=f"ID: {petition_id}",
        timestamp=datetime.datetime.utcnow(),
    )
    emb.add_field(name="Тема", value=reason[:256] or "-", inline=False)
    emb.add_field(name="Подробности", value=details[:1024] or "-", inline=False)
    emb.add_field(name="Заявитель", value=f"{author.mention} ({author.id})", inline=False)
    emb.add_field(name="Статус заявителя", value=human_status(author), inline=True)
    joined = author.joined_at.strftime("%Y-%m-%d") if author.joined_at else "—"
    emb.add_field(name="На сервере с", value=joined, inline=True)
    if author.display_avatar:
        emb.set_thumbnail(url=author.display_avatar.url)
    return emb


# =========================
# View’ы для одобряющих
# =========================

class ApproverView(discord.ui.View):
    def __init__(self, petition_id: str):
        super().__init__(timeout=None)  # timeout=None для persistent view
        self.petition_id = petition_id

    @discord.ui.button(
        label="Принять на рассмотрение",
        style=discord.ButtonStyle.primary,
        custom_id="petition_accept"
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = petitions.get(self.petition_id)
        if not p:
            await interaction.response.send_message("Петиция недоступна.", ephemeral=True)
            return

        async with p.lock:
            guild = bot.get_guild(p.guild_id)
            if not guild:
                await interaction.response.send_message("Сервер недоступен.", ephemeral=True)
                return
            member = guild.get_member(interaction.user.id)
            if not member or not member_has_any_role(member, APPROVER_ROLE_IDS):
                await interaction.response.send_message("Нет прав на обработку.", ephemeral=True)
                return
            if p.status != "pending":
                await interaction.response.send_message("Петиция уже обработана.", ephemeral=True)
                return

            p.status = "accepted"
            p.accepted_by = interaction.user.id

            # Обновить всем остальным: дизейбл + статус/цвет
            for approver_id, (ch_id, msg_id) in list(p.approver_messages.items()):
                try:
                    ch = await bot.fetch_channel(ch_id)
                    msg = await ch.fetch_message(msg_id)
                    emb = msg.embeds[0] if msg.embeds else None
                    if emb:
                        emb = apply_status_to_embed(emb, p, guild)
                    if approver_id != interaction.user.id:
                        await msg.edit(embed=emb, view=None)
                except Exception as e:
                    print(f"[petition] accept update error: {e}")

            # На сообщении принявшего — кнопка "Завершить"
            try:
                ch_id, msg_id = p.approver_messages.get(interaction.user.id, (None, None))
                if ch_id and msg_id:
                    ch = await bot.fetch_channel(ch_id)
                    msg = await ch.fetch_message(msg_id)
                    emb = msg.embeds[0] if msg.embeds else None
                    if emb:
                        emb = apply_status_to_embed(emb, p, guild)
                    await msg.edit(embed=emb, view=FinishView(self.petition_id))
            except Exception as e:
                print(f"[petition] accept self message error: {e}")

            # Уведомления
            author = guild.get_member(p.author_id)
            acc_name = interaction.user.display_name
            for appr_id in p.approvers:
                if appr_id == interaction.user.id:
                    continue
                try:
                    user = await bot.fetch_user(appr_id)
                    await user.send(f"{acc_name} принял петицию № {p.id} на рассмотрение.")
                except Exception:
                    pass
            if author:
                try:
                    await author.send(f"Ваша петиция № {p.id} принята на рассмотрение.")
                except Exception:
                    pass

            save_petitions()
            await interaction.response.defer()

    @discord.ui.button(
        label="Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="petition_reject"
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = petitions.get(self.petition_id)
        if not p:
            await interaction.response.send_message("Петиция недоступна.", ephemeral=True)
            return

        async with p.lock:
            guild = bot.get_guild(p.guild_id)
            if not guild:
                await interaction.response.send_message("Сервер недоступен.", ephemeral=True)
                return
            member = guild.get_member(interaction.user.id)
            if not member or not member_has_any_role(member, APPROVER_ROLE_IDS):
                await interaction.response.send_message("Нет прав на обработку.", ephemeral=True)
                return
            if p.status != "pending":
                await interaction.response.send_message("Петиция уже обработана.", ephemeral=True)
                return

            p.status = "rejected"
            p.rejected_by = interaction.user.id

            # Дизейбл всем + обновить статус/цвет
            for approver_id, (ch_id, msg_id) in list(p.approver_messages.items()):
                try:
                    ch = await bot.fetch_channel(ch_id)
                    msg = await ch.fetch_message(msg_id)
                    emb = msg.embeds[0] if msg.embeds else None
                    if emb:
                        emb = apply_status_to_embed(emb, p, guild)
                    await msg.edit(embed=emb, view=None)
                except Exception as e:
                    print(f"[petition] reject update error: {e}")

            # Уведомления (кроме отклонившего)
            rej_name = interaction.user.display_name
            for appr_id in p.approvers:
                if appr_id == interaction.user.id:
                    continue
                try:
                    user = await bot.fetch_user(appr_id)
                    await user.send(f"{rej_name} отклонил петицию № {p.id}.")
                except Exception:
                    pass
            author = guild.get_member(p.author_id)
            if author:
                try:
                    await author.send(f"Ваша петиция № {p.id} отклонена.")
                except Exception:
                    pass

            save_petitions()
            await interaction.response.defer()


class FinishView(discord.ui.View):
    def __init__(self, petition_id: str):
        super().__init__(timeout=None)  # timeout=None для persistent view
        self.petition_id = petition_id

    @discord.ui.button(
        label="Завершить",
        style=discord.ButtonStyle.success,
        custom_id="petition_finish"
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = petitions.get(self.petition_id)
        if not p:
            await interaction.response.send_message("Петиция недоступна.", ephemeral=True)
            return

        async with p.lock:
            guild = bot.get_guild(p.guild_id)
            if not guild:
                await interaction.response.send_message("Сервер недоступен.", ephemeral=True)
                return
            if p.status != "accepted" or p.accepted_by != interaction.user.id:
                await interaction.response.send_message("Только принявший может завершить.", ephemeral=True)
                return

            p.status = "finished"

            # Дизейбл текущее сообщение + обновить статус/цвет
            try:
                ch_id, msg_id = p.approver_messages.get(interaction.user.id, (None, None))
                if ch_id and msg_id:
                    ch = await bot.fetch_channel(ch_id)
                    msg = await ch.fetch_message(msg_id)
                    emb = msg.embeds[0] if msg.embeds else None
                    if emb:
                        emb = apply_status_to_embed(emb, p, guild)
                    await msg.edit(embed=emb, view=None)
            except Exception as e:
                print(f"[petition] finish update error: {e}")

            author = guild.get_member(p.author_id)
            if author:
                try:
                    await author.send(f"Петиция № {p.id} успешно исполнена.")
                except Exception:
                    pass

            # Удаляем петицию из памяти и JSON
            petitions.pop(self.petition_id, None)
            save_petitions()

            await interaction.response.defer()


# =========================
# Modal для /petition
# =========================

class PetitionModal(discord.ui.Modal, title="Подача петиции"):
    reason = discord.ui.TextInput(
        label="Причина петиции (тема)",
        style=discord.TextStyle.short,
        max_length=200,
        required=True
    )
    details = discord.ui.TextInput(
        label="Подробное описание",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is not None:
            await interaction.response.send_message("Команду используйте в ЛС с ботом.", ephemeral=True)
            return

        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("Сервер недоступен.", ephemeral=True)
            return

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                member = None
        if not member:
            await interaction.response.send_message("Вы не являетесь участником сервера.", ephemeral=True)
            return

        status = human_status(member)
        if status == "Нет статуса":
            await interaction.response.send_message(
                "Петицию могут подавать только участники со статусом (Виза/ПМЖ/Гражданство).",
                ephemeral=True
            )
            return

        petition_id = uuid.uuid4().hex[:8].upper()
        p = PetitionState(petition_id, member.id, guild.id)
        petitions[petition_id] = p

        await interaction.response.send_message(
            f"Ваша петиция № {petition_id} на рассмотрении.",
            ephemeral=True
        )

        emb = base_petition_embed(
            "Новая петиция",
            str(self.reason),
            str(self.details),
            petition_id,
            member
        )
        emb = apply_status_to_embed(emb, p, guild)

        approvers: set[int] = set()
        for m in guild.members:
            if member_has_any_role(m, APPROVER_ROLE_IDS):
                approvers.add(m.id)
        p.approvers = approvers

        for appr_id in approvers:
            try:
                user = await bot.fetch_user(appr_id)
                dm = await user.create_dm()
                msg = await dm.send(embed=emb, view=ApproverView(petition_id))
                p.approver_messages[appr_id] = (dm.id, msg.id)
            except Exception:
                pass

        save_petitions()

        try:
            await interaction.user.send(
                f"Петиция № {petition_id} отправлена ответственным органам власти."
            )
        except Exception:
            pass


# =========================
# Slash-команды (только DM)
# =========================

@bot.tree.command(name="help", description="Инструкция по использованию бота")
async def help_cmd(interaction: discord.Interaction):
    if interaction.guild is not None:
        await interaction.response.send_message(
            "Эта команда доступна только в ЛС с ботом.",
            ephemeral=True
        )
        return
    emb = discord.Embed(
        title="Дом Правительства ВФ — помощь",
        description=(
            "• /petition — подать петицию\n"
            "• Обращайтесь к этому боту по любым юридическим вопросам.\n"
            "• Команды доступны только в ЛС"
        ),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="petition", description="Подать петицию на рассмотрение")
async def petition_cmd(interaction: discord.Interaction):
    if interaction.guild is not None:
        await interaction.response.send_message(
            "Эта команда доступна только в ЛС с ботом.",
            ephemeral=True
        )
        return
    await interaction.response.send_modal(PetitionModal(GUILD_ID))


# =========================
# События
# =========================

@bot.event
async def on_ready():
    # Регистрируем persistent view'ы для уже существующих петиций
    for p in petitions.values():
        if p.status == "pending":
            bot.add_view(ApproverView(p.id))
        elif p.status == "accepted":
            bot.add_view(FinishView(p.id))

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash sync error: {e}")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        vc = await ensure_voice_in_guild(guild)
        if vc:
            await play_sound_in_guild(guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):
    if not member.bot or not bot.user or member.id != bot.user.id:
        return

    guild = member.guild
    target = guild.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(target, discord.VoiceChannel):
        return

    before_ch = before.channel
    after_ch = after.channel

    if after_ch is None or after_ch.id != target.id:
        await asyncio.sleep(1)

        async with voice_rejoin_lock:
            vc = guild.voice_client
            current_ch = vc.channel if vc and vc.is_connected() else None
            if isinstance(current_ch, discord.VoiceChannel) and current_ch.id == target.id:
                return

            try:
                await ensure_voice_in_guild(guild)
            except Exception as e:
                print(f"[voice] on_voice_state_update rejoin error: {e}")

        return

    if after_ch and after_ch.id == target.id and (
        before_ch is None or before_ch.id != target.id
    ):
        await play_sound_in_guild(guild)


@bot.event
async def on_message(message: discord.Message):
    if (
        message.author.bot
        and message.webhook_id is not None
        and message.channel.id == NEWS_CHANNEL_ID
    ):
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await play_sound_in_guild(guild)


# =========================
# Старт: сначала грузим JSON
# =========================

if __name__ == "__main__":
    load_petitions()
    bot.run(TOKEN)