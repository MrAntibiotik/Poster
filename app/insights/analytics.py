"""Аналітика за періодами: доба, тиждень, місяць, 3 / 6 / 12 місяців.

Дані беруться з бази (знімки статистики, які бот збирає щодня), тож звіт за рік будується миттєво.
• приріст переглядів / лайків / підписників за період по кожній платформі;
• скільки шортсів опубліковано;
• найуспішніші відео періоду (за переглядами, набраними саме в цей період);
• найкращі дні тижня й години публікації — за переглядами першої доби (чесне порівняння, вік однаковий);
• для кожного відео — «віхи»: перегляди за 1 день, 7, 30, 90, 180, 365 днів.
"""
from __future__ import annotations

import html
from collections import defaultdict

from app.core.config import NAMES, PLATFORMS, local_dt, now_ts

DAY = 86400
PERIODS = {1: "Доба", 7: "Тиждень", 30: "Місяць", 90: "3 місяці", 180: "6 місяців", 365: "Рік"}
MILESTONES = [(1, "1 д"), (7, "7 д"), (30, "30 д"), (90, "3 міс"), (180, "6 міс"), (365, "рік")]
ICONS = {"youtube": "▶️", "tiktok": "🎵", "instagram": "📸", "facebook": "📘"}
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
esc = html.escape


def n(v) -> str:
    return "н/д" if v is None else f"{int(v):,}".replace(",", " ")


def signed(v) -> str:
    return "н/д" if v is None else ("+" if v >= 0 else "−") + n(abs(v))


def snapshot_due(db, item: dict, platform: str, now: int) -> bool:
    """Як часто знімати статистику відео: щодня перший тиждень, щотижня до 3 міс., щомісяця до року."""
    age = now - (item["posted_at"] or now)
    if age <= 8 * DAY:
        return True
    if age > 400 * DAY:
        return False
    last = db.one("SELECT MAX(ts) AS t FROM post_stats WHERE item_id=? AND platform=?", (item["id"], platform))
    gap = 7 * DAY if age <= 95 * DAY else 30 * DAY
    return not last or not last["t"] or now - last["t"] >= gap - 6 * 3600


def _series(db) -> dict:
    """{(item_id, platform): [(ts, views, likes), …]} відсортовано за часом."""
    out = defaultdict(list)
    for r in db.q("SELECT item_id, platform, ts, views, likes FROM post_stats ORDER BY ts"):
        out[(r["item_id"], r["platform"])].append((r["ts"], r["views"], r["likes"]))
    return out


def _at(points: list, ts: int):
    """Останній знімок не пізніше ts."""
    best = None
    for p in points:
        if p[0] <= ts:
            best = p
        else:
            break
    return best


def first_day_views(item: dict, points_by_platform: dict) -> int | None:
    """Перегляди першої доби (усі платформи разом) — для порівняння днів і годин публікації."""
    total, found = 0, False
    for p, r in (item.get("results") or {}).items():
        if r.get("status") != "ok":
            continue
        v = r.get("views24")
        if v is None:
            pts = points_by_platform.get(p) or []
            cand = [x for x in pts if item["posted_at"] + 20 * 3600 <= x[0] <= item["posted_at"] + 48 * 3600]
            v = cand[0][1] if cand else None
        if isinstance(v, int):
            total += v
            found = True
    return total if found else None


def milestones(item: dict, points_by_platform: dict, now: int) -> list[tuple[str, int | None]]:
    out = []
    for days, label in MILESTONES:
        if now - item["posted_at"] < days * DAY - 6 * 3600 and days > 1:
            break
        total, found = 0, False
        for p, pts in points_by_platform.items():
            snap = _at(pts, item["posted_at"] + days * DAY + 12 * 3600)
            if snap and snap[1] is not None:
                total += snap[1]
                found = True
        if days == 1:
            fd = first_day_views(item, points_by_platform)
            total, found = (fd, True) if fd is not None else (total, found)
        out.append((label, total if found else None))
    return out


def _title(item: dict) -> str:
    from app.insights.reports import _clean_title
    return _clean_title({}, item)


