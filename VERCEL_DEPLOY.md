# Vercel Deployment Guide

## Prerequisites

1. **Supabase Account**: Create a free account at https://supabase.com
2. **Vercel Account**: Create a free account at https://vercel.com
3. **GitHub Account**: This repo needs to be pushed to GitHub for Vercel deployment

## Step 1: Setup Supabase Database

1. Create a new Supabase project
2. Go to SQL Editor in Supabase dashboard
3. Run the migration script from `supabase_migration.sql`:
   - Copy the entire content of `supabase_migration.sql`
   - Paste into SQL Editor and click "Run"
   - You should see "Migration completed successfully!"

4. Get your Supabase credentials:
   - Go to Project Settings → API
   - Copy **Project URL** (SUPABASE_URL)
   - Copy **service_role key** (SUPABASE_KEY) - this is the secret key, not the anon key!

## Step 2: Push to GitHub

```bash
# Initialize git and push to your GitHub repo
git init
git add .
git commit -m "Initial commit - Vercel migration"
git branch -M main
git remote add origin https://github.com/Xwuan19/Locket-Username.git
git push -u origin main
```

## Step 3: Deploy to Vercel

1. Go to https://vercel.com and sign in
2. Click "New Project"
3. Import your GitHub repository `Xwuan19/Locket-Username`
4. Vercel will auto-detect it as a Python/Flask project
5. **Don't click Deploy yet!** Set environment variables first (Step 4)

## Step 4: Configure Environment Variables

In Vercel project settings, add these environment variables:

### Required Variables:
```
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=your-service-role-key-from-supabase
SITE_PASSWORD=your-livestream-access-code
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-admin-panel-password
FLASK_SECRET_KEY=generate-random-32-char-string
BEHIND_HTTPS=1
```

### Optional Variables:
```
EMAIL=your-locket-account@example.com
PASSWORD=your-locket-password
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=your-telegram-chat-id
gist_token_url=https://gist.githubusercontent.com/.../tokens.json
```

**To generate FLASK_SECRET_KEY**: Run this in terminal:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Step 5: Deploy!

1. Click "Deploy" in Vercel
2. Wait 1-2 minutes for build to complete
3. Visit your deployment URL (e.g., `https://your-project.vercel.app`)

## Step 6: First-Time Setup

1. Visit your site → you'll see the access code login screen
2. Enter your `SITE_PASSWORD` to access the main UI
3. Go to `/admin/login` to access the admin panel
4. Add your Locket accounts via Admin Panel → Accounts
5. Add RevenueCat tokens via Admin Panel → Tokens

## Testing

1. Enter a Locket username in the main UI
2. Click "Check User" → should see avatar and name
3. Click "Unlock Gold" → wait 10-30 seconds for result
4. Check "Recent Activity" to see the unlock history

## Troubleshooting

### "Supabase connection check failed"
- Double-check your `SUPABASE_URL` and `SUPABASE_KEY` in Vercel environment variables
- Make sure you used the **service_role** key, not the anon key
- Verify you ran `supabase_migration.sql` in Supabase SQL Editor

### "0 accounts configured"
- Go to `/admin/login` and add a Locket account via Admin Panel
- Or set `EMAIL` and `PASSWORD` env vars (will auto-seed one account on first boot)

### "No restore payloads available"
- Go to `/admin/login` and add RevenueCat tokens via Admin Panel → Tokens
- Or set `gist_token_url` env var pointing to a JSON array of token payloads

### Unlock taking too long / timing out
- First unlock after cold start takes ~10-15s (Firebase login)
- Subsequent unlocks should be faster (~5-10s)
- If consistently timing out, check Vercel function logs for errors

### Function timeout
- Max duration is set to 60s in `vercel.json`
- If you need longer, increase `maxDuration` (max 300s on Pro plan)

## Architecture Notes

- **No queue system**: Unlock is synchronous (blocks until complete)
- **No background threads**: Everything runs within the request lifecycle
- **Stateless**: Each request may run on a different serverless instance
- **Session storage**: Flask sessions stored in encrypted cookies
- **Database**: All persistent data in Supabase Postgres

## Performance

- **Cold start**: ~1-2s (first request after idle period)
- **Warm requests**: ~200-500ms overhead
- **First unlock after cold start**: ~10-15s (includes Firebase login)
- **Subsequent unlocks**: ~5-10s (cached tokens, warm container)

## Limits (Vercel Free Tier)

- 100 GB bandwidth/month
- 100 hours serverless function execution/month
- 60s max function duration
- 1000 deployments/month

This should be more than enough for single-operator livestream demo use!

## Local Development

To test locally before deploying:

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file (copy from .env.example and fill in values)
cp .env.example .env

# Run with Vercel CLI (recommended)
npm i -g vercel
vercel dev

# Or run with Flask dev server
python wsgi.py
```

Visit http://localhost:3000 (Vercel) or http://localhost:5001 (Flask)
