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
- A Conflict of Nations account, signed in once in Safari

## Set up

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json   # then fill in your details
```

`config.json`:

| Setting | What it is |
|---|---|
| `phone` | the number alerts go to by iMessage |
| `country_code` | added in front of numbers typed the local way |
| `game_id` | the `gameID` in your game's address |
| `user_id` | the `uid` in your game's address |

You can also log in and pick the game on the page instead: **Settings → Game & account**. Log in
happens on the game's own website inside the background window (your password never passes through
this tool) and is remembered until you log out. Each game keeps its own spots, history and records.

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
