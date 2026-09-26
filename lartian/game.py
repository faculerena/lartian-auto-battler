"""Telegram side: tap buttons on @lartianquestbot, wait for replies, parse screens."""
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import BotResponseTimeoutError

BOT = "lartianquestbot"
# messages the bot pushes on its own, not in reply to a tap
NEWS = re.compile(r"(⚡ .+ discovered the Ancient|⚡ Your AP pool is full|🏆 .+ HAS BEEN DEFEATED|💨 .+ vanished)")
RARITY = 6  # ponytail: Mythic only; costs are wrong for other rarities


class GameError(Exception):
    pass


@dataclass
class Item:
    id: int
    name: str        # "Mythic Crystal Staff"
    plus: int        # +21
    stars: int
    equipped: bool
    atk: int = 0
    defense: int = 0
    capacity: int = 0
    gear: str = ""

    @property
    def level(self):
        return self.plus + 1  # the item screen shows "+21" as "Item level 22/55"

    @property
    def level_cap(self):
        return 10 + RARITY * 5 + 5 * self.stars

    @property
    def base(self):
        """Main stat with level and star bonuses removed: the item's roll, i.e. its potential."""
        return round(max(self.atk, self.defense) / ((1 + 0.05 * self.plus) * (1 + 0.18 * self.stars)))

    @property
    def label(self):
        return f"{self.name} +{self.plus}{'★' * self.stars}"


# ---- parsers: plain text + [(button_text, data)] in, data out ----

HEAD = re.compile(r"^([✓○]) (.+?) \+(\d+)(★*)$")


def buttons(msg):
    return [(b.text, b.data.decode()) for row in msg.buttons or [] for b in row
            if getattr(b, "data", None)]


def parse_inventory(text, btns):
    """Returns (items, page, pages) or None if this isn't an inventory screen."""
    m = re.search(r"Inventory \d+/\d+ · Page (\d+)/(\d+)", text)
    if not m:
        return None
    blocks = [b for b in text.split("\n\n") if b[:1] in "✓○"]
    ids = [int(d.split(":")[1]) for _, d in btns if re.fullmatch(r"item:\d+:\d+", d)]
    if len(blocks) != len(ids):
        raise GameError(f"inventory: {len(blocks)} item blocks but {len(ids)} buttons")
    items = []
    for id_, block in zip(ids, blocks):
        lines = block.splitlines()
        h = HEAD.match(lines[0])
        if not h:
            raise GameError(f"can't parse item line: {lines[0]!r}")
        it = Item(id_, h[2], int(h[3]), len(h[4]), h[1] == "✓")
        for line in lines[1:]:
            if s := re.match(r"Adds ATK (\d+), DEF (\d+)", line):
                it.atk, it.defense = int(s[1]), int(s[2])
            elif s := re.match(r"Capacity cost: (\d+) · (\w+) gear", line):
                it.capacity, it.gear = int(s[1]), s[2]
        items.append(it)
    return items, int(m[1]), int(m[2])


def parse_counts(text):
    """Inventory header totals: (items, equipped), or None if this isn't an inventory screen."""
    n = re.search(r"Inventory (\d+)/\d+", text)
    eq = re.search(r"Equipped: (\d+) /", text)
    return n and eq and (int(n[1]), int(eq[1]))


def parse_status(text):
    def num(pattern):
        m = re.search(pattern, text)
        return int(m[1]) if m else None
    ancient = re.search(r"Current Ancient: (.+)", text)
    return {"gold": num(r"Gold: (\d+)"), "level": num(r"\(Level (\d+)\)"),
            "ap": num(r"AP: (\d+)"), "bp": num(r"BP: (\d+)"),
            "ancient": ancient[1].strip() if ancient else None,
            "inbox": num(r"Inbox: (\d+) /"),
            "keys_regular": num(r"Keys: (\d+) Regular"), "keys_magical": num(r"Keys: \d+ Regular, (\d+) Magical")}


def next_fight(st, boss_ready, skip_ancient=False):
    """Adventure (full) priority. Returns ("ancient", n_bp) / ("boss",) / ("adventure",) / None."""
    if st["ancient"] and st["bp"] and not skip_ancient:
        # All-out is ~3.5x a normal hit for 3 BP, so it's the best use of a full bar.
        # Below 3, hit now: a level-up refills BP to 3 anyway, so saving it gains nothing.
        return ("ancient", 3 if st["bp"] >= 3 else 1)
    if boss_ready and st["ap"] >= 3:
        return ("boss",)
    if st["ap"] > 0:
        return ("adventure",)
    return None


