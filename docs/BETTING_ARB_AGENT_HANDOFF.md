# Betting Arbitrage Agent — Session Handoff

Generated: June 18, 2026  
Branch: `assessment-latency-profiling`  
Repo: https://github.com/woozard/betting-arbitrage

## 1. What This Project Is

Automated sports betting arbitrage system that:

- Scrapes MLB moneyline odds from multiple offshore books
- Compares cross-book prices to find arbitrage (combined implied probability < threshold)
- Places matched bets on two books sequentially
- Sends alerts to a private Telegram ops group

**Production server:** AWS EC2 `ubuntu@100.53.194.189`  
**App path:** `/home/ubuntu/betting-arbitrage`  
**Systemd service:** `betting-arb` (runs `scheduler.py` + `jobs.yml`)

## 2. What We Did In This Agent Session

### Sports411 — fixed duplicate bets (critical)

**Problem:** SendBets API returned `WagerResult: false` even when bets were accepted (async post). Bot logged FAILED and retried every few seconds, placing many duplicate $25 bets.

**Fix:**

- Confirm via https://be.sports411.ag/en/open-bets/ (NOT `/pending` — returns 404)
- Match stake format: `Risk:$10.00`
- Poll open bets up to 45s after SendBets false
- Single Place Bet click — no retry loops
- Skip if bet already on open bets
- `MAX_WAGER_ATTEMPTS_PER_ARB = 1`

### Production betting flow — sequential, $20, private Telegram

- $20 stake per leg (`BET_STAKE=20`)
- S411 first → Betamapola waits for S411 leg confirmed in Redis
- Paradise first → Betamapola waits for Paradise leg (second active pair)
- Alerts only to **Kley Carlos Arbs** (`TELEGRAM_CHAT_OPS`) — old dev groups unused
- Per-leg + complete + partial exposure Telegram alerts

### Active book pairs (only these two)

| Pair | First leg | Second leg |
|------|-----------|------------|
| `sports411:betamapola` | Sports411 | Betamapola |
| `paradisewager:betamapola` | ParadiseWager | Betamapola |

BetWar and other combos are disabled in `jobs.yml`.

### ParadiseWager — verified & enabled

- API path: AddBet → SaveBet (with account password) → confirmBet
- API Amount = to-win, not risk — code converts $20 risk from American odds
- Manual test: Yankees $12 to-win → $18 risk / $12 win at -150 ✓
- Pair activated with sequential Paradise → Betamapola flow

### Telegram `/scan` command

- `telegram_ops_bot.py` polls ops chat for `/scan`
- Returns MLB moneyline table + arb % + ASCII visual for active pairs
- Only responds in `TELEGRAM_CHAT_OPS`

### Profit threshold

- `MIN_ARB_PROFIT_PCT=1.01` on server → only auto-bets when profit ≥ 1.01%
- Equivalent: total implied probability < 0.9899
- (Briefly tested at 1.03 for near-arbs; reverted to strict, then set to 1.01% min profit)

### Manual Baltimore test (S411 × Betamapola)

- S411: Baltimore Orioles +125 @ $20 ✓
- Betamapola: Seattle Mariners -136 @ $20 ✓ (required stopping scheduler — session conflict with `betamapola_odds`)

## 3. Architecture

```
scheduler.py (jobs.yml every 30s)
├── arbitrage.py          → scan DB odds, insert arbs, Telegram "Arbitrage"
├── sports411_odds.py     → scrape S411 → DB
├── betamapola_odds.py    → scrape Betamapola → DB
├── paradisewager_odds.py → scrape Paradise → DB
├── sports411_betting.py  → place S411 leg ($20)
├── paradisewager_betting.py → place Paradise leg ($20)
├── betamapola_betting.py → place Betamapola leg after first leg confirms
└── telegram_ops_bot.py   → /scan command (long-running)
```

**Cache (Redis):** arb opportunities, leg-placed flags, scan locks, alert dedup

**Betting order:**

```
Arb found → First book places $20 → mark leg in Redis
         → Betamapola sees first leg → places $20
         → Both confirmed → "Arbitrage Complete" Telegram
```

## 4. Current Server Configuration (.env)

| Variable | Purpose |
|----------|---------|
| `BET_STAKE=20` | $20 risk per leg |
| `SEQUENTIAL_ARB_BETTING=true` | Sequential legs + ops-only alerts |
| `MIN_ARB_PROFIT_PCT=1.01` | Min 1.01% profit to auto-bet |
| `ACTIVE_ARB_BOOK_PAIRS=sports411:betamapola,paradisewager:betamapola` | Allowed pairs only |
| `TELEGRAM_CHAT_OPS=-1003803751267` | Kley Carlos Arbs group |
| `TELEGRAM_BOT_TOKEN` | Existing bot (kleyman_arb_bot) |

Credentials live in server `.env` — copy from EC2, do not commit:

- `BETAMAPOLA_ACCOUNT` / `BETAMAPOLA_PASSWORD`
- `PARADIESWAGER_ACCOUNT` / `PARADIESWAGER_PASSWORD`
- S411 account hardcoded in `sports411_betting.py` (8715)

## 5. Telegram Alerts (Kley Carlos Arbs)

| Event | Message |
|-------|---------|
| Arb found | `===== Arbitrage =====` |
| Any leg confirmed | `===== Leg Confirmed (Real Money) =====` |
| Both legs done | `===== Arbitrage Complete =====` |
| One leg only | `===== Partial Arb (One Leg Only) =====` |
| Manual `/scan` | Full odds + arb % tables |

## 6. Key Files Changed

| File | Role |
|------|------|
| `controllers/Sports411Controller.py` | Open-bets confirm, single click, skip duplicates |
| `controllers/BetamapolaController.py` | Wait for first leg (S411 or Paradise) |
| `controllers/ParadiseWagerController.py` | Password on SaveBet, risk→win conversion |
| `controllers/ArbitrageController.py` | Pair filter, MIN_ARB_PROFIT threshold |
| `utils/bet_placement.py` | Ops Telegram: leg / complete / partial alerts |
| `utils/config.py` | BET_STAKE, pairs, MIN_ARB_PROFIT_PCT |
| `utils/arb_scan_report.py` | `/scan` report builder |
| `telegram_ops_bot.py` | `/scan` polling bot |
| `jobs.yml` | Active jobs only |
| `test_s411_place_bet.py` | Manual S411 E2E test |
| `test_betamapola_place_bet.py` | Manual Betamapola test |
| `test_paradisewager_place_bet.py` | Manual Paradise test |

## 7. EC2 Access

```bash
ssh -i "/path/to/pinn-arb-new.pem" ubuntu@100.53.194.189
cd /home/ubuntu/betting-arbitrage
```

**Deploy from laptop:**

```bash
rsync -avz --exclude '.git' --exclude 'venv' --exclude 'logs' \
  -e 'ssh -i "PEM"' ./ ubuntu@100.53.194.189:/home/ubuntu/betting-arbitrage/
sudo systemctl restart betting-arb
```

**Monitor:**

```bash
sudo journalctl -u betting-arb -f
tail -f logs/sports411_betting.log logs/betamapola_betting.log logs/arbitrage.log
```

**Maintenance:**

```bash
./stop_for_maintenance.sh    # stop
./start_after_maintenance.sh # start
```

## 8. Continue On Your Laptop

```bash
git clone https://github.com/woozard/betting-arbitrage.git
cd betting-arbitrage
git checkout assessment-latency-profiling
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**Copy `.env` from EC2:**

```bash
scp -i PEM ubuntu@100.53.194.189:/home/ubuntu/betting-arbitrage/.env .
```

**Recent commits (newest first):**

- `01cbe72` — `/scan` bot, MIN_ARB_PROFIT_PCT, Betamapola test
- `c91edfa` — Paradise+Betamapola pair
- `f7f204e` — S411 sequential + ops Telegram
- `8bb3d98` — S411 open-bets fix (duplicate wagers)

**Manual tests (on EC2 with xvfb):**

```bash
xvfb-run -a venv/bin/python3 test_s411_place_bet.py --team-name "Team" --stake 20
xvfb-run -a venv/bin/python3 test_betamapola_place_bet.py --team-name "Team" --stake 20
xvfb-run -a venv/bin/python3 test_paradisewager_place_bet.py --team-name "Team" --stake 20
venv/bin/python3 show_arb_pairs.py   # cross-book scan from DB
```

**Telegram:** Send `/scan` in Kley Carlos Arbs.

## 9. Why Arbs May Not Execute

Scanner requires profit ≥ 1.01% (total prob < 0.9899). Typical MLB cross-book lines sit at 1.01–1.03 (negative edge) — no auto-bets is normal.

Use `/scan` to see live status. Closest pairs are often -1% to -2.6%.

## 10. Known Issues & Warnings

- **Duplicate open bets on S411** from pre-fix retries (~$2k at risk) — cancel manually on book
- **Betamapola session conflict:** only one wager session per account — stop `betting-arb` before manual Betamapola tests
- **ParadiseWager from Serbia:** needs US VPN or EC2; bot uses BrightData proxy
- **Paradise stake:** API uses to-win amount; $20 risk is converted in code
- **S411 attach/xdotool experiments** exist but are NOT used in production

## 11. Agent Context For Cursor

When resuming in a new agent/chat on your laptop:

> This is the betting-arbitrage repo on branch `assessment-latency-profiling`. Production runs on EC2 100.53.194.189. Active pairs: sports411×betamapola and paradisewager×betamapola at $20 sequential. S411 confirms via `/en/open-bets/`. Telegram ops chat: Kley Carlos Arbs. `MIN_ARB_PROFIT_PCT=1.01`. Read `docs/BETTING_ARB_AGENT_HANDOFF.md`.
