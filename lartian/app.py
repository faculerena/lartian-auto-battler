"""Lartian Quest TUI."""
import asyncio
import json
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static

from telethon.errors import ApiIdInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError

from .game import Game, GameError, Item, buttons, cost, parse_counts, parse_inventory, parse_status

# credentials, Telegram session and local cache live here, never next to the code
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lartian"
CREDS = CONFIG / "config.json"
SESSION = CONFIG / "lartian"  # Telethon adds .session
STATE = CONFIG / "state.json"

# Telethon stalls when it shares Textual's event loop in a real terminal,
# so every Telegram call runs on its own loop in a background thread.
TG_LOOP = asyncio.new_event_loop()
threading.Thread(target=TG_LOOP.run_forever, daemon=True).start()


def tg(coro):
    """Run `coro` on the Telegram loop; await the result from the app loop."""
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, TG_LOOP))

# column key -> (header, sort key)
COLUMNS = {
    "mark": ("", lambda app, i: (i.id != app.target, i.id not in app.marks)),
    "lock": ("🔒", lambda app, i: i.id in app.locks),
    "eq": ("Eq", lambda app, i: i.equipped),
    "id": ("ID", lambda app, i: i.id),
    "name": ("Name", lambda app, i: i.name),
    "plus": ("+", lambda app, i: i.plus),
    "stars": ("★", lambda app, i: i.stars),
    "lv": ("Lv", lambda app, i: i.level),
    "evo": ("Evo", lambda app, i: len(app.evo_copies(i))),
    "atk": ("ATK", lambda app, i: i.atk),
    "def": ("DEF", lambda app, i: i.defense),
    "base": ("Base", lambda app, i: i.base),
    "cap": ("Cap", lambda app, i: i.capacity),
    "gear": ("Gear", lambda app, i: i.gear),
}
SORT_CYCLE = ["atk", "def", "base", "plus", "stars", "name", "id", "gear"]


class Ask(ModalScreen[str]):
    DEFAULT_CSS = """
    Ask { align: center middle; }
    #dialog { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    """

    def __init__(self, prompt, password=False):
        super().__init__()
        self.prompt, self.password = prompt, password

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(self.prompt)
            yield Input(password=self.password)

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event):
        if event.value.strip():
            self.dismiss(event.value.strip())


