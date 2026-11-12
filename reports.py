"""Звіти: щоденний підсумок і перегляди через 24 год після публікації."""
from __future__ import annotations

import html
import logging
from datetime import datetime, time as dtime, timedelta

from config import NAMES, PLATFORMS, SHORT, fmt_slot, local_dt, now_ts
from meta import read_json, write_json

log = logging.getLogger("reports")
esc = html.escape


ICONS = {"youtube": "▶️", "tiktok": "🎵", "instagram": "📸", "facebook": "📘"}


def explain_error(platform: str, error: str) -> tuple[str, str, str]:
    """Технічна помилка → (ключ групи, зрозуміле пояснення, порада)."""
    e = str(error or "")
    low = e.lower()
    name = NAMES.get(platform, platform)
    if "токена tiktok" in low:
        return "tt_auth", "TikTok не підключено", "підключіть у панелі або вимкніть у «🔌 Платформи»"
    if "meta_page_token" in low or "meta_user_token" in low:
        return "meta_token", "Instagram/Facebook: не було токена Meta", "перевірте в панелі «🔍 Перевірити всі підключення»"
    if "авторизац" in low or "reauth" in low or "invalid_grant" in low or "oauth" in low and "expired" in low:
        return f"auth_{platform}", f"{name}: потрібна повторна авторизація", f"панель → вкладка {name}"
    if "processingfailed" in low or "request processing failed" in low:
        return "ig_processing", f"{name} не зміг обробити відео", "повторіть — програма зробить сумісну копію"
    if "winerror 2" in low or "winerror 3" in low or "no such file" in low or "cannot find the path" in low \
            or "не знайдено" in low:
        return "missing", "Файл відео не знайдено", "його перемістили чи видалили до публікації"
    if "quota" in low or "квот" in low:
        return f"quota_{platform}", f"{name}: вичерпано денну квоту", "повторіть завтра"
    if "timeout" in low or "timed out" in low or "connection" in low:
        return f"net_{platform}", f"{name}: проблема зі зв'язком", "повторіть пізніше"
    short = e.split("\n")[0][:120]
    return f"other_{platform}_{short[:40]}", f"{name}: {short}", ""


def _clean_title(cfg: dict, item: dict | None, fallback: str = "") -> str:
    """Назва без обов'язкових хештегів у кінці — щоб звіт читався легко."""
    import re
    title = _title(item) if item else fallback
    title = re.sub(r"(\s+#[\wЀ-ӿ]+)+\s*$", "", title or "").strip()
    return title or fallback or "без назви"