def period_report(cfg: dict, db, days: int, platforms: list[str] | None = None, now: int | None = None) -> str:
    now = now or now_ts()
    start = now - days * DAY
    platforms = [p for p in PLATFORMS if p in (platforms or PLATFORMS)]
    s_dt, e_dt = local_dt(cfg, start), local_dt(cfg, now)
    lines = [f"📊 <b>Аналітика: {PERIODS.get(days, f'{days} днів').lower()}</b>",
             f"<i>{s_dt:%d.%m.%Y} — {e_dt:%d.%m.%Y %H:%M}</i>"]
    if cfg.get("dry_run"):
        lines.append("🧪 Тестовий режим — реальної статистики немає")
        return "\n".join(lines)

    series = _series(db)
    items = {it["id"]: it for it in db.items("status='posted' AND posted_at IS NOT NULL")}
    published = [it for it in items.values() if it["posted_at"] >= start]

    # ---- приріст за період по платформах ----
    gained: dict[int, int] = defaultdict(int)
    plat_v: dict[str, int] = defaultdict(int)
    plat_l: dict[str, int] = defaultdict(int)
    for (iid, p), pts in series.items():
        it = items.get(iid)
        if not it or p not in platforms:
            continue
        last = _at(pts, now)
        if not last or last[1] is None:
            continue
        if it["posted_at"] >= start:
            base_v, base_l = 0, 0
        else:
            base = _at(pts, start)
            if not base:  # знімків до початку періоду немає — рахуємо від першого відомого
                base = pts[0]
            base_v, base_l = base[1] or 0, base[2] or 0
        dv = max(0, last[1] - base_v)
        dl = max(0, (last[2] or 0) - base_l) if last[2] is not None else 0
        gained[iid] += dv
        plat_v[p] += dv
        plat_l[p] += dl

    lines.append(f"\n🎬 Опубліковано шортсів: <b>{len(published)}</b>")
    tot_v = tot_l = 0
    for p in platforms:
        acc_last = db.one("SELECT * FROM account_stats WHERE platform=? AND ts<=? ORDER BY ts DESC LIMIT 1", (p, now))
        acc_base = (db.one("SELECT * FROM account_stats WHERE platform=? AND ts<=? ORDER BY ts DESC LIMIT 1", (p, start))
                    or db.one("SELECT * FROM account_stats WHERE platform=? AND ts>=? ORDER BY ts LIMIT 1", (p, start)))
        if not acc_last and not plat_v.get(p):
            continue
        lines.append(f"\n{ICONS[p]} <b>{NAMES[p]}</b>")
        if acc_last and acc_last.get("followers") is not None:
            d = (acc_last["followers"] - acc_base["followers"]) if acc_base and acc_base.get("followers") is not None \
                and acc_base["ts"] != acc_last["ts"] else None
            lines.append(f"   👥 Підписники: <b>{n(acc_last['followers'])}</b>"
                         + (f" ({signed(d)} за період)" if d is not None else ""))
        if acc_last and acc_base and acc_last.get("views") is not None and acc_base.get("views") is not None \
                and acc_base["ts"] != acc_last["ts"]:
            lines.append(f"   📺 Перегляди каналу: <b>{signed(acc_last['views'] - acc_base['views'])}</b>")
        lines.append(f"   🎬 Наші шортси: 👁 <b>+{n(plat_v.get(p, 0))}</b> переглядів · ❤️ <b>+{n(plat_l.get(p, 0))}</b> лайків")
        tot_v += plat_v.get(p, 0)
        tot_l += plat_l.get(p, 0)
    lines.append(f"\n<b>Разом:</b> 👁 +{n(tot_v)} переглядів · ❤️ +{n(tot_l)} лайків")

    # ---- найуспішніші відео ----
    top = sorted((iid for iid in gained if gained[iid] > 0), key=lambda i: gained[i], reverse=True)[:5]
    if top:
        lines.append("\n🏆 <b>Найуспішніші відео періоду</b>")
        medals = ["🥇", "🥈", "🥉", "4.", "5."]
        for k, iid in enumerate(top):
            it = items[iid]
            url = next((r.get("url") for p, r in it["results"].items() if r.get("status") == "ok" and r.get("url")), "")
            name = f'<a href="{esc(url)}">{esc(_title(it))}</a>' if url else esc(_title(it))
            by_p = {p: pts for (i, p), pts in series.items() if i == iid}
            ms = [f"{lab}: {n(v)}" for lab, v in milestones(it, by_p, now) if v is not None][:4]
            lines.append(f"{medals[k]} {name} — 👁 <b>+{n(gained[iid])}</b> <i>({local_dt(cfg, it['posted_at']):%d.%m})</i>"
                         + (f"\n      {' · '.join(ms)}" if ms else ""))

    # ---- найкращі дні та години (за переглядами першої доби) ----
    scored = []
    for it in published:
        by_p = {p: pts for (i, p), pts in series.items() if i == it["id"]}
        v = first_day_views(it, by_p)
        if v is not None:
            scored.append((local_dt(cfg, it["posted_at"]), v))
    if len(scored) >= 3:
        by_day, by_hour = defaultdict(list), defaultdict(list)
        for dt, v in scored:
            by_day[dt.weekday()].append(v)
            by_hour[dt.hour].append(v)
        avg = lambda xs: sum(xs) / len(xs)  # noqa: E731
        days_sorted = sorted(by_day.items(), key=lambda kv: avg(kv[1]), reverse=True)
        hours_sorted = sorted(by_hour.items(), key=lambda kv: avg(kv[1]), reverse=True)
        lines.append("\n📅 <b>Найкращі дні публікації</b> <i>(середні перегляди першої доби)</i>")
        for wd, vs in days_sorted[:3]:
            lines.append(f"   {WEEKDAYS[wd]}: 👁 {n(avg(vs))} <i>({len(vs)} шорт.)</i>")
        lines.append("🕐 <b>Найкращий час публікації</b>")
        for h, vs in hours_sorted[:3]:
            lines.append(f"   {h:02d}:00–{h:02d}:59: 👁 {n(avg(vs))} <i>({len(vs)} шорт.)</i>")
    elif published:
        lines.append("\n<i>Найкращі дні й години з'являться, коли буде ≥3 шортсів зі статистикою першої доби.</i>")
    if not series:
        lines.append("\n<i>Статистики ще немає — бот збирає її щодня під час звіту (о 22:30) і при «📈 Аналітика».</i>")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3990] + "…"


def keyboard(current: int) -> dict:
    row1 = [(1, "Доба"), (7, "Тиждень"), (30, "Місяць")]
    row2 = [(90, "3 міс"), (180, "6 міс"), (365, "Рік")]
    mk = lambda d, t: {"text": ("• " + t + " •") if d == current else t, "callback_data": f"an:{d}"}  # noqa: E731
    top = {"text": "• 🏆 Топ тижня •" if current == TOP else "🏆 Топ тижня", "callback_data": f"an:{TOP}"}
    return {"inline_keyboard": [[mk(d, t) for d, t in row1], [mk(d, t) for d, t in row2], [top]]}


TOP = -7   # кнопка «🏆 Топ тижня» у клавіатурі аналітики