def parse_preview(text):
    m = re.search(r"^(Enhance|Evolve) (.+)\nConsume (.+)\nCost: (\d+) gold", text, re.M)
    return m and {"target": m[2], "material": m[3], "cost": int(m[4])}


def parse_result(text):
    first = text.splitlines()[0] if text else ""
    if m := re.search(r"Enhanced .+? to \+(\d+) for (\d+) gold", first):
        return {"plus": int(m[1]), "gold": int(m[2]), "line": first}
    if m := re.search(r"Evolved .+? to (★+) for (\d+) gold", first):
        return {"stars": len(m[1]), "gold": int(m[2]), "line": first}
    return None


def strip_level(label):
    """'Mythic Crystal Staff +21★★★' -> 'Mythic Crystal Staff'."""
    return re.sub(r" \+\d+★*$", "", label)


def cost(kind, target_stars, material_plus):
    """Forge formula; evolve cost depends on the star being reached."""
    if kind == "enhance":
        return 10 * RARITY * (material_plus + 1)
    return 100 * RARITY * (target_stars + 1)


# ---- driver ----

def find(msg, pattern, text=None):
    for t, d in buttons(msg):
        if re.fullmatch(pattern, d) and (text is None or t.startswith(text)):
            return d
    return None


class Game:
    def __init__(self, session, api_id, api_hash):
        self.client = TelegramClient(str(session), int(api_id), api_hash)
        self.q = asyncio.Queue()
        self.cur = None  # last bot message we navigated to
        self.seen = {}  # msg id -> recent bot messages with buttons, see recall()
        self.log = print

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise GameError("not logged in")
        async def handler(e):
            self.q.put_nowait(e.message)
        self.client.add_event_handler(handler, events.NewMessage(chats=BOT, incoming=True))
        self.client.add_event_handler(handler, events.MessageEdited(chats=BOT, incoming=True))

    async def stop(self):
        await self.client.disconnect()

    def _drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    async def _reply(self, timeout):
        """Next bot message. Keeps the last one within a short window, and prefers real replies
        over news broadcasts (discoveries, other players' kills) that can arrive at any time."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        reply = news = None
        while True:
            if reply:
                wait = 0.4
            elif news:
                wait = min(3, deadline - loop.time())  # maybe the news *is* the reply
            else:
                wait = deadline - loop.time()
            try:
                msg = await asyncio.wait_for(self.q.get(), max(wait, 0))
            except TimeoutError:
                if not (reply or news):
                    raise
                break
            if NEWS.match(msg.raw_text or ""):
                news = msg
                self.log(f"news: {msg.raw_text.splitlines()[0]}")
            else:
                reply = msg
        self.cur = reply or news
        if self.cur.buttons:
            self.seen[self.cur.id] = self.cur
            if len(self.seen) > 300:  # ponytail: plain size cap, oldest first
                del self.seen[next(iter(self.seen))]
        return self.cur

    def recall(self, pattern):
        """Newest seen message with a button matching `pattern`. Old bot messages keep their
        buttons and the bot answers them (it replies with a new message and never edits the old
        one), so this jumps straight to a screen instead of navigating there."""
        for msg in reversed(self.seen.values()):
            if find(msg, pattern):
                return msg
        return None

    async def home(self):
        self._drain()
        await self.client.send_message(BOT, "/start")
        try:
            return await self._reply(15)
        except TimeoutError:
            raise GameError("bot didn't answer /start")

    async def tap(self, msg, pattern, text=None):
        data = find(msg, pattern, text)
        if data is None:
            raise GameError(f"no button {pattern!r} on screen: {msg.raw_text.splitlines()[0]!r}")
        self._drain()
        popup = None
        try:
            answer = await msg.click(data=data.encode())
            popup = getattr(answer, "message", None)
        except BotResponseTimeoutError:
            pass
        try:
            # a popup with no new message is how the game reports errors
            return await self._reply(5 if popup else 15)
        except TimeoutError:
            raise GameError(popup or f"no reply after tapping {data}")

    # ---- high level ----

    async def sync(self):
        """Walks every inventory page. Returns (items, status)."""
        msg = await self.home()
        status = parse_status(msg.raw_text)
        msg = await self.tap(msg, r"inventory:1")
        items = []
        while True:
            parsed = parse_inventory(msg.raw_text, buttons(msg))
            if not parsed:
                raise GameError("expected inventory screen")
            page_items, page, pages = parsed
            items += page_items
            self.log(f"synced page {page}/{pages}")
            if page >= pages:
                return items, status
            msg = await self.tap(msg, rf"inventory:{page + 1}", "Next")

    async def _open_item(self, target):
        msg = self.recall(rf"item:{target}:\d+")  # every item after a sync
        if msg is None:
            msg = self.cur
            if msg is None or not find(msg, r"inventory:1"):
                msg = await self.home()
            msg = await self.tap(msg, r"inventory:1")
            while not find(msg, rf"item:{target}:\d+"):
                if not find(msg, r"inventory:\d+", "Next"):
                    raise GameError(f"item {target} not in inventory")
                msg = await self.tap(msg, r"inventory:\d+", "Next")
        return await self.tap(msg, rf"item:{target}:\d+")

    async def feed_one(self, kind, target, material):
        """Consume `material` into `target` (Items from the cache). Returns parse_result dict."""
        pick = rf"preview:{kind}:{target.id}:{material.id}:\d+:\d+"
        msg = self.recall(pick)  # a materials list from an earlier step of the batch
        if msg is None:
            msg = await self._open_item(target.id)
            msg = await self.tap(msg, rf"materials:{kind}:{target.id}:\d+:1")
            while not find(msg, pick):
                if not find(msg, rf"materials:{kind}:.*", "Next"):
                    raise GameError(f"{material.label} (#{material.id}) not offered as material")
                msg = await self.tap(msg, rf"materials:{kind}:.*", "Next")
        msg = await self.tap(msg, pick)

        pv = parse_preview(msg.raw_text)
        if not pv:
            raise GameError(f"expected confirmation, got: {msg.raw_text.splitlines()[0]!r}")
        if strip_level(pv["target"]) != target.name or pv["material"] != material.label:
            await self.tap(msg, rf"materials:{kind}:.*")  # Cancel
            raise GameError(f"confirmation shows {pv['target']} <- {pv['material']}, "
                            f"expected {target.name} <- {material.label}; press r to resync")

        msg = await self.tap(msg, rf"upgrade:{kind}:{target.id}:{material.id}:\d+")
        res = parse_result(msg.raw_text)
        if not res:
            raise GameError(f"unexpected result: {msg.raw_text.splitlines()[0]!r}")
        return res

    async def sell_one(self, item):
        """Sell `item` (from the cache). Returns {"gold", "line"}."""
        msg = await self._open_item(item.id)
        msg = await self.tap(msg, rf"sellask:{item.id}:\d+")
        m = re.search(r"^Sell (.+) for (\d+) gold\?", msg.raw_text, re.M)
        if not m or m[1] != item.label:
            await self.tap(msg, rf"item:{item.id}:\d+")  # Cancel
            raise GameError(f"sell screen doesn't match {item.label}; press r to resync")
        msg = await self.tap(msg, rf"selldo:{item.id}:\d+")
        return {"gold": int(m[2]), "line": msg.raw_text.splitlines()[0]}

    async def adventure_full(self, stop=lambda: False):
        """Spend BP on the Ancient, AP on the floor boss, the rest on adventures.
        Re-checks home after every fight, so BP refilled by a level-up goes to the Ancient.
        Returns (totals, last status)."""
        totals = {"adventures": 0, "bosses": 0, "ancient_hits": 0, "ancient_damage": 0, "level_ups": 0}
        skip_ancient = False
        msg = await self.home()
        while True:
            st = parse_status(msg.raw_text)
            if st["bp"] and not skip_ancient and find(msg, r"ancientattack:\d+:1"):
                # still on the Ancient with BP left: hit again, skip Home
                st["ancient"] = re.search(r"🌌 ([^,(]+)", msg.raw_text)[1].strip()
            elif st["ap"] is None or not find(msg, "adventure"):
                msg = await self.tap(msg, "status") if find(msg, "status") else await self.home()
                st = parse_status(msg.raw_text)
            if stop():
                self.log("stopped by you")
                return totals, st
            action = next_fight(st, bool(find(msg, "boss")), skip_ancient)
            if action is None:
                self.log(f"out of AP and BP (AP {st['ap']}, BP {st['bp']})")
                return totals, st
            if action[0] == "ancient":
                attack = rf"ancientattack:\d+:{action[1]}"
                screen = msg if find(msg, attack) else await self.tap(msg, "ancient")
                if not find(screen, attack):
                    self.log(f"no attack button on the Ancient screen, skipping it: "
                             f"{screen.raw_text.splitlines()[0]!r}")
                    skip_ancient, msg = True, screen
                    continue
                msg = await self.tap(screen, attack)
                if d := re.search(r"(\d+) damage!", msg.raw_text):
                    totals["ancient_hits"] += 1
                    totals["ancient_damage"] += int(d[1])
            else:
                msg = await self.tap(msg, action[0])
                totals["bosses" if action[0] == "boss" else "adventures"] += 1
            first = msg.raw_text.splitlines()[0]
            if action[0] == "ancient" and NEWS.match(msg.raw_text):
                self.log(f"ancient: {first}")  # it died or escaped; home will show it gone
                continue
            if "Spent" not in msg.raw_text:  # the fight didn't happen; don't loop on it
                raise GameError(f"{action[0]} didn't go through: {first!r}")
            self.log(f"{action[0]}: {first}")
            if m := re.search(r"Level up! You are now level (\d+)", msg.raw_text):
                totals["level_ups"] += 1
                self.log(f"level up → {m[1]}")

    async def dump(self, path, limit=200):
        """Write the last `limit` messages with the bot (text + button data) to `path`."""
        out = []
        async for m in self.client.iter_messages(BOT, limit=limit):
            block = [f"--- #{m.id} {'ME' if m.out else 'BOT'} {m.date:%Y-%m-%d %H:%M}", m.raw_text or ""]
            for row in m.buttons or []:
                block.append("   buttons: " + " | ".join(
                    f"{b.text} [{b.data.decode(errors='replace')}]" if getattr(b, "data", None) else b.text
                    for b in row))
            out.append("\n".join(block))
        Path(path).write_text("\n\n".join(reversed(out)))
        return len(out)

    async def toggle_equip(self, item):
        """Equip or unequip `item`. Returns the game's reply line."""
        msg = await self._open_item(item.id)
        msg = await self.tap(msg, rf"equip:{item.id}:\d+")
        first = msg.raw_text.splitlines()[0]
        if not re.match(r"(Equipped|Unequipped) ", first):
            raise GameError(first)
        return first

    async def auto_equip(self):
        """The game's Auto Equip Best Gear. Returns its summary lines."""
        msg = self.recall(r"autoequip:\d+") or await self.tap(await self.home(), r"inventory:1")
        msg = await self.tap(msg, r"autoequip:\d+")
        lines = msg.raw_text.splitlines()
        if not lines[0].startswith("🛡 Auto-equipped"):
            raise GameError(lines[0])
        return lines[:2]

    async def open_chests(self, kind):
        """Bulk-open inbox chests with `kind` keys ("regular" / "magical"). Regular keys break half
        the time, so batches repeat until the inbox is empty or a batch uses no keys
        (out of keys or inventory full). Returns the item names found."""
        found = []
        msg = await self.tap(self.recall(r"inbox:1") or await self.home(), r"inbox:1")
        for _ in range(100):  # safety cap
            if not find(msg, r"openallmenu:\d+") and "Your inbox is empty" not in msg.raw_text:
                msg = await self.tap(self.recall(r"inbox:1") or await self.home(), r"inbox:1")
            if not find(msg, r"openallmenu:\d+"):
                self.log("inbox empty")
                return found
            msg = await self.tap(msg, r"openallmenu:\d+")
            msg = await self.tap(msg, rf"openallask:{kind}:\d+")
            msg = await self.tap(msg, rf"openalldo:{kind}:\d+")
            m = re.search(r"Used (\d+) \w+ Key\(s\): (\d+) chest\(s\) opened", msg.raw_text)
            if not m:
                raise GameError(msg.raw_text.splitlines()[0])
            items = re.search(r"Items found:\n(.+?)\n\n", msg.raw_text, re.S)
            found += items[1].splitlines() if items else []
            self.log(msg.raw_text.splitlines()[0])
            if int(m[1]) == 0 or kind == "magical":
                return found
        return found