def daily_report(app, now: int | None = None, stats: dict | None = None) -> str:
    from config import fmt_when
    now = now or now_ts()
    cfg, db = app.cfg, app.db
    day = local_dt(cfg, now).date()
    start = int(datetime.combine(day, dtime(0, 0), cfg["tz"]).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), dtime(0, 0), cfg["tz"]).timestamp())
    ev = db.events(start, end)
    active = set(getattr(app, "platforms", {}) or PLATFORMS)

    # шортси, з якими сьогодні щось відбувалося (публікація або спроба)
    ids: list[int] = []
    for e in ev:
        if e["kind"] in ("published", "publish_failed", "platform_error") and e["item_id"] and e["item_id"] not in ids:
            ids.append(e["item_id"])
    items = {i: db.get_item(i) for i in ids}
    items = {i: it for i, it in items.items() if it}
    first_ts = {}
    for e in ev:
        if e["item_id"] in items and e["kind"] in ("published", "publish_failed"):
            first_ts.setdefault(e["item_id"], e["ts"])

    lines = [f"📊 <b>Підсумок дня · {day.strftime('%d.%m.%Y')}</b>"]
    ok_items = [it for it in items.values() if any(r.get("status") == "ok" for r in it["results"].values())]
    bad_items = [it for it in items.values() if it not in ok_items and it["status"] not in ("publishing",)]
    total_posts = sum(1 for it in ok_items for r in it["results"].values() if r.get("status") == "ok")
    head = f"🎬 Вийшло шортсів: <b>{len(ok_items)}</b> · публікацій на платформах: <b>{total_posts}</b>"
    if bad_items:
        head += f" · не вийшло: <b>{len(bad_items)}</b>"
    lines.append(head)

    problems: dict[str, dict] = {}
    for it in sorted(items.values(), key=lambda x: first_ts.get(x["id"], 0)):
        ok_parts, bad_parts = [], []
        for p in PLATFORMS:
            r = it["results"].get(p)
            if not r or p in it["disabled"]:
                continue
            if r.get("status") == "ok":
                u = r.get("url")
                ok_parts.append(f'{ICONS[p]} <a href="{esc(u)}">{NAMES[p]}</a>' if u else f"{ICONS[p]} {NAMES[p]}")
            elif p in active:
                key, text, hint = explain_error(p, r.get("error", ""))
                bad_parts.append(NAMES[p])
                g = problems.setdefault(key, {"text": text, "hint": hint, "ids": []})
                if it["id"] not in g["ids"]:
                    g["ids"].append(it["id"])
        t = it["posted_at"] or first_ts.get(it["id"])
        when = local_dt(cfg, t).strftime("%H:%M") if t else ""
        mark = "✅" if it in ok_items else "❌"
        lines.append(f"\n{mark} <b>{esc(_clean_title(cfg, it))}</b> <i>#{it['id']}{', ' + when if when else ''}</i>")
        if ok_parts:
            lines.append("    " + " · ".join(ok_parts))
        if bad_parts:
            lines.append("    ❌ не вийшло: " + ", ".join(bad_parts))
    if not items:
        lines.append("\nСьогодні нічого не публікувалося.")

    # помилки, які потім виправили повторні спроби
    fixed = 0
    for e in ev:
        if e["kind"] == "platform_error" and e["item_id"] in items:
            p = (e["details"] or {}).get("platform")
            if (items[e["item_id"]]["results"].get(p) or {}).get("status") == "ok":
                fixed += 1
    if problems or fixed:
        lines.append("\n⚠️ <b>Потребує уваги</b>" if problems else "\n⚠️ <b>Помилки</b>")
        for g in problems.values():
            n = len(g["ids"])
            lines.append(f"• <b>{esc(g['text'])}</b>: {n} шортс{'и' if 1 < n < 5 else 'ів' if n >= 5 else ''} "
                         f"({', '.join('#' + str(i) for i in g['ids'])})" + (f"\n   → {esc(g['hint'])}" if g["hint"] else ""))
        if problems:
            lines.append("  Повторити — «⚠️ Невдалі» в боті або в панелі.")
        if fixed:
            lines.append(f"🔁 Ще {fixed} помил{'ку' if fixed == 1 else 'ки' if fixed < 5 else 'ок'} "
                         f"виправили повторні спроби — там усе вийшло.")

    # розклад
    skipped = [e for e in ev if e["kind"] == "skipped"]
    moved = [e for e in ev if e["kind"] == "moved_to_skipped"]
    empty = [e for e in ev if e["kind"] == "empty_slot"]
    lines.append("\n🗓 <b>Розклад</b>")
    if skipped:
        names = ", ".join(esc(_clean_title(cfg, None, (e["details"] or {}).get("title", ""))) for e in skipped[:5])
        lines.append(f"⏭ Без підтвердження пропущено: {len(skipped)} ({names}) — їх перенесено в кінець черги")
    for e in moved:
        lines.append(f"🗑 {esc((e['details'] or {}).get('title', ''))} — пропущено 3 рази, перенесено в skipped/")
    if empty:
        lines.append(f"⬜ Порожніх слотів: {len(empty)} (у черзі не було відео)")
    queue = app.scheduler.projection(now)
    lines.append(f"📦 У черзі: <b>{len(queue)}</b>")
    for it, ts in queue[:5]:
        when = f" → {fmt_when(cfg, ts, now)}" if ts else ""
        lines.append(f"   • {esc(_clean_title(cfg, it))}{when}")
    if len(queue) > 5:
        lines.append(f"   … і ще {len(queue) - 5}")
    waiting = db.items("status IN ('notified','approved')", order="slot_at")
    for it in waiting:
        st = "підтверджено" if it["status"] == "approved" else "чекає на «🚀 Опублікувати»"
        lines.append(f"   • {esc(_clean_title(cfg, it))} → {fmt_slot(cfg, it['slot_at'], now) if it['slot_at'] else '—'} ({st})")
    if not queue and not waiting:
        free = app.scheduler.upcoming(now, 3)
        if free:
            lines.append("   Наступні слоти порожні — покладіть нові шортси в папку.")
    if stats:
        lines.append("")
        lines.append(analytics_text(app, stats, title=False))
    if cfg.get("dry_run"):
        lines.append("\n🧪 Тестовий режим — реальних публікацій немає")
    return "\n".join(lines)


