# Summer 6-Pack 2026 — GitHub-hosted live dashboard

Everything runs on GitHub, no Mac required:
- **GitHub Pages** serves `index.html` (the dashboard).
- **GitHub Actions** (`.github/workflows/push-actuals.yml`) runs `push_actuals_ci.py` every 15 min, queries Commerce7 for the campaign window, and pushes actuals to the JSONbin the dashboard polls.
- `audiences.json.enc` is the customer→audience map, **AES-encrypted** — decrypted at run time from a secret, so nothing sensitive is public.

## One-time setup

### 1. Create the repo and push
Create a new **public** repo on github.com (empty, no README), then from this folder:
```
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

### 2. Add Actions secrets
Repo **Settings → Secrets and variables → Actions → New repository secret**, add all 6:

| Secret | Value |
|---|---|
| `COMMERCE7_APP_ID` | from `~/george/.env` |
| `COMMERCE7_SECRET` | from `~/george/.env` |
| `COMMERCE7_TENANT` | from `~/george/.env` |
| `JSONBIN_BIN_ID` | `6a559cfef5f4af5e298b6d6e` |
| `JSONBIN_ACCESS_KEY` | your jsonbin Access Key (`$2a$10$cSm…`) |
| `AUDIENCE_KEY` | (the AES passphrase — provided separately) |

### 3. Enable Pages
**Settings → Pages → Build and deployment → Source: Deploy from a branch → `main` / `root` → Save.**
Your dashboard will be at `https://<you>.github.io/<repo>/`.

### 4. Test + connect
- **Actions tab → "Push Summer 6-Pack actuals" → Run workflow** (manual trigger). The log should end with `[pushed] … N txn / $X`.
- Open the Pages URL → **⚙ Connect** → paste the **Bin ID** and **Access Key**. Live.

## Notes
- The cron `*/15 * * * *` is UTC and best-effort — GitHub may delay a run by a few minutes under load. Bump to `*/10` or `*/5` in the workflow if you want tighter (still subject to GitHub's scheduling).
- To change audiences later: regenerate `audiences.json`, re-encrypt (`openssl enc -aes-256-cbc -pbkdf2 -in audiences.json -out audiences.json.enc -pass pass:$AUDIENCE_KEY`), commit, push.
- The dashboard logic is unchanged from the Netlify version; only the pusher's host moved.
- `audiences.json` (plaintext) is gitignored — only the `.enc` is ever committed.
