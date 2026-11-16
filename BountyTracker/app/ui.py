import math
import tkinter as tk
from datetime import date, timedelta
from tkinter import ttk

BG = '#f5f4fa'
SIDEBAR = '#211a36'
SIDEBAR_TILE = '#302746'
SIDEBAR_ACTIVE = '#46376e'
SIDEBAR_TEXT = '#d7cfee'
SIDEBAR_BRIGHT = '#f6f2ff'
PANEL = '#ffffff'
CARD = '#f0edf8'
BORDER = '#ded9ec'
FG = '#241d37'
MUTED = '#605874'
ACCENT = '#5e3bc6'
ACCENT_HOVER = '#4b2aa9'
ACCENT_SOFT = '#ece6fd'
PURPLE_SURFACE = '#f2edff'
GREEN = '#17694b'
GREEN_SOFT = '#e5f4eb'
INK = '#ffffff'
ALT = '#faf9fd'
WARN = '#a22545'
FONT = 'Segoe UI'

# Background, text, edge, and marker for each progress state.
PROGRESS_PALETTE = {
    'Not Started': ('#f1eff5', '#635a72', '#b9b2c6', '○'),
    'In Progress': ('#e6efff', '#234e9c', '#6d9bef', '◐'),
    'Reviewed': ('#e2f4f4', '#125c61', '#60b1b4', '◉'),
    'Completed': ('#e0f3e7', '#166343', '#59b383', '✓'),
    'Needs Revisit': ('#fff0d8', '#804415', '#e7a04d', '↻'),
    'On Hold': ('#f0e7fa', '#643d83', '#af89cb', 'Ⅱ'),
}


def progress_palette(state):
    return PROGRESS_PALETTE.get(state, PROGRESS_PALETTE['Not Started'])


def progress_badge(parent, state, *, prominent=False):
    background, text, _, marker = progress_palette(state)
    return tk.Label(parent, text=f'{marker}  {state}', bg=background, fg=text,
                    font=(FONT, 11 if prominent else 10, 'bold'),
                    padx=10, pady=5 if prominent else 4)


def progress_tree_tags(tree):
    for state, (background, text, _, _) in PROGRESS_PALETTE.items():
        tree.tag_configure('progress:' + state, background=background, foreground=text)


DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def number(value, places=1):
    return f'{value:.{places}f}'.rstrip('0').rstrip('.') if places else str(round(value))


def hours(seconds):
    return f'{number(seconds / 3600)} h'