def send_stats(app, item: dict) -> None:
    """Перегляди через 24 год: повідомлення в Telegram + запис у json."""
    from meta import load_meta

    db = app.db
    views: dict[str, int | None | str] = {}
    likes: dict[str, int] = {}
    for p in PLATFORMS:
        r = item["results"].get(p) or {}
        if r.get("status") != "ok" or p not in app.platforms:
            continue
        try:
            st = {} if app.cfg.get("dry_run") else (app.platforms[p].get_stats(r) or {})
            views[p] = st.get("views")
            if st.get("likes") is not None:
                likes[p] = st["likes"]
        except Exception as e:  # noqa: BLE001
            log.warning("Перегляди #%s %s: %s", item["id"], p, e)
            views[p] = f"помилка: {str(e)[:80]}"
    db.update_item(item["id"], stats_sent=1)
    try:
        title = load_meta(item["json"], item["title_override"]).yt_title
    except Exception:  # noqa: BLE001
        title = f"#{item['id']}"
    lines = [f"👁 <b>Статистика за 24 год</b>\n🎬 {esc(title)}"]
    total = 0
    for p, v in views.items():
        url = item["results"][p].get("url")
        name = f'<a href="{esc(url)}">{NAMES[p]}</a>' if url else NAMES[p]
        if isinstance(v, int):
            total += v
            lk = f" · ❤️ {fmt_n(likes[p])}" if p in likes else ""
            lines.append(f"{SHORT[p]} {name}: 👁 <b>{fmt_n(v)}</b>{lk}")
        elif v is None:
            lines.append(f"{SHORT[p]} {name}: н/д" + (" (приватний пост)" if p == "tiktok" else ""))
        else:
            lines.append(f"{SHORT[p]} {name}: {esc(v)}")
    lines.append(f"Разом: <b>{total:,}</b>".replace(",", " "))
    # окремого повідомлення немає — перегляди за добу з'являються в підписі відео
    results = dict(item["results"])
    for p, v in views.items():
        if isinstance(v, int):
            results[p] = {**results[p], "views24": v, **({"likes24": likes[p]} if p in likes else {})}
    db.update_item(item["id"], results=results)
    if hasattr(app.bot, "refresh"):
        app.bot.refresh(item["id"])
    try:
        data = read_json(item["json"])
        data.setdefault("posting", {})["views_24h"] = views
        write_json(item["json"], data)
    except Exception as e:  # noqa: BLE001
        log.warning("Не вдалося записати перегляди в json #%s: %s", item["id"], e)


# ======================================================================
# Аналітика: підписники, перегляди, лайки по кожній платформі
# ======================================================================
DAY = 24 * 3600


def fmt_n(n) -> str:
    return "н/д" if n is None else f"{int(n):,}".replace(",", " ")


