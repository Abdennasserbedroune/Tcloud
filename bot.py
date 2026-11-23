"""
TG Store — un bot Telegram qui range tes fichiers par catégorie
dans des canaux privés et te permet de les retrouver avec des boutons.

Aucune limite de 50 Mo : le bot ne télécharge ni ne ré-uploade jamais
les fichiers, il les copie (copy_message) d'un chat à l'autre.
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # évite de logger l'URL avec le token
log = logging.getLogger("tgstore")

# ---------------------------------------------------------------- config

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
def parse_ids(raw: str) -> list[int]:
    """Extrait tous les nombres d'une valeur, même mal formatée
    (espaces, guillemets, retour à la ligne, "OWNER_IDS=123"...)."""
    return [int(x) for x in re.findall(r"-?\d{5,}", raw or "")]


OWNER_IDS = set(parse_ids(os.environ.get("OWNER_IDS", "")))
DB_PATH = os.environ.get("DB_PATH", "tgstore.db")
PAGE_SIZE = 8

# Canal privé où l'index (la base SQLite) est sauvegardé, indispensable sur
# GitHub Actions car le disque du runner est effacé à chaque exécution.
_index_ids = parse_ids(os.environ.get("CHANNEL_INDEX", ""))

# Durée max d'une exécution en minutes (0 = illimité, pour un PC ou un VPS).
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "0") or 0)
BACKUP_EVERY_S = 30

# clé -> (libellé, variable d'env du canal)
CATEGORIES = {
    "images": ("🖼 Images", "CHANNEL_IMAGES"),
    "videos": ("🎬 Vidéos", "CHANNEL_VIDEOS"),
    "jeux": ("🎮 Jeux", "CHANNEL_JEUX"),
    "docs": ("📄 Documents", "CHANNEL_DOCS"),
}


def as_channel_id(value: int) -> int:
    """Un ID de canal commence toujours par -100. On répare les formes courantes :
    1234567890 ou -1234567890 (copié depuis l'URL de web.telegram.org) → -1001234567890."""
    text = str(value)
    if text.startswith("-100"):
        return value
    return int("-100" + text.lstrip("-"))


INDEX_CHANNEL = as_channel_id(_index_ids[0]) if _index_ids else None


def channel_of(cat: str) -> int | None:
    ids = parse_ids(os.environ.get(CATEGORIES[cat][1], ""))
    return as_channel_id(ids[0]) if ids else None


GAME_EXT = {".apk", ".xapk", ".exe", ".msi", ".iso", ".zip", ".rar", ".7z", ".nsp", ".xci", ".cia", ".pkg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}

# ---------------------------------------------------------------- base de données


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                status          TEXT NOT NULL DEFAULT 'pending',   -- pending | done
                category        TEXT,
                file_name       TEXT NOT NULL,
                size_bytes      INTEGER,
                mime_type       TEXT,
                file_unique_id  TEXT,
                src_chat_id     INTEGER NOT NULL,
                src_message_id  INTEGER NOT NULL,
                channel_id      INTEGER,
                message_id      INTEGER,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_cat ON files(status, category, id);
            CREATE INDEX IF NOT EXISTS idx_files_uid ON files(file_unique_id);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """
        )


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- sauvegarde de l'index


def mark_dirty(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data["dirty"] = True


async def restore_index(app: Application) -> None:
    """Au démarrage : récupère la dernière base épinglée dans le canal index."""
    if INDEX_CHANNEL is None:
        return
    # Si cet appel échoue, on laisse l'exception arrêter le bot : démarrer avec une
    # base vide puis l'envoyer écraserait l'index existant.
    chat = await app.bot.get_chat(INDEX_CHANNEL)
    pinned = chat.pinned_message
    if pinned and pinned.document:
        tg_file = await pinned.document.get_file()
        await tg_file.download_to_drive(DB_PATH)
        log.info("Index restauré depuis le canal.")
    else:
        log.info("Aucun index épinglé : nouveau départ.")


async def backup_index(app: Application, new_message: bool = False) -> None:
    """Envoie la base dans le canal index. new_message=True crée un nouveau point
    de sauvegarde épinglé (l'ancien reste comme historique), sinon on le remplace."""
    if INDEX_CHANNEL is None:
        return
    snapshot = DB_PATH + ".snapshot"
    src, dst = db(), sqlite3.connect(snapshot)
    try:
        src.backup(dst)  # copie cohérente même si la base est en cours d'utilisation
    finally:
        src.close()
        dst.close()
    caption = f"🗂 Index TG Store — {datetime.now():%d/%m/%Y %H:%M}"
    msg_id = app.bot_data.get("index_msg")
    with open(snapshot, "rb") as fh:
        if msg_id and not new_message:
            try:
                await app.bot.edit_message_media(
                    chat_id=INDEX_CHANNEL, message_id=msg_id,
                    media=InputMediaDocument(fh, filename="tgstore.db", caption=caption),
                )
                app.bot_data["dirty"] = False
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    app.bot_data["dirty"] = False
                    return
                fh.seek(0)  # message supprimé : on en crée un nouveau
        sent = await app.bot.send_document(INDEX_CHANNEL, fh, filename="tgstore.db", caption=caption)
    await app.bot.pin_chat_message(INDEX_CHANNEL, sent.message_id, disable_notification=True)
    app.bot_data["index_msg"] = sent.message_id
    app.bot_data["dirty"] = False


async def backup_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(BACKUP_EVERY_S)
        if app.bot_data.get("dirty"):
            try:
                await backup_index(app)
            except Exception:
                log.exception("Échec de la sauvegarde de l'index (nouvel essai plus tard)")


async def stop_after(app: Application, minutes: int) -> None:
    await asyncio.sleep(minutes * 60)
    log.info("Durée max atteinte, arrêt propre.")
    app.stop_running()


async def post_init(app: Application) -> None:
    await restore_index(app)
    init_db()
    await backup_index(app, new_message=True)  # point de sauvegarde de début d'exécution
    # asyncio.create_task : la boucle tourne déjà ici (évite le PTBUserWarning)
    app.bot_data["tasks"] = [asyncio.create_task(backup_loop(app))]
    if MAX_RUNTIME_MIN > 0:
        app.bot_data["tasks"].append(asyncio.create_task(stop_after(app, MAX_RUNTIME_MIN)))


async def post_stop(app: Application) -> None:
    for task in app.bot_data.get("tasks", []):
        task.cancel()
    if app.bot_data.get("dirty"):
        await backup_index(app)
        log.info("Index sauvegardé avant l'arrêt.")


# ---------------------------------------------------------------- utilitaires


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} o"


def extract_file(msg: Message) -> dict | None:
    """Renvoie les infos du fichier contenu dans le message, ou None."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if msg.document:
        d = msg.document
        return dict(name=d.file_name or f"fichier_{stamp}", size=d.file_size,
                    mime=d.mime_type or "", uid=d.file_unique_id)
    if msg.photo:
        p = msg.photo[-1]  # la plus grande résolution
        return dict(name=f"photo_{stamp}.jpg", size=p.file_size, mime="image/jpeg", uid=p.file_unique_id)
    if msg.video:
        v = msg.video
        return dict(name=v.file_name or f"video_{stamp}.mp4", size=v.file_size,
                    mime=v.mime_type or "video/mp4", uid=v.file_unique_id)
    if msg.animation:
        a = msg.animation
        return dict(name=a.file_name or f"gif_{stamp}.mp4", size=a.file_size,
                    mime=a.mime_type or "video/mp4", uid=a.file_unique_id)
    if msg.audio:
        a = msg.audio
        return dict(name=a.file_name or a.title or f"audio_{stamp}", size=a.file_size,
                    mime=a.mime_type or "audio/mpeg", uid=a.file_unique_id)
    return None


def detect_category(name: str, mime: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    if mime.startswith("image/") or ext in IMAGE_EXT:
        return "images"
    if mime.startswith("video/") or ext in VIDEO_EXT:
        return "videos"
    if ext in GAME_EXT or "android.package-archive" in mime:
        return "jeux"
    return "docs"


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in OWNER_IDS)


def main_menu() -> tuple[str, InlineKeyboardMarkup]:
    with db() as conn:
        counts = dict(
            conn.execute(
                "SELECT category, COUNT(*) FROM files WHERE status='done' GROUP BY category"
            ).fetchall()
        )
    rows, line = [], []
    for key, (label, _) in CATEGORIES.items():
        line.append(InlineKeyboardButton(f"{label} ({counts.get(key, 0)})", callback_data=f"ls:{key}:0"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.insert(0, [InlineKeyboardButton("📤 Envoyer des fichiers", callback_data="send")])
    auto = get_setting("auto", "0") == "1"
    target = get_setting("target", "")
    text = (
        "📦 <b>Ton stockage</b>\n\n"
        "• 📤 <b>Envoyer</b> : choisis la catégorie, puis envoie tes fichiers.\n"
        "• Ou envoie directement un fichier, je te demanderai où le ranger.\n"
        "• Clique sur une catégorie pour parcourir.\n"
        "• <code>/search mot</code> pour chercher, /check pour tester les canaux.\n\n"
        + (f"📥 Envoi en cours vers : {CATEGORIES[target][0]}\n" if target in CATEGORIES else "")
        + f"Mode auto : {'✅ activé' if auto else '❌ désactivé'} (/auto)"
    )
    return text, InlineKeyboardMarkup(rows)


def send_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"target:{key}")]
            for key, (label, _) in CATEGORIES.items()]
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def done_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✋ Terminer l'envoi", callback_data="target:off")]])


# ---------------------------------------------------------------- rangement


async def store_file(context: ContextTypes.DEFAULT_TYPE, row_id: int, cat: str) -> str:
    """Copie le fichier dans le canal de la catégorie et finalise l'index."""
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (row_id,)).fetchone()
    if not row:
        return "❌ Fichier introuvable, renvoie-le."
    if row["status"] == "done":
        return f"ℹ️ Déjà rangé dans {CATEGORIES[row['category']][0]}."

    channel = channel_of(cat)
    if channel is None:
        return f"❌ Aucun canal configuré pour {CATEGORIES[cat][0]} ({CATEGORIES[cat][1]} dans .env)."

    caption = (
        f"#{cat}\n📄 {html.escape(row['file_name'])}\n💾 {human_size(row['size_bytes'])}"
    )
    try:
        copied = await context.bot.copy_message(
            chat_id=channel,
            from_chat_id=row["src_chat_id"],
            message_id=row["src_message_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except (BadRequest, Forbidden) as exc:
        return (
            f"❌ Impossible d'envoyer dans {CATEGORIES[cat][0]} : <i>{html.escape(exc.message)}</i>\n\n"
            f"ID utilisé : <code>{channel}</code>\n"
            "Vérifie que le bot est <b>administrateur</b> de ce canal et que l'ID est le bon. "
            "Tape /check pour tester tous les canaux. Ton fichier reste en attente : "
            "renvoie-le une fois corrigé."
        )
    with db() as conn:
        conn.execute(
            "UPDATE files SET status='done', category=?, channel_id=?, message_id=? WHERE id=?",
            (cat, channel, copied.message_id, row_id),
        )
    mark_dirty(context)
    return f"✅ <b>{html.escape(row['file_name'])}</b> rangé dans {CATEGORIES[cat][0]}."


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    info = extract_file(msg)
    if not info:
        return
    if msg.caption:  # la légende que tu écris devient le nom affiché
        ext = os.path.splitext(info["name"])[1]
        info["name"] = msg.caption.strip()[:120] + ("" if msg.caption.strip().endswith(ext) else ext)

    with db() as conn:
        dup = conn.execute(
            "SELECT category FROM files WHERE file_unique_id=? AND status='done' LIMIT 1",
            (info["uid"],),
        ).fetchone()
        if dup:
            await msg.reply_text(f"ℹ️ Ce fichier est déjà rangé dans {CATEGORIES[dup['category']][0]}.")
            return
        cur = conn.execute(
            "INSERT INTO files(file_name, size_bytes, mime_type, file_unique_id, "
            "src_chat_id, src_message_id, created_at) VALUES(?,?,?,?,?,?,?)",
            (info["name"], info["size"], info["mime"], info["uid"],
             msg.chat_id, msg.message_id, datetime.now().isoformat(timespec="seconds")),
        )
        row_id = cur.lastrowid
    mark_dirty(context)

    guess = detect_category(info["name"], info["mime"])

    target = get_setting("target", "")
    if target in CATEGORIES:  # catégorie choisie avant l'envoi
        await msg.reply_text(await store_file(context, row_id, target),
                             parse_mode=ParseMode.HTML, reply_markup=done_button())
        return

    if get_setting("auto", "0") == "1":
        await msg.reply_text(await store_file(context, row_id, guess), parse_mode=ParseMode.HTML)
        return

    others = [InlineKeyboardButton(CATEGORIES[k][0], callback_data=f"up:{k}:{row_id}")
              for k in CATEGORIES if k != guess]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ {CATEGORIES[guess][0]}", callback_data=f"up:{guess}:{row_id}")],
        others,
        [InlineKeyboardButton("✖️ Annuler", callback_data=f"cancel:{row_id}")],
    ])
    await msg.reply_text(
        f"📥 <b>{html.escape(info['name'])}</b> ({human_size(info['size'])})\n"
        f"Catégorie détectée : {CATEGORIES[guess][0]}",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------- navigation


def list_page(cat: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='done' AND category=?", (cat,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, file_name, size_bytes FROM files WHERE status='done' AND category=? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (cat, PAGE_SIZE, page * PAGE_SIZE),
        ).fetchall()
    pages = max(1, -(-total // PAGE_SIZE))
    buttons = [
        [InlineKeyboardButton(f"{r['file_name'][:40]} · {human_size(r['size_bytes'])}",
                              callback_data=f"get:{r['id']}")]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ls:{cat}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ls:{cat}:{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    text = f"{CATEGORIES[cat][0]} — {total} fichier(s)" + ("" if total else "\n\nRien ici pour l'instant.")
    return text, InlineKeyboardMarkup(buttons)


async def send_back(update: Update, context: ContextTypes.DEFAULT_TYPE, row_id: int) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=? AND status='done'", (row_id,)).fetchone()
    chat_id = update.effective_chat.id
    if not row:
        await context.bot.send_message(chat_id, "❌ Fichier introuvable dans l'index.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Retirer", callback_data=f"del:{row_id}"),
        InlineKeyboardButton("🏠 Menu", callback_data="menu"),
    ]])
    try:
        await context.bot.copy_message(chat_id, row["channel_id"], row["message_id"], reply_markup=keyboard)
    except BadRequest:
        await context.bot.send_message(
            chat_id, "⚠️ Le message n'existe plus dans le canal (supprimé à la main ?).",
            reply_markup=keyboard,
        )


# ---------------------------------------------------------------- boutons


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update):
        await query.answer()
        return
    data = query.data or ""
    action, *args = data.split(":")
    try:
        await query.answer()
    except BadRequest:
        pass  # clic fait pendant que le bot était arrêté : trop ancien pour y répondre, on traite quand même

    if action == "menu":
        text, kb = main_menu()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML) \
            if query.message.text else await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    elif action == "ls" and args[0] in CATEGORIES:
        text, kb = list_page(args[0], int(args[1]))
        if query.message.text:
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await query.message.reply_text(text, reply_markup=kb)

    elif action == "get":
        await send_back(update, context, int(args[0]))

    elif action == "up" and args[0] in CATEGORIES:
        result = await store_file(context, int(args[1]), args[0])
        await query.edit_message_text(result, parse_mode=ParseMode.HTML)

    elif action == "send":
        await query.edit_message_text("📤 Dans quelle catégorie veux-tu envoyer ?", reply_markup=send_menu()) \
            if query.message.text else await query.message.reply_text(
                "📤 Dans quelle catégorie veux-tu envoyer ?", reply_markup=send_menu())

    elif action == "target":
        if args[0] in CATEGORIES:
            set_setting("target", args[0])
            mark_dirty(context)
            await query.edit_message_text(
                f"📥 OK, envoie tes fichiers : ils iront tous dans {CATEGORIES[args[0]][0]}.\n"
                "Tu peux en envoyer plusieurs d'un coup.",
                reply_markup=done_button(),
            )
        else:
            set_setting("target", "")
            mark_dirty(context)
            text, kb = main_menu()
            await query.message.reply_text("✋ Envoi terminé.\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)

    elif action == "cancel":
        with db() as conn:
            conn.execute("DELETE FROM files WHERE id=? AND status='pending'", (int(args[0]),))
        mark_dirty(context)
        await query.edit_message_text("✖️ Annulé.")

    elif action == "del":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Oui, supprimer", callback_data=f"delok:{args[0]}"),
            InlineKeyboardButton("Non", callback_data="menu"),
        ]])
        await query.message.reply_text("Supprimer ce fichier du canal et de l'index ?", reply_markup=kb)

    elif action == "delok":
        row_id = int(args[0])
        with db() as conn:
            row = conn.execute("SELECT * FROM files WHERE id=?", (row_id,)).fetchone()
        if row and row["channel_id"]:
            try:
                await context.bot.delete_message(row["channel_id"], row["message_id"])
            except BadRequest:
                pass  # déjà supprimé ou trop ancien : on nettoie quand même l'index
        with db() as conn:
            conn.execute("DELETE FROM files WHERE id=?", (row_id,))
        mark_dirty(context)
        await query.edit_message_text("🗑 Supprimé.")


