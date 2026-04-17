# Blood Bot — Command List

## General
| Command | Description |
|---|---|
| `@blood <message>` | Talk to Blood |
| `!reset` | Clear Blood's conversation memory in this channel |
| `!debug` | Show debug info (model, tokens, rate limits) |
| `!fastdebug` | Toggle real-time trace logs in chat |
| `!compact` | Force-compact conversation history (mod+) |

## Coins
| Command | Aliases | Description |
|---|---|---|
| `!coins` | `!bal`, `!balance` | Check your coin balance |
| `!coins @user` | | Check someone else's balance |
| `!leaderboard` | `!lb` | Top 10 richest members |
| `!addcoins @user <amount>` | | Admin: manually add/remove coins |

> Blood also gives/takes coins autonomously via AI — reward for smart questions, punishment for dumb ones.

## Gambling
| Command | Aliases | Description |
|---|---|---|
| `!coinflip <amount>` | `!cf`, `!flip` | 50/50 double or nothing |
| `!slots <amount>` | `!slot` | Slot machine — 3-match = 3x (💎 = 5x), 2-match = 1.5x |
| `!duel @user <amount>` | | PvP coin battle, 50/50, both need enough coins |

## Blood Market — Real-Time Trading
| Command | Aliases | Description |
|---|---|---|
| `!market` | `!m`, `!stocks`, `!prices` | Market overview — all stocks, crypto, commodities |
| `!market <ticker>` | | Detailed view with 1-month price chart |
| `!buy <ticker> <coins>` | | Invest BHC coins at real market price |
| `!sell <ticker>` | | Sell all shares of a ticker |
| `!sell <ticker> <coins>` | | Partial sell |
| `!portfolio` | `!port`, `!holdings` | View your holdings with live P&L |

### Supported Tickers
**Stocks:** `NVDA`, `AAPL`, `TSLA`, `MSFT`, `GOOG`, `AMZN`, `META`, `AMD`
**Crypto:** `BTC`, `ETH`, `SOL`, `DOGE`
**Commodities:** `GOLD`, `SILVER`, `OIL`

> Any valid Yahoo Finance ticker works too (e.g. `!market PLTR`, `!buy RBLX 50`).

## How Market Trading Works
1. Use `!buy NVDA 100` to invest 100 BHC coins in NVIDIA at the current real price
2. Your coins are converted to fractional shares based on the real stock price
3. When you `!sell`, your shares are converted back to BHC coins at the current price
4. If the stock went up → you get more coins back. Down → you get fewer.
5. Real market movements = real BHC gains/losses

## Remote Terminal (Admin+)
| Command | Aliases | Description |
|---|---|---|
| `!openterminal` | `!ot` | Open a remote terminal session — gives Blood control of the host machine |
| `!closeterminal` | `!ct` | Close the remote terminal session |

When a terminal session is active, Blood gains access to:
- **run_terminal_command** — execute shell commands
- **open_url_browser** — open URLs in Chrome (porn sites blocked)
- **view_screen** — screenshot + AI vision description
- **keyboard_type** — type text at cursor
- **press_key** — press keys/combos (enter, ctrl+c, etc.)
- **mouse_click** — click at screen coordinates
- **scroll_screen** — scroll up/down

> Auto-screenshots are sent every 2 seconds while the session is open.

## Permission Tiers
```
blacklisted < user < mod < admin < owner
```
- **Everyone**: coins, gambling, market, leaderboard
- **Mod+**: compact
- **Admin+**: addcoins, openterminal, closeterminal
- **Owner**: all