def fmt_d(d) -> str:
    if not d:
        return ""
    return f" ({'+' if d > 0 else '−'}{fmt_n(abs(d))})"


def _title(item: dict) -> str:
    from pathlib import Path
    try:
        from meta import load_meta
        return load_meta(item["json"], item["title_override"]).yt_title
    except Exception:  # noqa: BLE001
        return Path(item["mp4"]).stem


def collect(app, days: int = 8) -> dict:
    """Знімає поточну статистику з платформ і зберігає знімок у БД (для різниці «за добу»)."""
    db, now = app.db, now_ts()
    out = {"ts": now, "accounts": {}, "posts": [], "errors": {}}
    dry = bool(app.cfg.get("dry_run"))
    for key, plat in app.platforms.items():
        if dry:
            continue
        try:
            acc = plat.account_stats() or {}
            out["accounts"][key] = acc
            db.x("INSERT INTO account_stats(platform, ts, followers, views, likes) VALUES (?,?,?,?,?)",
                 (key, now, acc.get("followers"), acc.get("views"), acc.get("likes")))
        except Exception as e:  # noqa: BLE001
            log.warning("Статистика акаунта %s: %s", key, e)
            out["errors"][key] = str(e)[:150]
    for item in db.items("status='posted' AND posted_at>=?", (now - days * DAY,), order="posted_at DESC"):
        title = _title(item)
        for key, res in item["results"].items():
            if res.get("status") != "ok" or key not in app.platforms or dry:
                continue
            try:
                st = app.platforms[key].get_stats(res) or {}
            except Exception as e:  # noqa: BLE001
                log.warning("Статистика #%s %s: %s", item["id"], key, e)
                st = {}
            if st:
                db.x("INSERT INTO post_stats(item_id, platform, ts, views, likes, comments) VALUES (?,?,?,?,?,?)",
                     (item["id"], key, now, st.get("views"), st.get("likes"), st.get("comments")))
            out["posts"].append({"item_id": item["id"], "title": title, "platform": key, "url": res.get("url"),
                                 "posted_at": item["posted_at"], **st})
    return out


