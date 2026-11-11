"""Звіти: щоденний підсумок і перегляди через 24 год після публікації."""
from __future__ import annotations

import html
import logging
from datetime import datetime, time as dtime, timedelta

from config import NAMES, PLATFORMS, SHORT, fmt_slot, local_dt, now_ts
from meta import read_json, write_json

log = logging.getLogger("reports")
esc = html.escape


def daily_report(app, now: int | None = None, stats: dict | None = None) -> str:
    now = now or now_ts()
    cfg, db = app.cfg, app.db
    day = local_dt(cfg, now).date()
    start = int(datetime.combine(day, dtime(0, 0), cfg["tz"]).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), dtime(0, 0), cfg["tz"]).timestamp())
    ev = db.events(start, end)

    lines = [f"📊 <b>Підсумок за {day.strftime('%d.%m.%Y')}</b>"]

    published = [e for e in ev if e["kind"] == "published"]
    lines.append(f"\n✅ <b>Опубліковано: {len(published)}</b>")
    for e in published:
        d = e["details"] or {}
        links = " ".join(f'<a href="{esc(u)}">{SHORT[p]}</a>' if u else SHORT[p]
                         for p, u in (d.get("urls") or {}).items())
        fail = f" | ❌ {', '.join(SHORT[p] for p in d.get('failed', []))}" if d.get("failed") else ""
        lines.append(f"• {local_dt(cfg, e['ts']).strftime('%H:%M')} {esc(d.get('title', ''))} — {links}{fail}")

    skipped = [e for e in ev if e["kind"] == "skipped"]
    moved = [e for e in ev if e["kind"] == "moved_to_skipped"]
    empty = [e for e in ev if e["kind"] == "empty_slot"]
    lines.append(f"\n⏭ <b>Пропущено слотів: {len(skipped)}</b>" + (f" (порожніх слотів: {len(empty)})" if empty else ""))
    for e in skipped:
        d = e["details"] or {}
        lines.append(f"• {esc(d.get('title', ''))} ({d.get('skips')}/{cfg['max_skips']})")
    for e in moved:
        lines.append(f"• 🗑 {esc((e['details'] or {}).get('title', ''))} → skipped/")

    errors = [e for e in ev if e["kind"] in ("platform_error", "publish_failed")]
    lines.append(f"\n⚠️ <b>Помилки: {len(errors)}</b>")
    for e in errors:
        d = e["details"] or {}
        if e["kind"] == "platform_error":
            lines.append(f"• #{e['item_id']} {NAMES.get(d.get('platform'), d.get('platform'))}: "
                         f"{esc(str(d.get('error', ''))[:150])}")
        else:
            lines.append(f"• #{e['item_id']} {esc(d.get('title', ''))}: не опубліковано ніде")

    queue = db.queue()
    waiting = db.items("status IN ('notified','approved')", order="slot_at")
    lines.append(f"\n📦 <b>Відкладено (у черзі): {len(queue)}</b>")
    for it in queue[:5]:
        lines.append(f"• {esc(_title(it))}" + (f" (пропусків: {it['skips']})" if it["skips"] else ""))
    if len(queue) > 5:
        lines.append(f"… і ще {len(queue) - 5}")
    upcoming = app.scheduler.upcoming(now, 6)
    if upcoming or waiting:
        lines.append("🗓 <b>Заплановано:</b> " + ", ".join(
            fmt_slot(cfg, u["slot_at"], now) + (" ✅" if u["item_id"] else "") for u in upcoming))
    if stats:
        lines.append("")
        lines.append(analytics_text(app, stats, title=False))
    if cfg.get("dry_run"):
        lines.append("🧪 Режим dry_run — реальних публікацій немає")
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
    app.bot.send("\n".join(lines), html_mode=True, to="all")
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
    """По кожній платформі: підписники (+за добу), перегляди й лайки за добу (по наших шортсах)."""
    db, now = app.db, stats["ts"]
    lines = ["📈 <b>Аналітика</b>"] if title else ["📈 <b>Платформи за добу</b>"]
    if app.cfg.get("dry_run"):
        lines.append("🧪 Тестовий режим — реальної статистики немає")
        return "\n".join(lines)
    if not app.platforms:
        lines.append("Немає підключених платформ")
    for key in PLATFORMS:
        if key not in app.platforms:
            continue
        acc = stats["accounts"].get(key) or {}
        prev = _prev_account(db, key, now) or {}
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
        parts = []
        if acc.get("followers") is not None:
            parts.append(f"👥 {fmt_n(acc['followers'])}{fmt_d(_delta(acc.get('followers'), prev.get('followers')))}")
        ch_views = _delta(acc.get("views"), prev.get("views"))  # YouTube: перегляди всього каналу за добу
        if ch_views is not None:
            parts.append(f"👁 +{fmt_n(ch_views)} (канал)")
        if has_prev_posts:
            parts.append(f"👁 +{fmt_n(dv)} · ❤️ +{fmt_n(dl)} (шортси)")
        if key in stats["errors"] and not parts:
            parts.append("⚠️ " + esc(stats["errors"][key]))
        lines.append(f"<b>{NAMES[key]}</b>: " + (" · ".join(parts) or "даних поки немає"))
    if not any(_prev_account(db, k, now) for k in app.platforms):
        lines.append("<i>Різниця «за добу» з'явиться з наступного звіту.</i>")
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
