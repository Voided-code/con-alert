# CON alert

An unofficial helper for the browser game **Conflict of Nations**. It watches your game in the
background on a Mac and messages you when foreign units come into areas you care about, and it
gives you a live map of everything your side can see.

> Not made by or connected to the game's makers. It reads the game in a hidden browser window
> on your own Mac. Automated tools may be against the game's rules: use it at your own risk.

## What it does

- **Alerts:** draw boxes ("spots") on the map, or follow one of your groups. When an army that is
  not yours or an ally's enters, you get a message with a picture: iMessage/SMS, email or WhatsApp.
  Radar-only contacts count too.
- **Live map** (a web page, also on your phone): every unit your side can see, with the game's own
  symbols, fights, routes of allied units, sight and weapon ranges, and search by player or country.
- **Predictions:** where foreign groups that went out of sight probably are now, with how sure it is.
  Aircraft follow the game's patrol, return and refuel cycle.
- **Player activity:** when each player tends to give orders (a sign they are online), and the
  movements they repeat.
- **History:** every check is saved for 7 days; scroll back or play it back on the map.
- **Account:** log in and switch games from the page.

The game only shares the orders of countries you share intelligence with. For everyone else it
shows positions only, so their direction is worked out from how they move.

## Needs

- macOS (the hidden game window and iMessage sending use macOS)
- Python 3 and the Xcode command line tools (`xcode-select --install`), for `swiftc`
- A Conflict of Nations account

## Set up

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./conalert ui
```

Then on the page, under **Settings**:

1. **Game & account:** press **Log in** and log in on the game's own website in the window that opens
   (your password never passes through this tool; the login is remembered until you log out). Enter
   your game id (the number after `gameID=` in the game's address) and press **Watch this game**.
2. **Where alerts go:** add the numbers and email addresses to alert.
3. **Start monitor.**

Each game keeps its own spots, history and records. If you would rather use a file, copy
`config.example.json` to `config.json` and fill in `phone`, `country_code` and `game_id`.

## Use

```sh
./conalert start     # start watching in the background
./conalert ui        # open the map page (http://127.0.0.1:8765)
./conalert status    # is it running, last check
./conalert check     # one check now
./conalert test      # send a test alert
./conalert stop
```

Everything else (where alerts go, spots, email, WhatsApp) is set up on the page under **Settings**.
Your details, alert lists and history stay in files on your Mac that are not part of this repository.

## The code

| File | What it does |
| --- | --- |
| `con_alert.py` | The `./conalert` commands |
| `monitor.py` | The watch loop: reads the game every 20 s and sends alerts (each foreign group once) |
| `game.py` | The hidden game window, logging in, and reading the game's own data |
| `predict.py` | Where out-of-sight groups probably are, and learning from how right that was |
| `activity.py` | When players give orders (are online) |
| `history.py` | A saved copy of every check, for going back in time on the page |
| `delivery.py` | Messages, email and WhatsApp, and the recipient lists |
| `server.py` | The page server; the page itself is `ui.html`, `ui.css` and `ui.js` |
| `common.py` | Settings, file locations, the log |
| `viewer.swift` | The invisible window the game runs in |

Tests (they use a temporary folder, never your real data, and send nothing):

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
```
