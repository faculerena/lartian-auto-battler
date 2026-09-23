# lartian

Terminal UI and bot for [@lartianquestbot](https://t.me/lartianquestbot). It logs in as your
Telegram account and taps the game's buttons for you.

- Inventory table: sort, filter, lock, Base stat (an item's roll without upgrades)
- Enhance / evolve a target with marked items
- Sell marked items (respects locks)
- Adventure (full): spend BP on the Ancient, AP on the floor boss, the rest on adventures

Automating a game may break its rules. Your account, your risk.

## Install

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uvx --from git+https://github.com/faculerena/lartian-auto-battler lartian
```

First run asks for:

1. API ID and hash: https://my.telegram.org → API development tools → create an app (any name)
2. Phone number and the login code Telegram sends you
3. 2FA password, if you have one

Everything is stored in `~/.config/lartian/`. `lartian.session` there is full access to your
Telegram account. Don't share it.

## Keys

| Key | Action |
|---|---|
| `r` | sync inventory |
| `t` | set target |
| `space` | mark / unmark as material |
| `j` | mark junk (+0, no stars, not the best copy of its item) |
| `c` | clear marks |
| `l` | lock / unlock |
| `e` / `v` | enhance / evolve target with marked items |
| `x` | sell marked items |
| `p` | Adventure (full) |
| `k` | stop current run |
| `s` / `S` / click header | sort / reverse |
| `f` | only items that can be eaten |
| `/` | filter by name |
| `q` | quit |

Locked and equipped items are never eaten or sold. Every run asks for confirmation first.

## Development

```sh
uv run lartian              # run from a clone
uv run tests/test_parse.py  # parser tests
uv run lartian dump 400     # save the last 400 bot messages to ./dump.txt, to map new screens
```

`DESIGN.md` explains how it works. `GAME.md` maps the bot's screens and button data.
