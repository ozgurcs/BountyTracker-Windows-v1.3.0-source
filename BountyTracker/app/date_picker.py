"""A clickable Tk calendar for dates stored as ISO values."""

from calendar import Calendar, day_abbr, month_name
from datetime import date
import tkinter as tk

from ui import ACCENT, ACCENT_SOFT, BORDER, CARD, FG, MUTED, PANEL


class DateField:
    def __init__(self, app, parent, title, value, changed, *, earliest=None, optional=False):
        self.app = app
        self.owner = parent.winfo_toplevel()
        self.title = title
        self.changed = changed
        self.earliest = earliest
        self.optional = optional
        self.popup = None
        self.button = app.button(parent, '', self.open)
        self.button.configure(anchor='w', takefocus=True)
        self.set(value)

    def set(self, value):
        self.value = value
        label = value.strftime('%a, %d %b %Y') if value is not None else 'No deadline'
        self.button.configure(text=f'{label}  ▾')

    def open(self):
        if self.popup is not None:
            self.popup.lift()
            return
        today = date.today()
        minimum = self.earliest() if self.earliest else date.min
        anchor = max(self.value or today, minimum)
        self.month = anchor.year * 12 + anchor.month - 1
        first_month = minimum.year * 12 + minimum.month - 1
        last_month = date.max.year * 12 + date.max.month - 1

        app = self.app
        owner = self.owner
        popup = tk.Toplevel(owner, bg=PANEL)
        self.popup = popup
        popup.withdraw()
        popup.title(self.title)
        popup.transient(owner)
        popup.resizable(False, False)
        body = tk.Frame(popup, bg=PANEL, padx=12, pady=10)
        body.pack(fill='both', expand=True)

        header = tk.Frame(body, bg=PANEL)
        header.pack(fill='x')
        previous = app.button(header, '‹', lambda: shift(-1), kind='quiet', width=3)
        previous.pack(side='left')
        following = app.button(header, '›', lambda: shift(1), kind='quiet', width=3)
        following.pack(side='right')
        current = app.label(header, '', 13, FG, bold=True)
        current.configure(anchor='center')
        current.pack(fill='x', expand=True)

        grid = tk.Frame(body, bg=PANEL)
        grid.pack(fill='both', expand=True, pady=(5, 7))
        for column, label in enumerate(day_abbr):
            grid.grid_columnconfigure(column, weight=1, uniform='calendar-day')
            caption = app.label(grid, label, 9, MUTED)
            caption.configure(anchor='center')
            caption.grid(row=0, column=column, sticky='ew', pady=(0, 4))

        tk.Frame(body, bg=BORDER, height=1).pack(fill='x', pady=(1, 7))
        actions = tk.Frame(body, bg=PANEL)
        actions.pack(fill='x')
        today_button = app.button(actions, 'Today', lambda: choose(today), kind='quiet')
        today_button.pack(side='left')
        if today < minimum:
            today_button.configure(state='disabled')
        if self.optional:
            app.button(actions, 'No deadline', lambda: choose(None), kind='quiet').pack(side='right')

        def choose(value):
            self.set(value)
            popup.destroy()
            self.changed()

        def restore(event):
            if event.widget is not popup:
                return
            self.popup = None
            if owner.winfo_exists() and self.button.winfo_exists():
                owner.grab_set()
                self.button.focus_set()

        def render():
            year, month_index = divmod(self.month, 12)
            month = month_index + 1
            current.configure(text=f'{month_name[month]} {year}')
            previous.configure(state='normal' if self.month > first_month else 'disabled')
            following.configure(state='normal' if self.month < last_month else 'disabled')
            for widget in grid.winfo_children():
                if int(widget.grid_info()['row']) > 0:
                    widget.destroy()
            weeks = Calendar().monthdayscalendar(year, month)
            for row in range(1, 7):
                grid.grid_rowconfigure(row, minsize=35)
            for row, week in enumerate(weeks, 1):
                for column, number in enumerate(week):
                    if number == 0:
                        continue
                    value = date(year, month, number)
                    selected = value == self.value
                    button = app.button(grid, str(number), lambda day=value: choose(day),
                                        kind='primary' if selected else 'quiet', width=3)
                    button.configure(padx=0, pady=4, takefocus=value >= minimum)
                    if value < minimum:
                        button.configure(state='disabled')
                    elif value == today and not selected:
                        button.configure(bg=CARD, fg=ACCENT, highlightbackground=ACCENT_SOFT)
                    button.grid(row=row, column=column, sticky='ew', padx=1, pady=1)

        def shift(offset):
            candidate = self.month + offset
            if first_month <= candidate <= last_month:
                self.month = candidate
                render()

        popup.bind('<Escape>', lambda _: popup.destroy())
        popup.bind('<Prior>', lambda _: shift(-1))
        popup.bind('<Next>', lambda _: shift(1))
        popup.bind('<Destroy>', restore, add=True)
        popup.protocol('WM_DELETE_WINDOW', popup.destroy)
        render()
        popup.update_idletasks()
        width = popup.winfo_reqwidth()
        height = popup.winfo_reqheight()
        left = min(self.button.winfo_rootx(), owner.winfo_rootx() + owner.winfo_width() - width)
        bottom = self.button.winfo_rooty() + self.button.winfo_height()
        top = min(bottom, owner.winfo_rooty() + owner.winfo_height() - height)
        popup.geometry(f'+{max(0, left)}+{max(0, top)}')
        popup.deiconify()
        popup.grab_set()
        popup.focus_set()