def _prev_account(db, key: str, now: int) -> dict | None:
    return db.one("SELECT * FROM account_stats WHERE platform=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                  (key, now - 20 * 3600))


def _prev_post(db, item_id: int, key: str, now: int) -> dict | None:
    return db.one("SELECT * FROM post_stats WHERE item_id=? AND platform=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                  (item_id, key, now - 20 * 3600))


def _delta(cur, prev):
    return None if cur is None or prev is None else cur - prev


def analytics_text(app, stats: dict, title: bool = True) -> str:
    """По кожній платформі окремим блоком: підписники, перегляди й лайки за добу — з підписами, не цифри підряд."""
    db, now = app.db, stats["ts"]
    when = local_dt(app.cfg, now).strftime("%d.%m %H:%M")
    lines = [f"📈 <b>Аналітика за добу</b> · {when}"] if title else ["📈 <b>Платформи за добу</b>"]
    if app.cfg.get("dry_run"):
        lines.append("🧪 Тестовий режим — реальної статистики немає")
        return "\n".join(lines)
    if not app.platforms:
        lines.append("Немає підключених платформ")
    tot_v = tot_l = tot_f = 0
    any_prev = False
    for key in PLATFORMS:
        if key not in app.platforms:
            continue
        acc = stats["accounts"].get(key) or {}
        prev = _prev_account(db, key, now) or {}
        any_prev = any_prev or bool(prev)
        posts = [p for p in stats["posts"] if p["platform"] == key]
        dv = dl = 0
        has_prev_posts = False
        for p in posts:
            pp = _prev_post(db, p["item_id"], key, now)
            fresh = p.get("posted_at") and p["posted_at"] > now - DAY
            base_v = (pp or {}).get("views") if pp else (0 if fresh else None)
            base_l = (pp or {}).get("likes") if pp else (0 if fresh else None)
            if p.get("views") is not None and base_v is not None:
                dv += p["views"] - base_v
                has_prev_posts = True
            if p.get("likes") is not None and base_l is not None:
                dl += p["likes"] - base_l
        lines.append(f"\n{ICONS.get(key, '•')} <b>{NAMES[key]}</b>")
        if acc.get("followers") is not None:
            d = _delta(acc.get("followers"), prev.get("followers"))
            tot_f += d or 0
            lines.append(f"   👥 Підписники: <b>{fmt_n(acc['followers'])}</b>"
                         + (f" ({'+' if d >= 0 else '−'}{fmt_n(abs(d))} за добу)" if d is not None else ""))
        ch_views = _delta(acc.get("views"), prev.get("views"))  # YouTube: перегляди всього каналу за добу
        if ch_views is not None:
            lines.append(f"   📺 Перегляди каналу: <b>+{fmt_n(ch_views)}</b>")
        if has_prev_posts:
            tot_v += dv
            tot_l += dl
            lines.append(f"   🎬 Наші шортси: 👁 <b>+{fmt_n(dv)}</b> переглядів · ❤️ <b>+{fmt_n(dl)}</b> лайків")
        elif not posts:
            lines.append("   🎬 Наших шортсів тут ще немає")
        if key in stats["errors"]:
            lines.append("   ⚠️ " + esc(stats["errors"][key]))
        elif acc.get("followers") is None and not has_prev_posts and ch_views is None and posts:
            lines.append("   даних поки немає")
    if tot_v or tot_l or tot_f:
        lines.append(f"\n<b>Разом за добу:</b> 👁 +{fmt_n(tot_v)} · ❤️ +{fmt_n(tot_l)} · 👥 "
                     f"{'+' if tot_f >= 0 else '−'}{fmt_n(abs(tot_f))}")
    if not any_prev:
        lines.append("\n<i>Зміни «за добу» з'являться з наступного дня — зараз перший замір.</i>")
    return "\n".join(lines)


def weekly_top(app, stats: dict, limit: int = 5) -> str:
    """Лідери тижня: шортси за останні 7 днів, за сумою переглядів на всіх платформах."""
    now = stats["ts"]
    by_item: dict[int, dict] = {}
    for p in stats["posts"]:
        if not p.get("posted_at") or p["posted_at"] < now - 7 * DAY:
            continue
        it = by_item.setdefault(p["item_id"], {"title": p["title"], "views": 0, "likes": 0, "per": {}})
        it["views"] += p.get("views") or 0
        it["likes"] += p.get("likes") or 0
        it["per"][p["platform"]] = p
    lines = ["🏆 <b>Лідери тижня</b>"]
    if app.cfg.get("dry_run"):
        lines.append("🧪 Тестовий режим — реальної статистики немає")
        return "\n".join(lines)
    top = sorted(by_item.values(), key=lambda x: (x["views"], x["likes"]), reverse=True)[:limit]
    if not top:
        lines.append("За останні 7 днів опублікованих шортсів зі статистикою немає.")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    for i, it in enumerate(top):
        lines.append(f"\n{medals[i]} <b>{esc(it['title'])}</b>\n👁 {fmt_n(it['views'])} · ❤️ {fmt_n(it['likes'])}")
        per = []
        for key in PLATFORMS:
            p = it["per"].get(key)
            if not p:
                continue
            name = f'<a href="{esc(p["url"])}">{SHORT[key]}</a>' if p.get("url") else SHORT[key]
            per.append(f"{name} {fmt_n(p.get('views'))}")
        if per:
            lines.append("   " + " · ".join(per))
    total = sum(x["views"] for x in by_item.values())
    lines.append(f"\nУсього за тиждень: {len(by_item)} шортсів, 👁 {fmt_n(total)}")
    return "\n".join(lines)
