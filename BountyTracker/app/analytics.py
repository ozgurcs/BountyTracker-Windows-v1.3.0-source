import calendar
import math
import tkinter as tk
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from ui import ACCENT, ACCENT_SOFT, BORDER, CARD, FONT, MUTED, PANEL, duration, short_date


PERIODS = ('Daily', 'Weekly', 'Monthly')


def bounds(anchor, period):
    if period == 'Daily':
        return anchor, anchor + timedelta(days=1)
    if period == 'Weekly':
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=7)
    start = anchor.replace(day=1)
    return start, start + timedelta(days=calendar.monthrange(anchor.year, anchor.month)[1])


def previous_bounds(start, period):
    return bounds(start - timedelta(days=1), period)


def shift_period(anchor, period, direction):
    start, end = bounds(anchor, period)
    target_start, target_end = bounds(start - timedelta(days=1) if direction < 0 else end, period)
    offset = min((anchor - start).days, (target_end - target_start).days - 1)
    return target_start + timedelta(days=offset)


def date_range_label(start, end, period):
    if period == 'Daily':
        return f'{short_date(start)} {start.year}'
    if period == 'Monthly':
        return f'{calendar.month_name[start.month]} {start.year}'
    last = end - timedelta(days=1)
    return f'{short_date(start)} {start.year} – {short_date(last)} {last.year}' if start.year != last.year else (
        f'{short_date(start)} – {short_date(last)} {last.year}')


@dataclass
class Summary:
    seconds: int
    sessions: int
    active_days: int
    daily: dict
    groups: list


def summarize(records, *, by_item):
    daily = defaultdict(int)
    groups = {}
    active = set()
    seconds = 0
    for row in records:
        day = row['started_at'][:10]
        daily[day] += row['seconds']
        active.add(day)
        seconds += row['seconds']
        if by_item:
            key = row['function_id']
            label = row['item_name'] or 'Unassigned'
        else:
            key = row['program_id']
            label = row['project_name']
        group = groups.setdefault(key, {'name': label, 'seconds': 0, 'sessions': 0})
        group['seconds'] += row['seconds']
        group['sessions'] += 1
    breakdown = sorted(groups.values(), key=lambda group: (-group['seconds'], group['name'].casefold()))
    return Summary(seconds, len(records), len(active), dict(daily), breakdown)


def activity_chart(app, parent, totals, start, end, *, selected_day=None):
    days = [start + timedelta(days=offset) for offset in range((end - start).days)]
    points = [totals.get(day.isoformat(), 0) for day in days]
    label = app.label(parent, 'Hover over a bar to see the date and time.', 9, MUTED)
    label.pack(anchor='w', padx=15)
    canvas = tk.Canvas(parent, height=143, bg=PANEL, highlightthickness=0)
    canvas.pack(fill='both', expand=True, padx=14, pady=(3, 11))
    upper_hours = max(1, math.ceil(max(points, default=0) / 3600))
    hovered = None
    slots = []

    def draw(_=None):
        canvas.delete('all')
        slots.clear()
        left, top = 34, 10
        right, bottom = canvas.winfo_width() - 6, canvas.winfo_height() - 22
        if right <= left or bottom <= top:
            return
        canvas.create_line(left, top, right, top, fill=BORDER, dash=(3, 3))
        canvas.create_line(left, bottom, right, bottom, fill=BORDER)
        canvas.create_text(left - 6, top + 1, anchor='e', text=f'{upper_hours} h', fill=MUTED, font=(FONT, 8))
        canvas.create_text(left - 6, bottom, anchor='e', text='0 h', fill=MUTED, font=(FONT, 8))
        slot = (right - left) / len(days)
        bar_width = max(2, min(27, slot * .63))
        for index, (day, seconds) in enumerate(zip(days, points)):
            x = left + (index + .5) * slot
            slots.append(x)
            amount = min(1, seconds / (upper_hours * 3600))
            height = max(3, (bottom - top) * amount)
            color = ACCENT if index == hovered or day == selected_day else ('#a58ae8' if seconds else CARD)
            canvas.create_rectangle(x - bar_width / 2, bottom - height, x + bar_width / 2, bottom,
                                    fill=color, outline='')
            show_day = (len(days) <= 7 or index == 0 or index == len(days) - 1 or (day.day % 5 == 0 and index < len(days) - 2))
            if show_day and slot >= 5:
                canvas.create_text(x, bottom + 12, text=str(day.day), fill=MUTED, font=(FONT, 8))

    def hover(event):
        nonlocal hovered
        if not slots:
            return
        index = min(range(len(slots)), key=lambda i: abs(event.x - slots[i]))
        if index == hovered:
            return
        hovered = index
        label.configure(text=f'{short_date(days[index])}  ·  {duration(points[index])}')
        draw()

    def leave(_):
        nonlocal hovered
        hovered = None
        label.configure(text='Hover over a bar to see the date and time.')
        draw()

    canvas.bind('<Configure>', draw)
    canvas.bind('<Motion>', hover)
    canvas.bind('<Leave>', leave)