# ---------------------------------------------------------------- commandes


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = main_menu()
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    new = "0" if get_setting("auto", "0") == "1" else "1"
    set_setting("auto", new)
    mark_dirty(context)
    await update.effective_message.reply_text(
        "✅ Mode auto activé : les fichiers sont rangés directement dans la catégorie détectée."
        if new == "1" else "❌ Mode auto désactivé : je te demanderai la catégorie."
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    term = " ".join(context.args).strip()
    if not term:
        await update.effective_message.reply_text("Usage : /search mot")
        return
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with db() as conn:
        rows = conn.execute(
            "SELECT id, file_name, category FROM files WHERE status='done' "
            "AND file_name LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 20",
            (f"%{escaped}%",),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(f"Aucun résultat pour « {term} ».")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{CATEGORIES[r['category']][0].split()[0]} {r['file_name'][:45]}",
                               callback_data=f"get:{r['id']}")] for r in rows]
        + [[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]
    )
    await update.effective_message.reply_text(f"🔎 {len(rows)} résultat(s) :", reply_markup=kb)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Ton user ID : <code>{update.effective_user.id}</code>",
                                              parse_mode=ParseMode.HTML)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Teste l'accès du bot à chaque canal configuré."""
    me = await context.bot.get_me()
    targets = [(CATEGORIES[k][0], CATEGORIES[k][1], channel_of(k)) for k in CATEGORIES]
    targets.append(("🗂 Index", "CHANNEL_INDEX", INDEX_CHANNEL))
    lines = ["🔧 <b>Test des canaux</b>\n"]
    for label, secret, cid in targets:
        if cid is None:
            lines.append(f"⚪ {label} : secret <code>{secret}</code> vide")
            continue
        try:
            chat = await context.bot.get_chat(cid)
            member = await context.bot.get_chat_member(cid, me.id)
            if member.status == "administrator":
                lines.append(f"✅ {label} : « {html.escape(chat.title or '')} » (admin)")
            else:
                lines.append(f"⚠️ {label} : « {html.escape(chat.title or '')} », mais le bot n'est pas admin")
        except TelegramError as exc:
            lines.append(f"❌ {label} : <code>{cid}</code> → {html.escape(exc.message)}")
    lines.append(
        "\nSi un canal est en ❌ : ajoute @" + html.escape(me.username or "ton_bot")
        + " comme administrateur du canal, puis écris /id dans le canal pour avoir le bon ID."
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message privé d'un compte non autorisé : on lui donne son ID au lieu de rester muet."""
    await update.effective_message.reply_text(
        "⛔ Ce bot est privé.\n"
        f"Ton ID Telegram : <code>{update.effective_user.id}</code>\n\n"
        "Si c'est ton bot, mets exactement ce nombre dans le secret GitHub OWNER_IDS, "
        "puis relance le workflow.",
        parse_mode=ParseMode.HTML,
    )


async def channel_id_helper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Écris /id dans un canal où le bot est admin : il répond avec l'ID du canal."""
    post = update.channel_post
    if post and post.text and post.text.strip().startswith("/id"):
        await post.reply_text(f"ID de ce canal : {post.chat.id}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Erreur pendant le traitement d'une mise à jour", exc_info=context.error)


# ---------------------------------------------------------------- lancement


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN manquant (fichier .env ou secrets GitHub)")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    # /myid et /id fonctionnent pour tout le monde, pour faciliter la configuration
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_id_helper))

    if not OWNER_IDS:
        log.warning("OWNER_IDS vide : envoie /myid au bot puis remplis .env. Le bot reste verrouillé.")
    owner = filters.User(user_id=OWNER_IDS) & filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(CommandHandler("menu", cmd_start, filters=owner))
    app.add_handler(CommandHandler("auto", cmd_auto, filters=owner))
    app.add_handler(CommandHandler("search", cmd_search, filters=owner))
    app.add_handler(CommandHandler("check", cmd_check, filters=owner))
    app.add_handler(MessageHandler(owner & filters.ATTACHMENT, on_file))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, cmd_start))  # "salut" → menu
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~owner, deny))
    app.add_error_handler(on_error)

    configured = [k for k in CATEGORIES if channel_of(k) is not None]
    log.info(
        "Bot démarré : %d propriétaire(s) autorisé(s), canaux configurés : %s, index : %s.",
        len(OWNER_IDS), ", ".join(configured) or "aucun", "oui" if INDEX_CHANNEL else "non",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
