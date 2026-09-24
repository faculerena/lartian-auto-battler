"""Parser and decision checks against real bot messages. Run: uv run tests/test_parse.py"""
import re
from pathlib import Path
from lartian.game import (NEWS, Item, next_fight, parse_inventory, parse_preview, parse_result,
                          parse_status, strip_level)

RESULT = Path(__file__).with_name("sample.txt").read_text()


def split(raw):
    """`lartian dump` format -> (text, [(button_text, data)])"""
    text, btns = [], []
    for line in raw.splitlines():
        if line.startswith("   buttons: "):
            btns += re.findall(r"(.+?) \[([^\]]+)\](?: \| |$)", line[12:])
        else:
            text.append(line)
    return "\n".join(text).strip(), btns


text, btns = split(RESULT)
items, page, pages = parse_inventory(text, btns)
assert (page, pages, len(items)) == (1, 5, 10)
staff = next(i for i in items if i.id == 2007)
assert (staff.name, staff.plus, staff.stars, staff.equipped) == ("Mythic Crystal Staff", 21, 3, True)
assert (staff.atk, staff.defense, staff.capacity, staff.gear) == (299, 68, 6, "Arcane")
helm = items[-1]
assert (helm.id, helm.label, helm.equipped, helm.gear) == (3717, "Mythic Iron Helm +0", False, "Heavy")

assert parse_result(text) == {"plus": 21, "gold": 60, "line": text.splitlines()[0]}
assert parse_result("✨ Evolved Crystal Staff to ★★ for 1200 gold. Its stats and level cap increased.")["stars"] == 2
assert parse_result("🎒 Inventory 41/50 · Page 1/5") is None

pv = parse_preview("⚠️ The material will be permanently consumed.\n\nEnhance Mythic Crystal Staff +21★★★\n"
                   "Consume Mythic Leather Vest +0\nCost: 60 gold")
assert pv == {"target": "Mythic Crystal Staff +21★★★", "material": "Mythic Leather Vest +0", "cost": 60}
assert strip_level(pv["target"]) == "Mythic Crystal Staff"

assert parse_status("🔮 Player (Level 108)\n• Gold: 45589\n\nAP: 29 / 104")["gold"] == 45589
# base stays constant (±1, the game rounds) as the same item levels and evolves; values from the dump
for stats in ([(0, 0, 200), (0, 1, 237), (0, 2, 272)],                  # 3750 evolving
              [(21, 3, 299), (22, 3, 307), (23, 3, 315), (25, 3, 329)]):  # 2007 enhancing
    bases = [Item(0, "x", p, s, False, atk=a).base for p, s, a in stats]
    assert max(bases) - min(bases) <= 1, bases
# adventure (full) priorities
home = parse_status("🔮 Player (Level 112)\n• Current Ancient: Starwyrm\n• Gold: 51944\n\nAP: 23 / 108\nBP: 3 / 3")
assert home["ancient"] == "Starwyrm" and home["ap"] == 23 and home["bp"] == 3
assert next_fight(home, boss_ready=True) == ("ancient", 3)
assert next_fight({**home, "bp": 2}, True) == ("ancient", 1)
assert next_fight({**home, "bp": 0}, True) == ("boss",)
assert next_fight({**home, "bp": 3}, True, skip_ancient=True) == ("boss",)
assert next_fight({**home, "bp": 0, "ap": 2}, True) == ("adventure",)
assert next_fight({**home, "ancient": None, "ap": 5}, False) == ("adventure",)
assert next_fight({**home, "ancient": None, "ap": 0}, True) is None
assert next_fight({**home, "ap": 0, "bp": 0}, True) is None
assert parse_status("🔮 Player (Level 112)\nAP: 1 / 108\nBP: 0 / 3")["ancient"] is None
for news in ["⚡ Someone discovered the Ancient Tidemother!", "🏆 STARWYRM HAS BEEN DEFEATED!",
             "💨 Stormcrown vanished! No victory rewards this time.", "⚡ Your AP pool is full! Ready for another adventure?"]:
    assert NEWS.match(news), news
for reply in ["💥 You've defeated a Tower Floor #87 tier Voidling.", "🏆 Void Emperor falls! You advanced to Tower Floor #88.",
              "⚔️ All-out attack: 4172 damage!", "🔮 Player (Level 112)"]:
    assert not NEWS.match(reply), reply
st = parse_status("• Inbox: 13 / 100\n• Keys: 83 Regular, 95 Magical\nAP: 1 / 108")
assert (st["inbox"], st["keys_regular"], st["keys_magical"]) == (13, 83, 95)
print("ok")