def duration(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    whole, rest = divmod(minutes, 60)
    parts = (f'{whole} h' if whole else '', f'{rest} min' if rest else '', f'{seconds} s' if seconds else '')
    return ' '.join(filter(None, parts)) or '0 s'


def count_label(count, noun):
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def short_date(day):
    return f'{day.day} {MONTHS[day.month - 1]}'


def display_date(value):
    try:
        day = date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return value or '—'
    return f'{short_date(day)} {day.year}'


def clip(value, limit=35):
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def surface(parent, color=PANEL):
    return tk.Frame(parent, bg=color, highlightbackground=BORDER, highlightthickness=1)


def configure_style(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('Treeview', background=PANEL, foreground=FG, fieldbackground=PANEL,
                    rowheight=38, borderwidth=0, relief='flat', bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, focuscolor=ACCENT, font=(FONT, 10))
    style.configure('Treeview.Heading', background=CARD, foreground=MUTED,
                    font=(FONT, 10, 'bold'), relief='flat', padding=(9, 9))
    style.map('Treeview', background=[('selected', ACCENT_SOFT)], foreground=[('selected', FG)])
    style.map('Treeview.Heading', background=[('active', BORDER)])
    style.configure('TCombobox', fieldbackground=CARD, background=CARD, foreground=FG,
                    arrowcolor=ACCENT, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    insertcolor=FG, padding=(8, 5))
    style.map('TCombobox', fieldbackground=[('readonly', CARD)],
              foreground=[('readonly', FG)], selectbackground=[('readonly', CARD)],
              selectforeground=[('readonly', FG)])
    for orientation in ('Vertical', 'Horizontal'):
        style.configure(f'{orientation}.TScrollbar', background=BORDER, troughcolor=BG,
                        bordercolor=BG, lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED,
                        arrowsize=12, gripcount=0, relief='flat')
        style.map(f'{orientation}.TScrollbar', background=[('active', '#bfb3da'), ('disabled', BG)])
    style.configure('Focus.Horizontal.TProgressbar', background=ACCENT, troughcolor=ACCENT_SOFT,
                    bordercolor=ACCENT_SOFT, lightcolor=ACCENT, darkcolor=ACCENT, thickness=9)
    style.configure('TNotebook', background=BG, borderwidth=0, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.configure('TNotebook.Tab', background=CARD, foreground=MUTED, padding=(15, 8), bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    font=(FONT, 10, 'bold'))
    style.map('TNotebook.Tab', background=[('selected', ACCENT_SOFT), ('active', ALT)], foreground=[('selected', ACCENT)])
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', FG)
    root.option_add('*TCombobox*Listbox.selectBackground', ACCENT_SOFT)
    root.option_add('*TCombobox*Listbox.selectForeground', FG)
    root.option_add('*TCombobox*Listbox.font', (FONT, 11))


def rounded_track(canvas, x, y, width, color, thickness=8):
    radius = thickness / 2
    if width <= 0:
        return
    if width <= thickness:
        canvas.create_oval(x, y - width / 2, x + width, y + width / 2, fill=color, outline='')
        return
    canvas.create_line(x + radius, y, x + width - radius, y,
                       fill=color, width=thickness, capstyle=tk.ROUND)


def progress_bar(parent, fraction, *, color=None):
    canvas = tk.Canvas(parent, height=11, bg=parent.cget('bg'), highlightthickness=0)
    fill = color or (GREEN if fraction >= 1 else ACCENT)
    def draw(event):
        canvas.delete('all')
        rounded_track(canvas, 0, 5, event.width, BORDER)
        rounded_track(canvas, 0, 5, event.width * max(0, min(fraction, 1)), fill)
    canvas.bind('<Configure>', draw)
    return canvas


def progress_percent(fraction):
    if fraction >= 1:
        return 100
    return max(0, min(99, round(fraction * 100)))


def draw_ring(parent, fraction):
    canvas = tk.Canvas(parent, width=132, height=132, bg=parent.cget('bg'), highlightthickness=0)
    bounds = (12, 12, 120, 120)
    canvas.create_oval(*bounds, outline=BORDER, width=9)
    fraction = max(0, min(fraction, 1))
    if fraction >= 1:
        canvas.create_oval(*bounds, outline=GREEN, width=9)
    elif fraction > 0:
        canvas.create_arc(*bounds, start=90, extent=-360 * fraction, style='arc', outline=ACCENT, width=9)
    percent = progress_percent(fraction)
    canvas.create_text(66, 65, text=f'{percent}%', fill=FG, font=(FONT, 22, 'bold'))
    return canvas


def scroll_area(parent):
    canvas = tk.Canvas(parent, bg=parent.cget('bg'), highlightthickness=0)
    scrollbar = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y', padx=(6, 0))
    canvas.pack(side='left', fill='both', expand=True)
    area = tk.Frame(canvas, bg=parent.cget('bg'))
    window = canvas.create_window(0, 0, window=area, anchor='nw')
    area.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
    def scroll(event):
        if event.delta:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    canvas.bind('<MouseWheel>', scroll)
    def bind_wheel(widget):
        for child in widget.winfo_children():
            if not isinstance(child, (ttk.Treeview, tk.Text)):
                child.bind('<MouseWheel>', scroll)
            bind_wheel(child)

    job = canvas.after_idle(lambda: bind_wheel(area))
    canvas.bind('<Destroy>', lambda _: canvas.after_cancel(job), add=True)
    return area


def weekly_chart(app, parent, daily, today):
    days = [today - timedelta(days=n) for n in range(6, -1, -1)]
    points = [daily.get(day.isoformat(), 0) for day in days]
    title = tk.Frame(parent, bg=PANEL)
    title.pack(fill='x', padx=17, pady=(14, 0))
    app.label(title, 'Last 7 days', 12, bold=True).pack(side='left')
    app.label(title, f'{short_date(days[0])} – {short_date(today)}', 9, MUTED).pack(side='right')
    caption = app.label(parent, hours(sum(points)), 21, bold=True)
    caption.pack(anchor='w', padx=17)
    canvas = tk.Canvas(parent, height=123, bg=PANEL, highlightthickness=0)
    canvas.pack(fill='both', expand=True, padx=16, pady=(5, 10))
    peak_hours = max(points) / 3600
    step = max(1, math.ceil(peak_hours / 2))
    ceiling = step * 2
    active = None
    columns = []

    def draw(_=None):
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        start, right, top, bottom = 38, width - 4, 11, height - 23
        if right <= start or bottom <= top:
            return
        for tick in (0, step, ceiling):
            y = bottom - (bottom - top) * tick / ceiling
            canvas.create_line(start, y, right, y, fill=BORDER, dash=(2, 3) if tick else ())
            canvas.create_text(start - 6, y, anchor='e', text=f'{tick} h', fill=MUTED, font=(FONT, 8))
        columns.clear()
        slot = (right - start) / len(days)
        for i, (day, seconds) in enumerate(zip(days, points)):
            x = start + (i + .5) * slot
            columns.append(x)
            bar = min(23, slot * .57)
            bar_height = max(3, (bottom - top) * seconds / (3600 * ceiling))
            color = ACCENT if i == active or (day == today and seconds) else ('#aca0e4' if seconds else CARD)
            canvas.create_rectangle(x - bar / 2, bottom - bar_height, x + bar / 2, bottom,
                                    fill=color, outline='')
            canvas.create_text(x, bottom + 12, text=DAYS[day.weekday()],
                               fill=ACCENT if day == today else MUTED, font=(FONT, 8))
    def hover(event):
        nonlocal active
        if not columns:
            return
        index = min(range(len(columns)), key=lambda i: abs(event.x - columns[i]))
        if index == active:
            return
        active = index
        caption.configure(text=f'{hours(points[index])}  ·  {short_date(days[index])}')
        draw()
    def leave(_):
        nonlocal active
        active = None
        caption.configure(text=hours(sum(points)))
        draw()
    canvas.bind('<Configure>', draw)
    canvas.bind('<Motion>', hover)
    canvas.bind('<Leave>', leave)