class Confirm(ModalScreen[bool]):
    BINDINGS = [("y", "answer(True)", "Yes"), ("n,escape", "answer(False)", "No")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog { width: 80; height: auto; max-height: 90%; border: thick $warning; background: $surface; padding: 1 2; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    """

    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(self.text)
            with Horizontal(id="buttons"):
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (n)", id="no")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes):
        self.dismiss(yes)


class Lartian(App):
    TITLE = "Lartian Quest"
    AUTO_FOCUS = "#items"
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #items { width: 2fr; }
    #log { width: 1fr; border-left: solid $primary; }
    #filter { dock: bottom; display: none; }
    #filter.shown { display: block; }
    """
    BINDINGS = [
        Binding("r", "sync", "Sync"),
        Binding("t", "target", "Target"),
        Binding("space", "mark", "Mark"),
        Binding("a", "mark_visible", "Mark shown", show=False),
        Binding("j", "mark_junk", "Mark junk"),
        Binding("c", "clear", "Clear marks"),
        Binding("l", "lock", "Lock"),
        Binding("e", "feed('enhance')", "Enhance"),
        Binding("v", "feed('evolve')", "Evolve"),
        Binding("x", "sell", "Sell marked"),
        Binding("p", "adventure", "Adventure (full)"),
        Binding("u", "equip", "Equip/unequip"),
        Binding("A", "auto_equip", "Auto-equip"),
        Binding("o", "open('regular')", "Open chests"),
        Binding("O", "open('magical')", "Open (magical)", show=False),
        Binding("k", "stop", "Stop"),
        Binding("s", "sort", "Sort"),
        Binding("S", "reverse", "Reverse", show=False),
        Binding("f", "free", "Free only"),
        Binding("slash", "filter", "Filter"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.items, self.locks, self.status = {}, set(), {}
        if STATE.exists():
            s = json.loads(STATE.read_text())
            self.items = {d["id"]: Item(**d) for d in s["items"]}
            self.locks, self.status = set(s["locks"]), s["status"]
        self.marks, self.target = set(), None
        self.sort_key, self.sort_rev = "atk", True
        self.text_filter, self.free_only = "", False
        self.game, self.busy, self.stop_requested = None, False, False

    def compose(self):
        yield Header()
        yield Static(id="status")
        with Horizontal():
            yield DataTable(id="items", cursor_type="row", zebra_stripes=True)
            yield RichLog(id="log", markup=True, wrap=True)
        yield Input(placeholder="filter by name (enter/esc to close)", id="filter")
        yield Footer()

    # ---- state ----

    def save(self):
        STATE.write_text(json.dumps({"items": [asdict(i) for i in self.items.values()],
                                     "locks": sorted(self.locks), "status": self.status}, indent=1))

    def log_line(self, msg):
        self.query_one("#log", RichLog).write(msg)

    def eligible(self, it):
        """Why `it` can't be eaten, or None if it can."""
        if it.id in self.locks:
            return "locked"
        if it.equipped:
            return "equipped"
        if it.id == self.target:
            return "the target"
        return None

    # ---- rendering ----

    def visible(self):
        rows = [i for i in self.items.values()
                if self.text_filter.lower() in i.label.lower()
                and not (self.free_only and self.eligible(i))]
        key = COLUMNS[self.sort_key][1]
        return sorted(rows, key=lambda i: key(self, i), reverse=self.sort_rev)

    def render_table(self):
        table = self.query_one("#items", DataTable)
        cur = self.cursor_id()
        table.clear()
        for it in self.visible():
            style = ("bold magenta" if it.id == self.target else "bold red" if it.id in self.marks
                     else "yellow" if it.id in self.locks else "green" if it.equipped else "")
            mark = "◎" if it.id == self.target else "●" if it.id in self.marks else ""
            cells = [mark, "🔒" if it.id in self.locks else "", "✓" if it.equipped else "", it.id,
                     it.name.removeprefix("Mythic "), f"+{it.plus}", "★" * it.stars,
                     f"{it.level}/{it.level_cap}", len(self.evo_copies(it)) or "",
                     it.atk, it.defense, it.base, it.capacity, it.gear]
            table.add_row(*(Text(str(c), style=style) for c in cells), key=str(it.id))
        if cur is not None and str(cur) in table.rows:
            table.move_cursor(row=table.get_row_index(str(cur)))
        self.render_status()

    def render_status(self):
        t = self.items.get(self.target)
        est = sum(cost("enhance", 0, self.items[m].plus) for m in self.marks)
        arrow = "↓" if self.sort_rev else "↑"
        st = self.status
        parts = [f"💰 {st.get('gold', '?')}  AP {st.get('ap', '?')}  BP {st.get('bp', '?')}"
                 + (f"  🌌 {st['ancient']}" if st.get("ancient") else ""),
                 f"📬 {st.get('inbox', '?')}  🔑 {st.get('keys_regular', '?')}/{st.get('keys_magical', '?')}",
                 f"items {len(self.items)}",
                 f"target: {t.label if t else '-'}",
                 f"marked {len(self.marks)} (enhance ≈{est}g)",
                 f"sort {COLUMNS[self.sort_key][0] or 'mark'}{arrow}"]
        if self.free_only:
            parts.append("free only")
        if self.text_filter:
            parts.append(f"filter '{self.text_filter}'")
        if self.busy:
            parts.append("[b]RUNNING[/b]")
        self.query_one("#status", Static).update("  │  ".join(parts))

    def cursor_id(self):
        table = self.query_one("#items", DataTable)
        if not table.row_count:
            return None
        return int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    # ---- lifecycle ----

    async def on_mount(self):
        table = self.query_one("#items", DataTable)
        for key, (label, _) in COLUMNS.items():
            table.add_column(label, key=key)
        self.render_table()
        self.log_line("connecting…")
        self.run_worker(self._connect())

    async def ask(self, prompt, password=False):
        return await self.push_screen_wait(Ask(prompt, password))

    async def _connect(self):
        if CREDS.exists():
            creds = json.loads(CREDS.read_text())
        else:
            creds = {"api_id": "", "api_hash": ""}
            while not creds["api_id"].isdigit():
                creds["api_id"] = await self.ask(
                    "First run. Get an API ID and hash at https://my.telegram.org → API development tools.\n\nAPI ID:")
            creds["api_hash"] = await self.ask("API hash:")
            CREDS.write_text(json.dumps(creds))
            CREDS.chmod(0o600)

        async def make():  # built on the Telegram loop so its queue and client bind there
            return Game(SESSION, creds["api_id"], creds["api_hash"])
        game = await tg(make())
        game.log = lambda m: self.call_from_thread(self.log_line, f"[dim]{m}[/dim]")
        try:
            await tg(game.client.connect())
            if not await tg(game.client.is_user_authorized()):
                await self._login(game)
            await tg(game.start())
        except ApiIdInvalidError:
            CREDS.unlink()
            self.log_line("[red]wrong API ID or hash. Restart to enter them again.[/red]")
            return
        except Exception as e:
            self.log_line(f"[red]{type(e).__name__}: {e}[/red]")
            return
        self.game = game
        self.log_line("connected")
        if not self.items:
            self.action_sync()
            return
        try:  # fresh gold/AP/BP/Ancient for the status bar
            self.status.update(parse_status((await tg(game.home())).raw_text))
        except Exception as e:
            self.log_line(f"[yellow]couldn't read status: {e}[/yellow]")
        self.render_status()

    async def _login(self, game):
        phone = await self.ask("Phone number, international format (+54 9 11 ...):")
        await tg(game.client.send_code_request(phone))
        while True:
            code = await self.ask("Login code (sent to your Telegram app):")
            try:
                await tg(game.client.sign_in(phone, code))
                return
            except PhoneCodeInvalidError:
                self.log_line("[red]wrong code[/red]")
            except SessionPasswordNeededError:
                await tg(game.client.sign_in(password=await self.ask("2FA password:", password=True)))
                return

    async def on_unmount(self):
        if self.game:
            await tg(self.game.stop())

    def ready(self):
        if self.game is None:
            self.notify("not connected", severity="error")
        elif self.busy:
            self.notify("busy, wait for the current run", severity="warning")
        return self.game is not None and not self.busy

    # ---- actions ----

    def action_sync(self):
        if self.ready():
            self.run_worker(self._run(self._sync()), exclusive=True)

    async def _run(self, coro):
        self.busy = True
        self.render_status()
        try:
            await coro
        except GameError as e:
            self.log_line(f"[red]stopped: {e}[/red]")
        except Exception as e:
            self.log_line(f"[red]error: {type(e).__name__}: {e}[/red]")
        finally:
            self.busy = False
            self.render_table()

    async def _sync(self):
        items, self.status = await tg(self.game.sync())
        self.items = {i.id: i for i in items}
        for gone in self.locks - self.items.keys():
            self.log_line(f"[yellow]locked item #{gone} is gone, dropping lock[/yellow]")
        self.locks &= self.items.keys()
        self.marks &= self.items.keys()
        if self.target not in self.items:
            self.target = None
        self.save()
        self.log_line(f"synced {len(items)} items, gold {self.status.get('gold')}")

    async def _refresh(self):
        """Merge the inventory page the last reply landed on (forge, sell and equip results show
        one). Walk every page only when the header's item or equipped count disagrees with the cache."""
        msg = self.game.cur
        try:
            page = msg and parse_inventory(msg.raw_text, buttons(msg))
        except GameError:
            page = None
        if page:
            self.items.update({i.id: i for i in page[0]})
        if not page or parse_counts(msg.raw_text) != (len(self.items),
                                                      sum(i.equipped for i in self.items.values())):
            self.log_line("refreshing inventory…")
            return await self._sync()
        self.save()

    def action_target(self):
        if (i := self.cursor_id()) is not None:
            self.target = None if self.target == i else i
            self.marks.discard(i)
            self.render_table()

    def action_mark(self):
        i = self.cursor_id()
        if i is None:
            return
        if i in self.marks:
            self.marks.discard(i)
        elif why := self.eligible(self.items[i]):
            self.notify(f"can't mark: {why}", severity="warning")
        else:
            self.marks.add(i)
        self.render_table()

    def action_mark_visible(self):
        self.marks |= {i.id for i in self.visible() if not self.eligible(i)}
        self.render_table()

    def action_clear(self):
        self.marks.clear()
        self.render_table()

    def action_lock(self):
        i = self.cursor_id()
        if i is None:
            return
        self.locks ^= {i}
        self.marks.discard(i)
        self.save()
        self.render_table()

    def action_sort(self):
        self.sort_key = SORT_CYCLE[(SORT_CYCLE.index(self.sort_key) + 1) % len(SORT_CYCLE)] \
            if self.sort_key in SORT_CYCLE else SORT_CYCLE[0]
        self.render_table()

    def action_reverse(self):
        self.sort_rev = not self.sort_rev
        self.render_table()

    def on_data_table_header_selected(self, event):
        key = event.column_key.value
        if key == self.sort_key:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_key, self.sort_rev = key, True
        self.render_table()

    def action_free(self):
        self.free_only = not self.free_only
        self.render_table()

    def action_filter(self):
        box = self.query_one("#filter", Input)
        box.add_class("shown")
        box.focus()

    def on_input_changed(self, event):
        self.text_filter = event.value
        self.render_table()

    def on_input_submitted(self, event):
        self._close_filter()

    def on_key(self, event):
        if event.key == "escape" and self.query_one("#filter", Input).has_focus:
            self._close_filter()

    def _close_filter(self):
        self.query_one("#filter", Input).remove_class("shown")
        self.query_one("#items", DataTable).focus()

    # ---- helpers for marking ----

    def evo_copies(self, it):
        """Free copies that could evolve `it` right now (same item, same stars)."""
        return [c for c in self.items.values() if c.id != it.id and c.name == it.name
                and c.stars == it.stars and not self.eligible(c)]

    def junk(self):
        """Visible free +0 no-star items that aren't the best-Base copy of their item."""
        best = {}
        for i in self.items.values():
            if i.name not in best or i.base > best[i.name].base:
                best[i.name] = i
        return [i for i in self.visible() if not self.eligible(i)
                and i.plus == 0 and i.stars == 0 and best[i.name] is not i]

    def action_mark_junk(self):
        junk = self.junk()
        self.marks |= {i.id for i in junk}
        self.notify(f"marked {len(junk)} junk item(s)")
        self.render_table()

    # ---- feeding / selling ----

    def plan(self, kind):
        """Ordered material list, or raises GameError with the reason."""
        if self.target is None:
            raise GameError("pick a target first (t)")
        t = self.items[self.target]
        if kind == "enhance" and t.level >= t.level_cap:
            raise GameError(f"{t.label} is at level cap {t.level}/{t.level_cap}; evolve it first")
        mats = [self.items[m] for m in self.marks]
        if kind == "evolve" and not mats:
            copies = self.evo_copies(t)
            if not copies:
                raise GameError(f"no free {t.label} copy to evolve with")
            mats = [min(copies, key=lambda c: c.base)]
        if not mats:
            raise GameError("mark materials first (space)")
        for m in mats:
            if why := self.eligible(m):
                raise GameError(f"{m.label} is {why}")
        if kind == "enhance":
            return sorted(mats, key=lambda m: m.id)
        # evolve: same item, and each copy must match the target's stars at that step
        mats.sort(key=lambda m: m.stars)
        for step, m in enumerate(mats):
            if m.name != t.name or m.stars != t.stars + step:
                raise GameError(f"{m.label} can't evolve {t.name} at ★{t.stars + step}")
        if t.stars + len(mats) > 3:
            raise GameError("an item can have at most 3 stars")
        return mats

    def action_feed(self, kind):
        if not self.ready():
            return
        try:
            mats = self.plan(kind)
        except GameError as e:
            self.notify(str(e), severity="warning")
            return
        t = self.items[self.target]
        total, lines = 0, []
        for step, m in enumerate(mats):
            c = cost(kind, t.stars + step, m.plus)
            total += c
            lines.append(f"  #{m.id}  {m.label}  base {m.base}  ≈{c}g")
        text = (f"[b]{kind.title()} {t.label}[/b] (Lv {t.level}/{t.level_cap}) "
                f"by consuming {len(mats)} item(s):\n\n" + "\n".join(lines)
                + f"\n\nEstimated cost {total} gold (you have {self.status.get('gold')}).\n"
                "[red]Consumed items are gone for good.[/red]")

        async def step(m):
            res = await tg(self.game.feed_one(kind, t, m))
            if "plus" in res:
                t.plus = res["plus"]
            else:
                t.stars = res["stars"]
            self.add_gold(-res["gold"])
            self.log_line(f"[green]{res['line']}[/green]")
            if kind == "enhance" and t.level >= t.level_cap:
                self.log_line(f"[yellow]{t.label} reached level cap, stopping[/yellow]")
                return False
            return True
        self.confirm(text, [m.id for m in mats], f"→ {t.label}", step)

    def action_sell(self):
        if not self.ready():
            return
        items = sorted((self.items[m] for m in self.marks), key=lambda i: i.id)
        if not items:
            self.notify("mark items to sell first (space / j)", severity="warning")
            return
        text = (f"[b]Sell {len(items)} item(s)[/b]:\n\n"
                + "\n".join(f"  #{i.id}  {i.label}  base {i.base}" for i in items)
                + "\n\n[red]Sold items are gone for good.[/red]")

        async def step(i):
            res = await tg(self.game.sell_one(i))
            self.add_gold(res["gold"])
            self.log_line(f"[green]{res['line']}[/green]")
            return True
        self.confirm(text, [i.id for i in items], "sell", step)

    def action_adventure(self):
        if not self.ready():
            return
        st = self.status
        text = ("[b]Adventure (full)[/b]\n\nRepeats until AP and BP run out:\n"
                "  1. Ancient alive and BP > 0: All-out (3 BP) or a 1 BP hit\n"
                "  2. Floor boss waiting and AP ≥ 3: fight the boss\n"
                "  3. AP > 0: adventure\n\n"
                f"Last known: AP {st.get('ap', '?')}, BP {st.get('bp', '?')}, "
                f"Ancient {st.get('ancient') or 'none'}.\nPress k to stop after the current fight.")

        self.confirm_run(text, self._adventure)

    async def _adventure(self):
        self.stop_requested = False
        try:
            totals, st = await tg(self.game.adventure_full(stop=lambda: self.stop_requested))
        finally:
            self.stop_requested = False
        self.status.update({k: v for k, v in st.items() if v is not None or k == "ancient"})
        self.save()
        self.log_line(f"[green]done: {totals['adventures']} adventures, {totals['bosses']} bosses, "
                      f"{totals['ancient_hits']} Ancient hits for {totals['ancient_damage']} damage, "
                      f"{totals['level_ups']} level-ups[/green]")

    def action_stop(self):
        if self.busy:
            self.stop_requested = True
            self.log_line("[yellow]stopping after the current step…[/yellow]")

    def add_gold(self, n):
        if self.status.get("gold") is not None:
            self.status["gold"] += n

    def confirm(self, text, ids, what, step):
        self.confirm_run(text, lambda: self._batch(ids, what, step))

    def confirm_run(self, text, make_coro):
        """Ask, then run `make_coro()` as the one active run."""
        def go(yes):
            if yes:
                self.run_worker(self._run(make_coro()), exclusive=True)
        self.push_screen(Confirm(text), go)

    # ---- equipment / inbox ----

    def action_equip(self):
        i = self.cursor_id()
        if i is None or not self.ready():
            return
        it = self.items[i]

        async def run():
            self.log_line(f"[green]{await tg(self.game.toggle_equip(it))}[/green]")
            await self._refresh()
        self.run_worker(self._run(run()), exclusive=True)

    def action_auto_equip(self):
        if not self.ready():
            return

        async def run():
            for line in await tg(self.game.auto_equip()):
                self.log_line(f"[green]{line}[/green]")
            await self._sync()
        self.confirm_run("[b]Auto-equip[/b]\n\nThe game's quick pick: highest class-adjusted ATK or DEF "
                         "first, within Capacity. Replaces your current gear.", run)

    def action_open(self, kind):
        if not self.ready():
            return
        st = self.status
        rule = ("50% per try. Repeats until the inbox is empty or keys or inventory space run out."
                if kind == "regular" else "Always works.")
        text = (f"[b]Open all chests with {kind} keys[/b]\n\n{rule}\n\n"
                f"Inbox {st.get('inbox', '?')}, keys {st.get('keys_regular', '?')} regular / "
                f"{st.get('keys_magical', '?')} magical.")

        async def run():
            found = await tg(self.game.open_chests(kind))
            for name in found:
                self.log_line(f"[green]+ {name}[/green]")
            self.log_line(f"opened {len(found)} chest(s)")
            await self._sync()
        self.confirm_run(text, run)

    async def _batch(self, ids, what, step):
        """Run `step(item)` for each id; each finished item leaves the cache. Resyncs at the end."""
        self.stop_requested = False
        try:
            for n, i in enumerate(ids, 1):
                if self.stop_requested:
                    self.log_line("[yellow]stopped by you[/yellow]")
                    break
                it = self.items.get(i)
                if it is None or self.eligible(it):  # locked or changed after confirming
                    self.log_line(f"[yellow]skipping #{i}[/yellow]")
                    continue
                self.log_line(f"[{n}/{len(ids)}] {it.label} {what}")
                keep_going = await step(it)
                del self.items[i]
                self.marks.discard(i)
                self.save()
                self.render_table()
                if not keep_going:
                    break
        finally:
            await self._refresh()

async def _dump(limit):
    if not CREDS.exists():
        sys.exit("run `lartian` first to log in")
    creds = json.loads(CREDS.read_text())
    game = Game(SESSION, creds["api_id"], creds["api_hash"])
    await game.start()
    print(f"wrote {await game.dump('dump.txt', limit)} messages to dump.txt")
    await game.stop()


def main():
    """`lartian` opens the TUI. `lartian dump [N]` writes the last N bot messages to ./dump.txt."""
    CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.argv[1:2] == ["dump"]:
        asyncio.run(_dump(int(sys.argv[2]) if len(sys.argv) > 2 else 200))
    else:
        Lartian().run()


if __name__ == "__main__":
    main()
